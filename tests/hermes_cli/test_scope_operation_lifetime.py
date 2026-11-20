"""Port adaptation A6 (owner decisions d56ea47c): a default-only restriction lives exactly as long as its operation.

1. Same operation: a gateway the restricted operation starts (``spawn_unmanaged_gateway``) boots under it, and the
   shared resolver (``publication_scope``: PM client/worker, ``pm.cli``, arming, locked seams) keeps it after the
   tail cleared the marker. Independent: once a long-lived server ends its start-up operation
   (``end_launch_operation``), its later requests and the starts it makes for them take deferred work.
2. Default-only Git and ZIP updates refuse a target without the operation capability (v1-only, A4-only).
3. Pending work of another scope belongs to its original owner: explicit repeated default, flagless and the locked
   tail/completion seams skip or refuse with that owner's remedy, and leave its marker and record unchanged.

Synthetic homes and checkouts; the gateway spawn, completion child, PM currency and installer are doubles.
Nothing is started, fetched, installed or updated.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="default-only maintenance refuses Windows")

DEFERRED = {"ok": True, "skipped": "default-only maintenance", "owed": True}
LEGACY_ALL = "source update tail not finished\n"  # an all-profile owner's marker payload


def _tree(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(path.rglob("*"))}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A default home with a live named sibling, a self-managed checkout this process runs from, nothing admitted."""
    import pm
    import pm.client
    import pm.paths
    from hermes_cli import _launchers, maintenance_scope, venv_sync
    from pm import environments

    root = tmp_path / ".hermes"
    sibling = root / "profiles" / "sibling"
    sibling.mkdir(parents=True)
    for home in (root, sibling):
        (home / "config.yaml").write_text("config_version: 1\nmodel:\n  default: fixture-model\n")
        (home / ".env").write_text("FIXTURE=only\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    for name in ("HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD"):
        monkeypatch.delenv(name, raising=False)
    checkout = tmp_path / "source"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (checkout / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}), encoding="utf-8")
    monkeypatch.setattr(pm.paths, "install_root", lambda: checkout)
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(pm.client, "ensure_tools_for_sync", lambda: None)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda *a, **k: Path(sys.executable))
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda *a, **k: None)
    monkeypatch.setattr(environments, "activation_environment", lambda project_root: {})
    return SimpleNamespace(root=root, sibling=sibling, checkout=checkout,
                           marker=venv_sync.completion_pending_path(checkout),
                           attempts=venv_sync._completion_attempts_path(checkout))


@pytest.fixture
def boot(env, monkeypatch):
    """The native boot entry with the default-plugin step only; the every-home installer is a spy."""
    from hermes_cli import boot_bootstrap, default_plugins, post_update

    monkeypatch.setattr(boot_bootstrap, "current_install_identity", lambda project_root: "a" * 40)
    monkeypatch.setattr(default_plugins, "_identity", lambda: "a" * 40)
    monkeypatch.setattr(post_update, "BOOT_HOME_STEPS",
                        (("install_default_plugins", default_plugins.step_install_default_plugins),))
    spawns = []
    monkeypatch.setattr(default_plugins, "spawn_install", lambda home=None: spawns.append(home) or True)

    def start(*argv):
        monkeypatch.setattr(sys, "argv", ["hermes", *argv])
        boot_bootstrap.maybe_run_boot_bootstrap(env.checkout)
        record = boot_bootstrap.record_path(env.checkout)
        return json.loads(record.read_text(encoding="utf-8"))["results"]["install_default_plugins"] \
            if record.exists() else None
    return start, spawns


def _admit(monkeypatch):
    """This operation was admitted default-only and its launch tail has since cleared the marker."""
    from hermes_cli import maintenance_scope
    from hermes_cli.maintenance_scope import MaintenanceScope
    monkeypatch.setattr(maintenance_scope, "_admitted", MaintenanceScope.select("default"))


