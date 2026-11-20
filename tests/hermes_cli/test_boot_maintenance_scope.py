"""Boot maintenance honours default-only maintenance scope at its native entry (port adaptation A3).

Native: ``maybe_run_boot_bootstrap`` (the call ``hermes_cli/main.py`` and ``gateway/run.py`` make),
launch-scope resolution from argv and the pending obligation, ``run_boot_bootstrap`` with its record and
owed-step retry, and the real ``install_default_plugins`` step. Doubles: the install identity, the
detached every-home installer (a spy that spawns nothing) and the in-process installers (tripwires).
Nothing is fetched, installed or launched. A disposable default home has one named sibling profile.

Port adaptation A4 (the selectors after the five A3 controls): one flagless invocation admitted
under a pending default-only obligation keeps that scope after its completed launch tail clears the
marker, through activation, the managed-interpreter relaunch and the main and gateway boot entries.
The launch chain is the real one hermes_bootstrap runs (admission, ``prepare_launch``,
``relaunch_command`` + ``os.execv``, activation scope, ``maybe_run_boot_bootstrap``); PM's sync and
currency answers, launcher publication and the completion child are doubles.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

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


# ── Port adaptation A4: an admitted default-only scope outlives its cleared marker in one invocation ──

REPO = Path(__file__).resolve().parents[2]
ALL_SCOPE_RETRY = {"ok": True, "spawned": True, "owed": True}
ENTRIES = pytest.mark.parametrize("argv", [(), ("gateway", "run")], ids=["main-entry", "gateway-entry"])


def _self_managed(checkout):
    """The A3 checkout as a self-updating install, so ``prepare_launch`` owns its launch tail."""
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (checkout / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}), encoding="utf-8")


def _handoff(root, checkout, **changes):
    """The record ``relaunch_command`` carries for a default-only invocation, optionally altered."""
    record = {"schema": 1, "root": str(checkout.resolve()),
              "maintenance_scope": {"mode": "default", "home": str(root.resolve())}}
    return {**record, **changes}


def _tail_scopes(tails):
    """(``--maintenance-scope`` the completion child was handed, scope bound around it) per tail."""
    return [(command[command.index("--maintenance-scope") + 1], scope["mode"]) for command, scope in tails]


@pytest.fixture
def invocation(homes, boot, monkeypatch):
    """One fresh process's launch chain on the self-managed checkout, in hermes_bootstrap's order.

    Doubles: PM's currency answer (current), the store interpreter (this one, so no relaunch),
    launcher publication and the completion child (records its argv and the bound scope).
    """
    import pm
    import pm.client
    from hermes_cli import _launchers, maintenance_scope, update_custody, venv_sync
    from pm import environments

    _root, _sibling, checkout = homes
    start, _spawns = boot
    _self_managed(checkout)
    monkeypatch.setattr(maintenance_scope, "_admitted", None)  # a new process: nothing admitted yet
    for name in ("HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(pm.client, "ensure_tools_for_sync", lambda: None)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda *a, **k: Path(sys.executable))
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda *a, **k: None)
    monkeypatch.setattr(environments, "activation_environment", lambda project_root: {})
    tails = []

    def tail(command, **kwargs):
        tails.append((command, maintenance_scope.current_scope().to_dict()))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(update_custody, "run", tail)

    def launch(*argv):
        """hermes_bootstrap's preflight admission, launch preparation and activation scope, then boot."""
        monkeypatch.setattr(sys, "argv", ["hermes", *argv])
        admitted = maintenance_scope.admit_launch_scope(checkout, sys.argv[1:])
        relaunch = venv_sync.prepare_launch(checkout, sys.argv[1:])
        with maintenance_scope.maintenance_scope(maintenance_scope.resolve_launch_scope(checkout, sys.argv[1:])):
            activated = maintenance_scope.current_scope()  # recover_if_needed + activate_dependencies run here
        return {"admitted": admitted.mode, "relaunch": relaunch, "activated": activated.mode,
                "boot": start(*argv)}
    return launch, tails


