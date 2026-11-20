"""Port adaptation A5: authorized profile creation under default-only maintenance defers only the automatic
every-home default-plugin install, and one install-level request reopens it for a later ordinary start.

Native paths: ``hermes profile create`` (``profile_cmd._profile_create``) creating directly or through the local
owning gateway (``create_through_gateway`` → the real ``profiles.create`` receiver), ``POST /api/profiles``, the
shared ``spawn_install`` / ``_run_child`` / ``install_defaults_all_homes`` seams, and the boot step's owed retry.
Doubles: the gateway transport (the receiver runs in a fresh context with this process's admission and handoff
hidden, as the gateway process sees them), the install identity, the detached child's launch, the catalog and the
per-profile installer. Nothing is fetched, installed or launched. Sources of default scope:

* ``explicit``: a bound default scope (as ``--maintenance-scope default``);
* ``pending``: a pending default-only obligation of the checkout;
* ``carried``: this invocation's admitted default after its launch tail cleared the marker (A4);
* ``handoff``: a managed-interpreter relaunch's carried record after the marker was cleared (A4).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="default-only maintenance refuses Windows")

SOURCES = ("explicit", "pending", "carried", "handoff")
DEFERRED = {"ok": True, "skipped": "default-only maintenance", "owed": True}


def _tree(path):
    """Every path under *path* with its bytes and mtime: any write, create or delete changes it."""
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
            for p in sorted(path.rglob("*"))}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A disposable default home with one live named sibling, the checkout this process runs from, and a
    tripwire on every effect of the automatic installer (each records itself before raising)."""
    import hermes_cli.left_core_migration as lcm
    import hermes_cli.plugin_catalog as catalog
    import pm.paths
    from hermes_cli import _launchers, default_plugins, gateway_multiplex_served, maintenance_scope

    root = tmp_path / ".hermes"
    sibling = root / "profiles" / "sibling"
    sibling.mkdir(parents=True)
    for home in (root, sibling):
        (home / "config.yaml").write_text("config_version: 1\nmodel:\n  default: fixture-model\n")
        (home / ".env").write_text("FIXTURE=only\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)  # fetching allowed: the effects are reachable
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    checkout = tmp_path / "source"
    checkout.mkdir()
    monkeypatch.setattr(pm.paths, "install_root", lambda: checkout)
    monkeypatch.setattr(maintenance_scope, "_admitted", None)  # a fresh process: nothing admitted yet
    monkeypatch.setattr(gateway_multiplex_served, "notify_multiplexer_profiles_changed", lambda *a, **k: None)
    effects: list[str] = []

    def tripwire(name):
        def fire(*args, **kwargs):
            effects.append(name)
            raise AssertionError(f"automatic installer effect under default-only maintenance: {name}")
        return fire
    monkeypatch.setattr(_launchers, "runtime_command", tripwire("detached spawn"))
    monkeypatch.setattr(catalog, "fetch_live_catalog", tripwire("catalog fetch"))
    monkeypatch.setattr(default_plugins, "default_entries", tripwire("catalog read"))
    monkeypatch.setattr(lcm, "_install_into", tripwire("per-profile install"))
    return SimpleNamespace(root=root, sibling=sibling, checkout=checkout, effects=effects,
                           owed=root / "cache" / "default-plugins.owed",
                           settled=root / "cache" / "default-plugins.settled",
                           rerun=root / "cache" / "default-plugins.rerun",
                           failed=root / "cache" / "left-core-default-plugins.failed")


def _scope(env, source, monkeypatch):
    """Put this process under default-only maintenance from *source*; the context the create runs in."""
    from hermes_cli.maintenance_scope import (
        RELAUNCH_SCOPE_ATTRIBUTE, MaintenanceScope, admit_launch_scope, maintenance_scope,
    )
    from hermes_cli.venv_sync import arm_completion, clear_completion

    if source == "explicit":
        return maintenance_scope(MaintenanceScope.select("default"))
    arm_completion(env.checkout, maintenance_scope=MaintenanceScope.select("default"))
    if source == "carried":
        assert admit_launch_scope(env.checkout, ["profile", "create"]).mode == "default"
        clear_completion(env.checkout)  # this invocation's launch tail finished: only the admitted scope remains
    elif source == "handoff":
        record = {"schema": 1, "root": str(env.checkout.resolve()),
                  "maintenance_scope": MaintenanceScope.select("default").to_dict()}
        clear_completion(env.checkout)  # the parent's tail cleared it before the relaunch
        monkeypatch.setattr(sys, RELAUNCH_SCOPE_ATTRIBUTE, record, raising=False)
    return contextlib.nullcontext()


def _cli_create(name):
    """``hermes profile create <name> --no-alias --no-skills`` through the CLI's own handler."""
    import argparse

    from hermes_cli import profile_cmd
    from hermes_cli.subcommands.profile import build_profile_parser
    parser = argparse.ArgumentParser()
    build_profile_parser(parser.add_subparsers(), cmd_profile=lambda args: None)
    profile_cmd._profile_create(parser.parse_args(["profile", "create", name, "--no-alias", "--no-skills"]))


@pytest.fixture
def owning_gateway(monkeypatch):
    """A live owning gateway for this home, reached in-process: the real ``profiles.create`` receiver runs in a
    fresh context with this process's admission and relaunch handoff hidden, as the gateway process sees them.
    ``older`` makes it refuse the new key as a gateway that predates it does (``extra="forbid"``: 4000)."""
    from hermes_cli import gateway_client, maintenance_scope, sessions_owner
    from tui_gateway import server

    state = SimpleNamespace(sent=[], older=False)
    monkeypatch.setattr(sessions_owner, "live_owner", lambda: SimpleNamespace(instance_id="owner-fixture"))

    class Client:
        async def rpc(self, method, _timeout=None, **params):
            state.sent.append(params)
            if state.older and "defer_default_plugins" in params:
                raise gateway_client.GatewayRPCError("invalid_params", code=4000)
            attribute = maintenance_scope.RELAUNCH_SCOPE_ATTRIBUTE
            admitted, carried = maintenance_scope._admitted, getattr(sys, attribute, None)
            maintenance_scope._admitted = None
            if carried is not None:
                delattr(sys, attribute)
            try:
                answer = contextvars.Context().run(server._methods[method], 1, params)
            finally:
                maintenance_scope._admitted = admitted
                if carried is not None:
                    setattr(sys, attribute, carried)
            if "error" in answer:
                error = answer["error"]
                raise gateway_client.GatewayRPCError("refused", code=error["code"], data=error.get("data"))
            return answer["result"]

    @contextlib.asynccontextmanager
    async def connect(endpoint=None, *args, **kwargs):
        yield Client()
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    return state


@pytest.fixture
def web(env, monkeypatch):
    """The dashboard's ``POST /api/profiles`` on this home."""
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", env.root / "state.db")
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def _assert_deferred_create(env, name, before):
    profile = env.root / "profiles" / name
    assert (profile / "config.yaml").is_file(), "the authorized create did not proceed"
    assert "_default_plugins_installed" not in (profile / "config.yaml").read_text(encoding="utf-8")
    assert env.effects == [], f"automatic every-home work ran: {env.effects}"
    assert _tree(env.sibling) == before, "the create changed an unrelated profile"
    assert env.owed.is_file(), "no durable install-level request was left"
    assert not env.rerun.exists() and not env.settled.exists() and not env.failed.exists()


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("path", ["cli-direct", "cli-owning-gateway"])
def test_cli_creation_proceeds_and_defers_only_the_every_home_installer(env, monkeypatch, request, path, source):
    from hermes_cli import sessions_owner
    from tui_gateway.contracts.profiles_vault_complete_foreign_subagents import ProfilesCreateParams

    if path == "cli-direct":
        monkeypatch.setattr(sessions_owner, "live_owner", lambda: None)  # no gateway serves this home
    else:
        gateway = request.getfixturevalue("owning_gateway")
    before = _tree(env.sibling)
    with _scope(env, source, monkeypatch):
        _cli_create("fresh")
    _assert_deferred_create(env, "fresh", before)
    if path == "cli-owning-gateway":
        # The CLI's scope crossed the owning gateway as the declared carrier (the gateway itself saw no
        # admission or handoff), and the receiver's contract accepts it.
        assert [sent.get("defer_default_plugins") for sent in gateway.sent] == [True]
        ProfilesCreateParams.model_validate(gateway.sent[0])


@pytest.mark.parametrize("source", ["pending", "carried", "handoff"])
def test_web_creation_proceeds_and_defers_only_the_every_home_installer(env, web, monkeypatch, source):
    """The dashboard process's own default-only maintenance. An explicit flag scopes ``update`` / ``pm install``
    invocations only, never the dashboard, so it reaches this route only as pending or carried scope."""
    before = _tree(env.sibling)
    with _scope(env, source, monkeypatch):
        response = web.post("/api/profiles", json={"name": "webfresh", "no_skills": True})
    assert response.status_code == 200, response.text
    _assert_deferred_create(env, "webfresh", before)


def test_an_older_owning_gateway_never_drops_the_restriction(env, owning_gateway, monkeypatch):
    """A gateway that predates the carrier refuses it before creating (4000); the create then runs here,
    under this process's own check. A URL's gateway that predates it is an error, never a silent create."""
    from hermes_cli import profile_provision, profile_provision_gateway
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    owning_gateway.older = True
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        _cli_create("fallback")
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == [True]
    _assert_deferred_create(env, "fallback", before)

    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", "ws://127.0.0.1:9/api/ws")
    monkeypatch.setattr("hermes_cli.gateway_runtime.gateway_profile_home", lambda home: env.sibling)
    with pytest.raises(GatewayCreateError, match="predates this version"):
        profile_provision_gateway.create_through_gateway(profile_provision.ProfileCreateRequest(
            name="byurl", no_skills=True, alias=False))
    assert not (env.root / "profiles" / "byurl").exists()


def test_an_ordinary_create_keeps_its_wire_and_its_installer(env, owning_gateway, monkeypatch):
    """All-profile control: no carrier on the wire (the request is the pre-A5 one), the detached installer
    starts once for the new profile, and nothing is owed."""
    from hermes_cli import _launchers, default_plugins

    launched = []
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, module=None: ["fixture-child", module])
    monkeypatch.setattr(default_plugins, "subprocess", SimpleNamespace(
        DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **kwargs: launched.append(argv)))
    _cli_create("ordinary")
    assert "defer_default_plugins" not in owning_gateway.sent[0]
    assert launched == [["fixture-child", "hermes_cli.default_plugins"]]
    assert not env.owed.exists() and env.rerun.exists()