@pytest.fixture
def gateway_spawn(env, monkeypatch):
    """``spawn_unmanaged_gateway`` for the default home with its daemon launch captured, never started."""
    from hermes_cli import gateway_runtime_start

    launched = []
    monkeypatch.setattr(gateway_runtime_start, "project_root_str", lambda: str(env.checkout))
    monkeypatch.setattr(gateway_runtime_start, "subprocess", SimpleNamespace(
        DEVNULL=subprocess.DEVNULL, Popen=lambda command, **kwargs: launched.append(command) or SimpleNamespace()))

    def spawn():
        gateway_runtime_start.spawn_unmanaged_gateway(env.root, deadline=time.monotonic() + 60)
        return launched[-1]
    return spawn


def _carried(command):
    """The record the inline source assigns, read as a literal (never executed)."""
    source = command[command.index("-c") + 1]
    assigns = [node for node in ast.parse(source).body if isinstance(node, ast.Assign)
               and ast.unparse(node.targets[0]) == "sys._hermes_maintenance_scope"]
    assert len(assigns) == 1
    return ast.literal_eval(assigns[0].value)


# ── 1. same-operation carriers and independent controls ─────────────────────────────────────────────

def test_a_gateway_this_operation_starts_boots_under_its_restriction(env, boot, gateway_spawn, monkeypatch):
    from gateway.status import looks_like_gateway_command_line
    from hermes_cli import maintenance_scope
    from hermes_cli.maintenance_scope import admit_launch_scope, relaunch_handoff

    start, spawns = boot
    _admit(monkeypatch)
    command = gateway_spawn()
    assert command[:2] == [sys.executable, "-c"], "the started gateway would boot without the restriction"
    assert _carried(command) == relaunch_handoff(env.checkout)
    assert looks_like_gateway_command_line(" ".join(command)), "process identity readers lost the gateway"

    # The started gateway, a new process: nothing admitted, only the carried record; no marker is pending.
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    monkeypatch.setattr(sys, maintenance_scope.RELAUNCH_SCOPE_ATTRIBUTE, _carried(command), raising=False)
    assert not env.marker.exists()
    before = _tree(env.sibling)
    assert admit_launch_scope(env.checkout, ["--profile", "default", "gateway", "run", "--quiet"]).mode == "default"
    assert start("--profile", "default", "gateway", "run", "--quiet") == DEFERRED
    assert spawns == [] and _tree(env.sibling) == before


def test_independent_starts_and_requests_after_the_operation_ends_take_deferred_work(env, boot, gateway_spawn,
                                                                                     monkeypatch):
    from hermes_cli import default_plugins
    from hermes_cli.maintenance_scope import end_launch_operation, publication_scope

    start, spawns = boot
    # All-profile control: an ordinary operation starts the gateway with the unchanged command.
    assert gateway_spawn()[:3] == [sys.executable, "-m", "hermes_cli.main"]

    _admit(monkeypatch)
    assert publication_scope(env.checkout).mode == "default" and default_plugins.installs_deferred()
    end_launch_operation()  # a long-lived server finished its own start-up maintenance and now serves requests
    assert gateway_spawn()[:3] == [sys.executable, "-m", "hermes_cli.main"], "a later start inherited it"
    assert publication_scope(env.checkout).mode == "all" and not default_plugins.installs_deferred()
    assert start() == {"ok": True, "spawned": True, "owed": True} and spawns == [None]


def test_forwarded_same_operation_work_keeps_the_floor_after_the_tail_cleared_the_marker(env, monkeypatch):
    """``pm.cli`` (flagless install), the PM client's worker context and every arm resolve through
    ``publication_scope``: the operation's default survives the cleared marker and cannot be broadened."""
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal, publication_scope
    from hermes_cli.venv_sync import arm_completion

    _admit(monkeypatch)
    assert not env.marker.exists()
    assert publication_scope(env.checkout).mode == "default"
    with pytest.raises(ScopeSafetyRefusal, match="broaden"):
        publication_scope(env.checkout, "all")
    arm_completion(env.checkout)  # an arm inside this operation records ITS scope, never all
    assert json.loads(env.marker.read_text(encoding="utf-8"))["maintenance_scope"]["mode"] == "default"