@ENTRIES
def test_an_admitted_flagless_launch_keeps_default_scope_after_its_tail_clears_the_marker(
        homes, boot, invocation, monkeypatch, argv):
    from hermes_cli import maintenance_scope
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion, completion_pending_path

    _root, sibling, checkout = homes
    _start, spawns = boot
    launch, tails = invocation
    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    before = _tree(sibling)

    seen = launch(*argv)
    assert _tail_scopes(tails) == [("default", "default")]
    assert not completion_pending_path(checkout).exists(), "the launch tail did not clear its marker"
    assert seen == {"admitted": "default", "relaunch": None, "activated": "default", "boot": DEFERRED}
    assert spawns == [], "the invocation that cleared its marker spawned the every-home installer"
    assert _tree(sibling) == before, "the invocation that cleared its marker wrote into the named sibling"

    # A later independent ordinary start (a new process: nothing admitted) takes the deferred work once.
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    assert launch(*argv) == {"admitted": "all", "relaunch": None, "activated": "all", "boot": ALL_SCOPE_RETRY}
    assert spawns == [None] and len(tails) == 1


# Run as real processes: the parent prepares, relaunches through relaunch_command + os.execv, and the
# child is a different interpreter process that receives only what the native seam carries.
_RELAUNCH_LAUNCH = r'''
import json, os, subprocess, sys
from pathlib import Path

repo, scratch, launch = os.environ["A4_REPO"], Path(os.environ["A4_SCRATCH"]), os.environ["A4_LAUNCH"]
checkout = Path(os.environ["A4_CHECKOUT"])
sys.argv = [sys.argv[0], *json.loads(os.environ["A4_COMMAND"])]  # a gateway argv alarms the test guard
sys.path.insert(0, repo)
generation = f"{launch}-child" if getattr(sys, "_hermes_relaunched", False) else launch


def note(name, value):
    with (scratch / name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value) + "\n")


note("generations", generation)
if len((scratch / "generations").read_text(encoding="utf-8").splitlines()) > 6:
    raise SystemExit(99)

import pm, pm.client, pm.environments
import hermes_cli.left_core_migration as lcm
from hermes_cli import _launchers, boot_bootstrap, default_plugins, post_update, update_custody, venv_sync
from hermes_cli import maintenance_scope as ms

state = scratch / "state"


def sync(*_args, **_kwargs):
    note("syncs", generation)
    state.write_text("current", encoding="utf-8")


def tail(command, **_kwargs):
    note("tails", [generation, command, ms.current_scope().to_dict()])
    return subprocess.CompletedProcess(command, 0)


def tripwire(*_args, **_kwargs):
    note("tripwires", generation)
    raise AssertionError("a default plugin install ran inside boot")


pm.venv_is_current = lambda **_kw: state.read_text(encoding="utf-8") == "current"
pm.sync_venv = sync
pm.client.ensure_tools_for_sync = lambda: None
pm.environments.activation_environment = lambda _root: {}
_launchers.resolve_store_python = lambda *_a, **_k: Path(sys.executable)
venv_sync.publish_launchers = lambda *_a, **_k: None
update_custody.run = tail
boot_bootstrap.current_install_identity = lambda project_root: "a" * 40
default_plugins._identity = lambda: "a" * 40
post_update.BOOT_HOME_STEPS = (("install_default_plugins", default_plugins.step_install_default_plugins),)
default_plugins.spawn_install = lambda home=None: note("spawns", [generation, home]) or True
default_plugins.install_defaults_all_homes = tripwire
lcm._install_into = tripwire

argv = sys.argv[1:]
note("stages", [generation, "carried", getattr(sys, ms.RELAUNCH_SCOPE_ATTRIBUTE, None)])
note("stages", [generation, "admitted", ms.admit_launch_scope(checkout, argv).mode])
python = venv_sync.prepare_launch(checkout, argv)
if python is not None:
    note("stages", [generation, "relaunch", None])
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(str(python), venv_sync.relaunch_command(python, checkout, sys.argv, sys.orig_argv, None))
with ms.maintenance_scope(ms.resolve_launch_scope(checkout, argv)):
    note("stages", [generation, "activated", ms.current_scope().mode])
boot_bootstrap.maybe_run_boot_bootstrap(checkout)
note("stages", [generation, "booted", None])
'''


