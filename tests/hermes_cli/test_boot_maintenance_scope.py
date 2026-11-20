"""Boot maintenance honours default-only maintenance scope at its native entry (port adaptation A3).

Native: ``maybe_run_boot_bootstrap`` (the call ``hermes_cli/main.py`` and ``gateway/run.py`` make),
launch-scope resolution from argv and the pending obligation, ``run_boot_bootstrap`` with its record and
owed-step retry, and the real ``install_default_plugins`` step. Doubles: the install identity, the
detached every-home installer (a spy that spawns nothing) and the in-process installers (tripwires).
Nothing is fetched, installed or launched. A disposable default home has one named sibling profile.
"""
import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="default-only maintenance refuses Windows")

DEFERRED = {"ok": True, "skipped": "default-only maintenance", "owed": True}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    sibling = root / "profiles" / "sibling"
    sibling.mkdir(parents=True)
    for home in (root, sibling):
        (home / "config.yaml").write_text("config_version: 1\nmodel:\n  default: fixture-model\n")
        (home / ".env").write_text("FIXTURE=only\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)  # fetching allowed: the spawn is reachable
    checkout = tmp_path / "source"
    checkout.mkdir()
    return root, sibling, checkout


@pytest.fixture
def boot(homes, monkeypatch):
    import hermes_cli.left_core_migration as lcm
    from hermes_cli import boot_bootstrap, default_plugins, post_update

    _root, _sibling, checkout = homes
    monkeypatch.setattr(boot_bootstrap, "current_install_identity", lambda project_root: "a" * 40)
    monkeypatch.setattr(default_plugins, "_identity", lambda: "a" * 40)  # no settled stamp: revision unsettled
    monkeypatch.setattr(post_update, "BOOT_HOME_STEPS",
                        (("install_default_plugins", default_plugins.step_install_default_plugins),))
    spawns = []
    monkeypatch.setattr(default_plugins, "spawn_install", lambda home=None: spawns.append(home) or True)

    def tripwire(*args, **kwargs):
        raise AssertionError("a default plugin install ran inside boot")
    monkeypatch.setattr(default_plugins, "install_defaults_all_homes", tripwire)
    monkeypatch.setattr(lcm, "_install_into", tripwire)

    def start(*argv):
        monkeypatch.setattr(sys, "argv", ["hermes", *argv])
        boot_bootstrap.maybe_run_boot_bootstrap(checkout)  # never raises; a missing record fails below
        record = json.loads(boot_bootstrap.record_path(checkout).read_text(encoding="utf-8"))
        return record["results"]["install_default_plugins"]
    return start, spawns


def _tree(path):
    """Every path under *path* with its bytes and mtime: any write, create or delete changes it."""
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(path.rglob("*"))}


def test_explicit_pm_install_scope_defers_the_every_home_installer_and_keeps_it_owed(homes, boot):
    _root, sibling, _checkout = homes
    start, spawns = boot
    before = _tree(sibling)

    assert start("pm", "install", "--maintenance-scope", "default") == DEFERRED
    assert spawns == [], "default-only boot spawned the every-home installer"
    assert _tree(sibling) == before, "default-only boot wrote into the named sibling"

    # The debt survives: the next ordinary start retries the owed step, unchanged, exactly once.
    assert start("pm", "install") == {"ok": True, "spawned": True, "owed": True}
    assert spawns == [None]


def test_a_pending_default_only_obligation_scopes_every_flagless_boot(homes, boot):
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion, clear_completion

    _root, sibling, checkout = homes
    start, spawns = boot
    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    before = _tree(sibling)

    # A flagless CLI start (first run), a gateway boot and PM without its flag (owed-step retries).
    for argv in ((), ("gateway", "run"), ("pm", "install")):
        assert start(*argv) == DEFERRED, argv
    assert spawns == [] and _tree(sibling) == before

    clear_completion(checkout)  # the scoped obligation finished: ordinary all-profile boot again
    assert start() == {"ok": True, "spawned": True, "owed": True}
    assert spawns == [None]


def test_an_all_scope_control_spawns_the_unchanged_installer_once(homes, boot):
    from pm.plugins_state import dependency_homes

    _root, sibling, _checkout = homes
    start, spawns = boot
    # The named sibling is live, so the every-home installer this control spawns would reach it.
    assert sibling.resolve() in [home.resolve() for home in dependency_homes()]
    assert start("pm", "install", "--maintenance-scope", "all") == {"ok": True, "spawned": True, "owed": True}
    assert spawns == [None]


def test_an_unprovable_scope_skips_boot_without_spending_its_debt(homes, boot, monkeypatch):
    from hermes_cli import boot_bootstrap
    from hermes_cli.venv_sync import completion_pending_path

    _root, sibling, checkout = homes
    _start, spawns = boot
    marker = completion_pending_path(checkout)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"schema": 1}\n', encoding="utf-8")  # unreadable scope: never read as permission
    before = _tree(sibling)

    monkeypatch.setattr(sys, "argv", ["hermes"])
    boot_bootstrap.maybe_run_boot_bootstrap(checkout)  # never raises
    assert not boot_bootstrap.record_path(checkout).exists(), "a refused scope recorded this revision done"
    assert spawns == [] and _tree(sibling) == before


def test_a_direct_post_update_run_honours_a_pending_default_only_obligation(homes, boot, monkeypatch):
    from hermes_cli import default_plugins, post_update
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion, clear_completion

    _root, sibling, checkout = homes
    _start, spawns = boot
    monkeypatch.setattr(post_update, "install_root", lambda: checkout)
    monkeypatch.setattr(post_update, "HOME_STEPS",
                        (("install_default_plugins", default_plugins.step_install_default_plugins),))
    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    before = _tree(sibling)

    assert post_update.main(["--scope", "home"]) == 0  # `python -m hermes_cli.post_update`: no launcher prologue
    assert spawns == [] and _tree(sibling) == before

    clear_completion(checkout)  # all-profile control: the unchanged step spawns the installer once
    assert post_update.main(["--scope", "home"]) == 0
    assert spawns == [None]
