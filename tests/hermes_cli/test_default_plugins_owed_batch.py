"""Port adaptation A7: a deferred default-plugin request recorded by another process while a pass runs is never lost.

Each pass captures the requests standing before it reads any home (``default_plugins._owed_batch``) and answers
only those, and only on a complete ordinary success: a clean installer pass (``install_and_record``) or the boot step
when every home denies fetching (``step_install_default_plugins``). A failed, partial or newly deferred pass answers
none. The deferral is the native helper (``_owe_deferred``) run in a real second process that exits before the pass
ends; the doubles are the per-home installer pass and, for the boot step, the fetch-policy read, which run that second
process at the point a concurrent writer would land. Nothing is fetched, installed or launched in this process.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="default-only maintenance refuses Windows")

REPO = Path(__file__).resolve().parents[2]
CLEAN = {"installed": [], "owed": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A disposable default home (the dependency root) and checkout; no maintenance scope in this process."""
    import pm.paths
    from hermes_cli import default_plugins, maintenance_scope

    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("config_version: 1\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    checkout = tmp_path / "source"
    checkout.mkdir()
    monkeypatch.setattr(pm.paths, "install_root", lambda: checkout)
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    monkeypatch.setattr(default_plugins, "_identity", lambda: "a" * 40)
    return SimpleNamespace(root=root, checkout=checkout, owed=root / "cache" / "default-plugins.owed.d",
                           settled=root / "cache" / "default-plugins.settled",
                           legacy=root / "cache" / "default-plugins.owed")


def _owed(env):
    return sorted(path.name for path in env.owed.iterdir()) if env.owed.is_dir() else []


def _defer_in_another_process(env):
    """A second process records one deferred request through the native helper, then exits."""
    code = ("import sys; from pathlib import Path; from hermes_cli.default_plugins import _owe_deferred; "
            "_owe_deferred(Path(sys.argv[1]))")
    subprocess.run([sys.executable, "-c", code, str(env.root)], cwd=REPO, check=True, timeout=60,
                   env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})


def _restrict_in_another_process(env):
    """A second process begins default-only maintenance of this checkout the native way (a pending default-only
    obligation, ``venv_sync.arm_completion``), then exits: the restriction appears while this process's pass runs."""
    code = ("import sys; from pathlib import Path; from hermes_cli.maintenance_scope import MaintenanceScope; "
            "from hermes_cli.venv_sync import arm_completion; "
            "arm_completion(Path(sys.argv[1]), maintenance_scope=MaintenanceScope.select('default'))")
    subprocess.run([sys.executable, "-c", code, str(env.checkout)], cwd=REPO, check=True, timeout=60,
                   env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})