# ── 2. default-only target capability: Git and ZIP validators ──────────────────────────────────────

V1_ONLY = b"MAINTENANCE_SCOPE_SCHEMA = 1\n"
A4_ONLY = b"MAINTENANCE_SCOPE_SCHEMA = 1\nRELAUNCH_SCOPE_ATTRIBUTE = '_hermes_maintenance_scope'\nRELAUNCH_SCOPE_SCHEMA = 1\n"


def _complete_target():
    from hermes_cli import maintenance_scope
    return Path(maintenance_scope.__file__).read_bytes()


@pytest.mark.parametrize("target, refused", [("v1-only", True), ("a4-only", True), ("complete", False)])
def test_default_only_git_update_needs_the_operation_capability(env, monkeypatch, target, refused):
    from hermes_cli import update_cmd_commit as commit
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope

    payload = {"v1-only": V1_ONLY, "a4-only": A4_ONLY, "complete": _complete_target()}[target]
    monkeypatch.setattr(commit, "read_target_files", lambda git, root, ref, rels: {rels[0]: payload})
    with maintenance_scope(MaintenanceScope.select("default")):
        refusal = commit.scope_target_refusal(["git"], env.checkout, "target")
    assert (refusal is not None and "target does not support" in refusal) is refused, refusal
    assert commit.scope_target_refusal(["git"], env.checkout, "target") is None  # the all-profile path


@pytest.mark.parametrize("target, refused", [("v1-only", True), ("a4-only", True), ("missing", True),
                                             ("complete", False)])
def test_default_only_zip_update_needs_the_operation_capability_before_its_commit_point(env, tmp_path, target,
                                                                                       refused):
    """The ZIP gate reads the extracted target's ``hermes_cli/maintenance_scope.py`` as data, before the commit
    point (``arm_commit_obligations``) and the first rename."""
    from hermes_cli import update_cmd_zip
    from hermes_cli.maintenance_scope import (
        MaintenanceScope, ScopeSafetyRefusal, maintenance_scope, require_scope_target,
    )

    extracted = tmp_path / "extracted"
    (extracted / "hermes_cli").mkdir(parents=True)
    if target != "missing":
        (extracted / "hermes_cli" / "maintenance_scope.py").write_bytes(
            {"v1-only": V1_ONLY, "a4-only": A4_ONLY, "complete": _complete_target()}[target])
    capability = extracted / "hermes_cli/maintenance_scope.py"
    with maintenance_scope(MaintenanceScope.select("default")):
        if refused:
            with pytest.raises(ScopeSafetyRefusal, match="target does not support"):
                require_scope_target(capability.read_bytes() if capability.is_file() else None)
        else:
            require_scope_target(capability.read_bytes())
    require_scope_target(V1_ONLY)  # the all-profile path never reads it
    source = Path(update_cmd_zip.__file__).read_text(encoding="utf-8")
    assert 0 < source.index("require_scope_target(capability.read_bytes()") < source.index(
        "_commit.arm_commit_obligations(root"), "the ZIP gate no longer precedes the commit point"


# ── 3. a conflicting broader pending obligation belongs to its original owner ─────────────────────

def _owner_marker(env):
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text(LEGACY_ALL, encoding="utf-8")
    env.attempts.write_text("1\n", encoding="utf-8")
    return env.marker.read_bytes(), env.attempts.read_bytes()