def _run_launch(tmp_path, homes, launch, argv):
    root, _sibling, checkout = homes
    script = tmp_path / "launch.py"
    script.write_text(_RELAUNCH_LAUNCH, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(root),
           "HERMES_TEST_ISOLATION": os.environ.get("HERMES_TEST_ISOLATION", "1"),
           "A4_REPO": str(REPO), "A4_SCRATCH": str(tmp_path / "scratch"), "A4_LAUNCH": launch,
           "A4_CHECKOUT": str(checkout), "A4_COMMAND": json.dumps(list(argv))}
    return subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True,
                          timeout=120, check=False)


def _notes(scratch, name):
    path = scratch / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@ENTRIES
def test_a_relaunched_flagless_launch_carries_default_scope_through_the_native_handoff(tmp_path, homes, argv):
    from hermes_cli import boot_bootstrap
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion, completion_pending_path

    root, sibling, checkout = homes
    _self_managed(checkout)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "state").write_text("stale", encoding="utf-8")  # the parent syncs, then relaunches
    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    before = _tree(sibling)

    result = _run_launch(tmp_path, homes, "first", argv)
    assert result.returncode == 0, result.stderr
    assert _notes(scratch, "generations") == ["first", "first-child"], "exactly one managed-interpreter relaunch"
    assert _notes(scratch, "stages") == [
        ["first", "carried", None], ["first", "admitted", "default"], ["first", "relaunch", None],
        ["first-child", "carried", _handoff(root, checkout)], ["first-child", "admitted", "default"],
        ["first-child", "activated", "default"], ["first-child", "booted", None]]
    assert _notes(scratch, "syncs") == ["first"]
    tails = _notes(scratch, "tails")
    assert [generation for generation, _command, _scope in tails] == ["first"]
    assert _tail_scopes([(command, scope) for _generation, command, scope in tails]) == [("default", "default")]
    assert not completion_pending_path(checkout).exists(), "the parent's launch tail did not clear its marker"
    record = json.loads(boot_bootstrap.record_path(checkout).read_text(encoding="utf-8"))
    assert record["results"]["install_default_plugins"] == DEFERRED
    assert _notes(scratch, "spawns") == [] and _notes(scratch, "tripwires") == []
    assert _tree(sibling) == before, "the relaunched child wrote into the named sibling"

    # A later independent ordinary start (a new process, nothing carried) takes the deferred work once.
    result = _run_launch(tmp_path, homes, "later", argv)
    assert result.returncode == 0, result.stderr
    assert _notes(scratch, "stages")[7:] == [
        ["later", "carried", None], ["later", "admitted", "all"],
        ["later", "activated", "all"], ["later", "booted", None]]
    assert _notes(scratch, "spawns") == [["later", None]]
    record = json.loads(boot_bootstrap.record_path(checkout).read_text(encoding="utf-8"))
    assert record["results"]["install_default_plugins"] == ALL_SCOPE_RETRY
    assert len(_notes(scratch, "tails")) == 1 and _notes(scratch, "tripwires") == []


@pytest.mark.parametrize("carried", [
    lambda root, checkout, other: "default",
    lambda root, checkout, other: {**_handoff(root, checkout), "extra": 1},
    lambda root, checkout, other: _handoff(root, checkout, schema=True),
    lambda root, checkout, other: _handoff(root, checkout, schema=2),
    lambda root, checkout, other: _handoff(root, checkout, root=str(other)),
    lambda root, checkout, other: _handoff(root, checkout, maintenance_scope={"mode": "all", "home": None}),
    lambda root, checkout, other: _handoff(root, checkout, maintenance_scope={"mode": "default", "home": str(other)}),
], ids=["not-a-record", "extra-field", "boolean-schema", "unknown-schema", "foreign-checkout",
        "all-profile", "named-home"])