def _legacy_write_in_another_process(env):
    """An A5/A6 build's writer in a second process: ``_owe_deferred`` was ``owed.parent.mkdir(...); owed.touch()``."""
    code = ("import sys; from pathlib import Path; owed = Path(sys.argv[1]); "
            "owed.parent.mkdir(parents=True, exist_ok=True); owed.touch()")
    subprocess.run([sys.executable, "-c", code, str(env.legacy)], cwd=REPO, check=True, timeout=60,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_a_clean_installer_pass_answers_only_the_requests_it_captured(env, monkeypatch):
    from hermes_cli import default_plugins

    default_plugins._owe_deferred(env.root)
    captured = _owed(env)

    def pass_with_a_concurrent_deferral(**kwargs):
        _defer_in_another_process(env)  # after the pass captured its batch, before it ends
        return dict(CLEAN)
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", pass_with_a_concurrent_deferral)
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    newer = _owed(env)
    assert len(captured) == len(newer) == 1 and newer != captured, "the captured request stayed or the newer was lost"
    assert default_plugins.deferred_request_owed() and env.settled.read_text(encoding="utf-8") == "a" * 40
    # The other process has exited; a later independent pass answers its request.
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", lambda **kwargs: dict(CLEAN))
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    assert _owed(env) == [] and not default_plugins.deferred_request_owed()


def test_the_all_denied_boot_step_answers_only_the_requests_it_captured(env, monkeypatch):
    from hermes_cli import default_plugins

    (env.root / "config.yaml").write_text("config_version: 1\nsecurity:\n  allow_lazy_installs: false\n")
    default_plugins._owe_deferred(env.root)
    captured = _owed(env)
    real, read = default_plugins.fetch_policy, []

    def policy_with_a_concurrent_deferral(home):
        if not read:
            _defer_in_another_process(env)  # while the step enumerates the homes, after it captured its batch
        read.append(home)
        return real(home)
    monkeypatch.setattr(default_plugins, "fetch_policy", policy_with_a_concurrent_deferral)
    assert default_plugins.step_install_default_plugins() == {"ok": True, "skipped": "lazy-installs-disabled"}
    newer = _owed(env)
    assert read and len(newer) == 1 and newer != captured
    # A later independent boot (every home still denies fetching) answers the newer request.
    monkeypatch.setattr(default_plugins, "fetch_policy", real)
    assert default_plugins.step_install_default_plugins() == {"ok": True, "skipped": "lazy-installs-disabled"}
    assert _owed(env) == []


@pytest.mark.parametrize("outcome", [
    {"installed": [], "owed": ["live catalog"]},       # failed or partial pass
    {"installed": [], "owed": [], "deferred": True},  # a pass that became restricted
], ids=["failed-or-partial", "deferred"])
def test_failed_partial_and_deferred_passes_answer_no_request(env, monkeypatch, outcome):
    from hermes_cli import default_plugins

    default_plugins._owe_deferred(env.root)
    captured = _owed(env)

    def pass_(**kwargs):
        _defer_in_another_process(env)
        return dict(outcome)
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", pass_)
    default_plugins.install_and_record(wait=1.0)
    after = _owed(env)
    assert set(captured) < set(after), "a request was answered by a pass that did not complete"
    assert len(after) == (3 if outcome.get("deferred") else 2)  # a deferred pass also leaves its own request
    assert not env.settled.exists()


def test_an_earlier_builds_single_request_counts_and_is_answered_once(env, monkeypatch):
    from hermes_cli import default_plugins

    legacy = env.root / "cache" / "default-plugins.owed"  # the A5/A6 single marker
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    assert default_plugins.deferred_request_owed()
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", lambda **kwargs: dict(CLEAN))
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    assert not legacy.exists() and not default_plugins.deferred_request_owed()


# ── A7.2 (Hermes 49979351): only a complete pass that is still ordinary settles; a legacy request is claimed ──

def test_a_restriction_arriving_before_settlement_leaves_the_batch_owed(env, monkeypatch):
    """The pass completes cleanly, but default-only maintenance begins in another process before it settles: nothing
    captured is answered, the revision is not marked settled, and a later ordinary pass answers it."""
    from hermes_cli import default_plugins
    from hermes_cli.venv_sync import clear_completion

    default_plugins._owe_deferred(env.root)
    captured = _owed(env)

    def pass_then_restriction(**kwargs):
        _restrict_in_another_process(env)  # after the pass's last per-home check, before settlement
        return dict(CLEAN)
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", pass_then_restriction)
    assert default_plugins.install_and_record(wait=1.0) == {**CLEAN, "deferred": True}
    assert set(captured) < set(_owed(env)), "the captured request was settled by a pass that ended restricted"
    assert not env.settled.exists()
    clear_completion(env.checkout)  # the restricted operation finished; an independent ordinary start follows
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", lambda **kwargs: dict(CLEAN))
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    assert _owed(env) == [] and env.settled.read_text(encoding="utf-8") == "a" * 40


def test_a_restriction_arriving_during_the_all_denied_scan_leaves_the_batch_owed(env, monkeypatch):
    """The all-denied shortcut settles only after a complete scan that is still ordinary."""
    from hermes_cli import default_plugins
    from hermes_cli.venv_sync import clear_completion

    (env.root / "config.yaml").write_text("config_version: 1\nsecurity:\n  allow_lazy_installs: false\n")
    default_plugins._owe_deferred(env.root)
    captured = _owed(env)
    real, read = default_plugins.fetch_policy, []

    def policy_with_a_restriction(home):
        if not read:
            _restrict_in_another_process(env)  # while the step lists the homes
        read.append(home)
        return real(home)
    monkeypatch.setattr(default_plugins, "fetch_policy", policy_with_a_restriction)
    assert default_plugins.step_install_default_plugins() == {
        "ok": True, "skipped": "default-only maintenance", "owed": True}
    assert read and _owed(env) == captured
    clear_completion(env.checkout)
    monkeypatch.setattr(default_plugins, "fetch_policy", real)
    assert default_plugins.step_install_default_plugins() == {"ok": True, "skipped": "lazy-installs-disabled"}
    assert _owed(env) == []


def test_a_legacy_request_written_during_a_pass_is_not_lost(env, monkeypatch):
    """An A5/A6 build's single marker is claimed (renamed into ``owed.d``) before the pass reads any home; an A5/A6
    writer in another process that writes the shared name during the pass leaves a new marker, which stays owed."""
    from hermes_cli import default_plugins

    env.legacy.parent.mkdir(parents=True)
    env.legacy.touch()

    def pass_with_a_legacy_writer(**kwargs):
        assert not env.legacy.exists(), "the pass began without claiming the earlier build's request"
        _legacy_write_in_another_process(env)
        return dict(CLEAN)
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", pass_with_a_legacy_writer)
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    assert env.legacy.is_file() and _owed(env) == [], "the claimed request stayed, or the newer one was lost"
    assert default_plugins.deferred_request_owed()
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", lambda **kwargs: dict(CLEAN))
    assert default_plugins.install_and_record(wait=1.0) == CLEAN
    assert not env.legacy.exists() and not default_plugins.deferred_request_owed()