def test_explicit_repeated_default_never_takes_an_all_profile_owners_work(env, boot, monkeypatch):
    from hermes_cli import source_completion
    from hermes_cli.maintenance_scope import MaintenanceScope, ScopeSafetyRefusal, resolve_launch_scope
    from hermes_cli.venv_sync import arm_completion

    start, spawns = boot
    owned = _owner_marker(env)
    for argv in (["pm", "install", "--maintenance-scope", "default"],
                 ["update", "--maintenance-scope", "default", "--no-gateway-restart"]):
        with pytest.raises(ScopeSafetyRefusal, match="run `hermes update`"):
            resolve_launch_scope(env.checkout, argv)
    with pytest.raises(ScopeSafetyRefusal):  # never re-armed narrower over the owner's marker
        arm_completion(env.checkout, maintenance_scope=MaintenanceScope.select("default"))

    def tripwire(*a, **k):
        raise AssertionError("the locked completion ran over another owner's pending work")
    monkeypatch.setattr(source_completion, "_complete_scoped", tripwire)
    with pytest.raises(ScopeSafetyRefusal):  # the locked completion seam revalidates the same way
        source_completion._complete_locked(env.checkout, maintenance_scope=MaintenanceScope.select("default"))
    assert start("pm", "install", "--maintenance-scope", "default") is None, "boot ran instead of skipping"
    assert spawns == [] and (env.marker.read_bytes(), env.attempts.read_bytes()) == owned


def test_a_flagless_operation_refuses_activation_on_a_conflicting_owner(env, monkeypatch):
    from hermes_cli import update_custody, venv_sync
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal, resolve_launch_scope

    _admit(monkeypatch)
    owned = _owner_marker(env)
    monkeypatch.setattr(update_custody, "run", lambda *a, **k: pytest.fail("the tail ran for another owner"))
    with pytest.raises(ScopeSafetyRefusal, match="run `hermes update`"):
        resolve_launch_scope(env.checkout, [])
    with pytest.raises(ScopeSafetyRefusal):
        venv_sync.prepare_launch(env.checkout, [])
    assert (env.marker.read_bytes(), env.attempts.read_bytes()) == owned


def test_the_locked_tail_revalidates_and_never_clears_another_owners_marker(env, monkeypatch):
    from hermes_cli import update_custody, update_lock, venv_sync
    from hermes_cli.maintenance_scope import MaintenanceScope, ScopeSafetyRefusal, admit_launch_scope

    # Race 1: the owner's broader marker lands between the unlocked preflight and checkout custody.
    venv_sync.arm_completion(env.checkout, maintenance_scope=MaintenanceScope.select("default"))
    assert admit_launch_scope(env.checkout, []).mode == "default"
    real_acquire = update_lock.UpdateLock.acquire_checkout

    def acquire_then_owner_arms(self, install_root):
        acquired = real_acquire(self, install_root)
        env.marker.write_text(LEGACY_ALL, encoding="utf-8")
        return acquired
    monkeypatch.setattr(update_lock.UpdateLock, "acquire_checkout", acquire_then_owner_arms)
    monkeypatch.setattr(update_custody, "run", lambda *a, **k: pytest.fail("the tail ran for another owner"))
    with pytest.raises(ScopeSafetyRefusal):
        venv_sync.prepare_launch(env.checkout, [])
    assert env.marker.read_text(encoding="utf-8") == LEGACY_ALL and not env.attempts.exists()

    # Race 2: the owner's marker replaces ours while our tail runs; the tail succeeds, the marker stays.
    monkeypatch.setattr(update_lock.UpdateLock, "acquire_checkout", real_acquire)
    venv_sync.clear_completion(env.checkout)
    venv_sync.arm_completion(env.checkout)  # this operation's floor: a default marker

    def tail(command, **kwargs):
        env.marker.write_text(LEGACY_ALL, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(update_custody, "run", tail)
    assert venv_sync.prepare_launch(env.checkout, []) is None
    assert env.marker.read_text(encoding="utf-8") == LEGACY_ALL, "the tail cleared another owner's marker"
    assert venv_sync.clear_completion(env.checkout, expected=MaintenanceScope.select("default")) is False