@pytest.mark.parametrize("source", SOURCES)
def test_the_child_and_the_installer_never_scan_under_default_only_maintenance(env, monkeypatch, source):
    from hermes_cli import default_plugins

    before = _tree(env.sibling)
    with _scope(env, source, monkeypatch):
        assert default_plugins._run_child() == 0
        assert default_plugins.install_and_record(wait=1.0) == {"installed": [], "owed": [], "deferred": True}
        assert default_plugins.spawn_install(env.sibling) is False
    assert env.effects == [] and _tree(env.sibling) == before
    assert env.owed.is_file() and not env.failed.exists() and not env.settled.exists() and not env.rerun.exists()


def _entry(name):
    from hermes_cli.plugin_catalog import entry_from_mapping
    return entry_from_mapping({"name": name, "repo": "https://github.com/NousResearch/hermes-official-plugins",
                               "sha": "a" * 40, "description": "d", "maintainer": "NousResearch",
                               "tier": "official", "default": True}, name)


def test_a_deferred_create_after_a_settled_revision_stays_owed_until_a_later_ordinary_start(env, monkeypatch):
    import hermes_cli.left_core_migration as lcm
    import hermes_cli.plugin_catalog as catalog
    from hermes_cli import _launchers, boot_bootstrap, default_plugins, post_update
    from hermes_cli.venv_sync import clear_completion

    monkeypatch.setattr(boot_bootstrap, "current_install_identity", lambda project_root: "a" * 40)
    monkeypatch.setattr(default_plugins, "_identity", lambda: "a" * 40)
    monkeypatch.setattr(post_update, "BOOT_HOME_STEPS",
                        (("install_default_plugins", default_plugins.step_install_default_plugins),))

    def boot():
        monkeypatch.setattr(sys, "argv", ["hermes"])
        boot_bootstrap.maybe_run_boot_bootstrap(env.checkout)  # never raises; a missing record fails below
        record = json.loads(boot_bootstrap.record_path(env.checkout).read_text(encoding="utf-8"))
        return record["results"]["install_default_plugins"]

    # This revision had settled: a clean pass recorded it, and the default profile's boot record closed.
    for home in (env.root, env.sibling):  # both already have the default; the sibling then removed it (a choice)
        (home / "config.yaml").write_text("config_version: 1\n_default_plugins_installed: [accent-picker]\n")
    env.settled.parent.mkdir(parents=True, exist_ok=True)
    env.settled.write_text("a" * 40, encoding="utf-8")
    assert boot() == {"ok": True}

    # A create under a pending default-only obligation leaves exactly one install-level request.
    with _scope(env, "pending", monkeypatch):
        from hermes_cli import sessions_owner
        monkeypatch.setattr(sessions_owner, "live_owner", lambda: None)
        _cli_create("late")
        assert env.owed.is_file() and env.effects == []
        # A same-scope retry leaves it owed, with nothing started.
        assert boot() == DEFERRED
        assert env.owed.is_file() and env.effects == []
    clear_completion(env.checkout)  # the obligation finished; a later independent ordinary start follows

    launched = []
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, module=None: ["fixture-child", module])
    monkeypatch.setattr(default_plugins, "subprocess", SimpleNamespace(
        DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **kwargs: launched.append(argv)))
    assert boot() == {"ok": True, "spawned": True, "owed": True}, "the settled revision hid the request"
    assert launched == [["fixture-child", "hermes_cli.default_plugins"]]

    # The child's pass under the existing per-profile choices and retry policy, then the request is consumed.
    installed = []

    def install_into(home, consent=None):
        def install(name, discard=None):
            installed.append((home.name, name))
            (home / "plugins" / name).mkdir(parents=True, exist_ok=True)
            return {"ok": True}
        return install
    monkeypatch.setattr(lcm, "_install_into", install_into)
    monkeypatch.setattr(default_plugins, "default_entries", lambda: [_entry("accent-picker")])
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda **kwargs: {"entries": [{"name": "accent-picker"}]})
    past = time.time() - 60  # written before the pass began
    os.utime(env.owed, (past, past))
    assert default_plugins.install_and_record(wait=1.0) == {"installed": ["accent-picker"], "owed": []}
    assert installed == [("late", "accent-picker")], "the pass did not honour the existing per-profile choices"
    assert not env.owed.exists() and env.settled.read_text(encoding="utf-8") == "a" * 40
    assert boot() == {"ok": True} and len(launched) == 1