def test_a_malformed_or_foreign_scope_handoff_refuses_and_never_broadens(homes, boot, monkeypatch, carried):
    from hermes_cli import boot_bootstrap, maintenance_scope
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal

    root, sibling, checkout = homes
    _start, spawns = boot
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    monkeypatch.setattr(sys, "argv", ["hermes"])
    attribute = maintenance_scope.RELAUNCH_SCOPE_ATTRIBUTE
    # Control: the exact record a relaunch carries reads as default-only for this checkout.
    monkeypatch.setattr(sys, attribute, _handoff(root, checkout), raising=False)
    assert maintenance_scope.resolve_launch_scope(checkout, []).mode == "default"

    monkeypatch.setattr(sys, attribute, carried(root, checkout, sibling.resolve()), raising=False)
    with pytest.raises(ScopeSafetyRefusal):
        maintenance_scope.admit_launch_scope(checkout, [])
    assert maintenance_scope._admitted is None
    before = _tree(sibling)
    boot_bootstrap.maybe_run_boot_bootstrap(checkout)  # never raises
    assert not boot_bootstrap.record_path(checkout).exists(), "a refused handoff recorded this revision done"
    assert spawns == [] and _tree(sibling) == before


def test_an_admitted_default_scope_cannot_be_broadened_within_the_invocation(homes, boot, monkeypatch):
    from hermes_cli import boot_bootstrap, maintenance_scope
    from hermes_cli.maintenance_scope import MaintenanceScope, ScopeSafetyRefusal
    from hermes_cli.venv_sync import arm_completion, clear_completion

    _root, sibling, checkout = homes
    _start, spawns = boot
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    assert maintenance_scope.admit_launch_scope(checkout, []).mode == "default"
    clear_completion(checkout)  # this invocation's launch tail finished

    with pytest.raises(ScopeSafetyRefusal):  # an explicit all-profile flag
        maintenance_scope.resolve_launch_scope(checkout, ["pm", "install", "--maintenance-scope", "all"])
    # Another process armed an all-profile obligation meanwhile (its own marker payload): not this one's. A6:
    # an arm from THIS process would be this operation's and carry its floor, so it is written as that owner's.
    from hermes_cli.venv_sync import completion_pending_path
    completion_pending_path(checkout).write_text("source update tail not finished\n", encoding="utf-8")
    with pytest.raises(ScopeSafetyRefusal):
        maintenance_scope.resolve_launch_scope(checkout, [])
    before = _tree(sibling)
    monkeypatch.setattr(sys, "argv", ["hermes"])
    boot_bootstrap.maybe_run_boot_bootstrap(checkout)  # never raises
    assert not boot_bootstrap.record_path(checkout).exists()
    assert spawns == [] and _tree(sibling) == before


def test_the_relaunch_seam_carries_only_an_admitted_default_scope(homes, monkeypatch):
    from gateway.status import looks_like_gateway_command_line
    from hermes_cli import maintenance_scope, venv_sync
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion

    root, _sibling, checkout = homes
    monkeypatch.setattr(maintenance_scope, "_admitted", None)
    python = Path("/opt/venv/bin/python3")
    entry = [str(checkout / "hermes_cli" / "main.py"), "gateway", "run"]
    original = ["/old/python", "-m", "hermes_cli.main", "gateway", "run"]
    plain = venv_sync.relaunch_command(python, checkout, entry, original, "hermes_cli.main")
    assert maintenance_scope.RELAUNCH_SCOPE_ATTRIBUTE not in plain[-1], "an all-profile relaunch changed"

    arm_completion(checkout, maintenance_scope=MaintenanceScope.select("default"))
    assert maintenance_scope.admit_launch_scope(checkout, entry[1:]).mode == "default"
    carried = venv_sync.relaunch_command(python, checkout, entry, original, "hermes_cli.main")
    assert carried[:-1] == plain[:-1]
    assert carried[-1] == plain[-1].replace(
        "sys._hermes_relaunched = True; ",
        f"sys._hermes_relaunched = True; sys._hermes_maintenance_scope = {_handoff(root, checkout)!r}; ")
    # Process-identity readers (/proc, psutil, ps space-join argv) still see a gateway.
    assert looks_like_gateway_command_line(" ".join(carried))
