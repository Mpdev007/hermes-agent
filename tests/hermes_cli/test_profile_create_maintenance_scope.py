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
import subprocess
import sys
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
                           owed=root / "cache" / "default-plugins.owed.d",  # A7: one file per deferred request
                           settled=root / "cache" / "default-plugins.settled",
                           rerun=root / "cache" / "default-plugins.rerun",
                           failed=root / "cache" / "left-core-default-plugins.failed")


def _owed(env):
    """The install-level deferred requests standing now (A7: one file each)."""
    return sorted(path.name for path in env.owed.iterdir()) if env.owed.is_dir() else []


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
    ``older`` names the keys an older gateway lacks: it refuses them as its dispatcher does (``extra="forbid"``:
    4000, the key only in the message text), and the client receives that through ``gateway_client.rpc_error``."""
    from hermes_cli import gateway_client, maintenance_scope, sessions_owner
    from tui_gateway import server

    state = SimpleNamespace(sent=[], older=None, attach_error=None, refusal=None)
    monkeypatch.setattr(sessions_owner, "live_owner", lambda: SimpleNamespace(instance_id="owner-fixture"))

    class Client:
        async def rpc(self, method, _timeout=None, **params):
            state.sent.append(params)
            if state.refusal is not None:  # A7: the authenticated owner refuses
                raise state.refusal
            if state.older is not None and set(params) & set(state.older):
                # A7.1: what an owner that predates these keys answers, received through the client boundary.
                raise _older_owner_refusal(params, state.older)
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
        if state.attach_error is not None:  # A7: dial, ticket, identity or protocol check fails before attaching
            raise state.attach_error
        yield Client()
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    return state


@pytest.fixture
def web(env, monkeypatch):
    """The dashboard's ``POST /api/profiles`` on this home (A7: never skipped; fastapi is a core dependency)."""
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
    assert _owed(env), "no durable install-level request was left"
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


@pytest.mark.parametrize("source", ["pending"])
def test_web_creation_proceeds_and_defers_only_the_every_home_installer(env, web, monkeypatch, source):
    """A dashboard request under a genuinely pending default-only obligation (A7: a carried or admitted one ended
    when the server began serving; see the next test). An explicit flag scopes ``update`` / ``pm install``
    invocations only, never the dashboard."""
    before = _tree(env.sibling)
    with _scope(env, source, monkeypatch):
        response = web.post("/api/profiles", json={"name": "webfresh", "no_skills": True})
    assert response.status_code == 200, response.text
    _assert_deferred_create(env, "webfresh", before)


def test_an_older_owning_gateway_never_drops_the_restriction(env, owning_gateway, monkeypatch):
    """A7.1 (Hermes 27d159d7): a gateway that predates the carrier (ba0482b4 lacks only it) refuses it before
    creating with 4000, which reaches this client as ``(request_failed, 4000, {})``: it cannot prove the refused
    key is the restriction's, so the create is unsupported, locally as at a URL; never a create without it."""
    from hermes_cli import profile_provision, profile_provision_gateway
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    owning_gateway.older = ("defer_default_plugins",)
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        with pytest.raises(GatewayCreateError, match=r"\(unsupported\); nothing was created"):
            profile_provision_gateway.create_through_gateway(_request("fallback"))
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == [True]
    assert not (env.root / "profiles" / "fallback").exists() and env.effects == [] and _tree(env.sibling) == before

    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", "ws://127.0.0.1:9/api/ws")
    monkeypatch.setattr("hermes_cli.gateway_runtime.gateway_profile_home", lambda home: env.sibling)
    sent = len(owning_gateway.sent)
    with pytest.raises(GatewayCreateError, match=r"\(unsupported\); nothing was created and nothing was retried"):
        profile_provision_gateway.create_through_gateway(profile_provision.ProfileCreateRequest(
            name="byurl", no_skills=True, alias=False))
    assert len(owning_gateway.sent) == sent + 1, "the restricted request was replayed or redirected"
    assert owning_gateway.sent[-1].get("defer_default_plugins") is True
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
    assert not _owed(env) and env.rerun.exists()


@pytest.mark.parametrize("source", SOURCES)
def test_the_child_and_the_installer_never_scan_under_default_only_maintenance(env, monkeypatch, source):
    from hermes_cli import default_plugins

    before = _tree(env.sibling)
    with _scope(env, source, monkeypatch):
        assert default_plugins._run_child() == 0
        assert default_plugins.install_and_record(wait=1.0) == {"installed": [], "owed": [], "deferred": True}
        assert default_plugins.spawn_install(env.sibling) is False
    assert env.effects == [] and _tree(env.sibling) == before
    assert _owed(env) and not env.failed.exists() and not env.settled.exists() and not env.rerun.exists()


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
        assert len(_owed(env)) == 1 and env.effects == []
        # A same-scope retry leaves it owed, with nothing started.
        assert boot() == DEFERRED
        assert len(_owed(env)) == 1 and env.effects == []
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
    assert default_plugins.install_and_record(wait=1.0) == {"installed": ["accent-picker"], "owed": []}
    assert installed == [("late", "accent-picker")], "the pass did not honour the existing per-profile choices"
    assert not _owed(env) and env.settled.read_text(encoding="utf-8") == "a" * 40
    assert boot() == {"ok": True} and len(launched) == 1


# ── A7 (Hermes a297425f): a restricted create falls back locally only on the authenticated owner's own refusal ──

FAILURES = ("identity", "protocol", "unreachable", "draining", "retiring", "discovery")


def _request(name):
    from hermes_cli import profile_provision
    return profile_provision.ProfileCreateRequest(name=name, no_skills=True, alias=False)


def _fail(owning_gateway, monkeypatch, kind):
    """The selected local owner is not reached, or answers with something other than "I predate this request"."""
    from hermes_cli import gateway_client, sessions_owner

    def discovery_fails():
        raise OSError("discovery failed")
    if kind == "discovery":
        monkeypatch.setattr(sessions_owner, "live_owner", discovery_fails)
    elif kind == "identity":
        owning_gateway.attach_error = gateway_client.GatewayClientError("Gateway bootstrap identity changed; retry")
    elif kind == "protocol":
        owning_gateway.attach_error = gateway_client.GatewayClientError("Gateway protocol mismatch")
    elif kind == "unreachable":
        owning_gateway.attach_error = OSError("connection refused")
    elif kind == "draining":
        owning_gateway.refusal = gateway_client.GatewayRPCError("runtime_draining", code=4001,
                                                                data={"reason": "runtime_draining"})
    else:
        owning_gateway.refusal = gateway_client.GatewayRPCError("retiring", code=5035)


@pytest.mark.parametrize("kind", FAILURES)
def test_a_restricted_create_never_falls_back_without_the_owners_own_refusal(env, owning_gateway, monkeypatch, kind):
    """A failed discovery, dial, identity or protocol check and a retiring or draining owner are no refusal of the
    request by the authenticated selected owner: nothing is created anywhere and nothing is replayed."""
    from hermes_cli import profile_provision_gateway
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    _fail(owning_gateway, monkeypatch, kind)
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        with pytest.raises(GatewayCreateError, match="nothing was created"):
            profile_provision_gateway.create_through_gateway(_request("held"))
    assert not (env.root / "profiles" / "held").exists() and env.effects == [] and _tree(env.sibling) == before
    assert len(owning_gateway.sent) == (1 if kind in ("draining", "retiring") else 0)


@pytest.mark.parametrize("kind", FAILURES)
def test_an_ordinary_create_keeps_its_local_fallback(env, owning_gateway, monkeypatch, kind):
    """All-profile control: the same outcomes still hand an unrestricted create to this process (``None``)."""
    from hermes_cli import profile_provision_gateway

    _fail(owning_gateway, monkeypatch, kind)
    assert profile_provision_gateway.create_through_gateway(_request("plain")) is None
    assert all("defer_default_plugins" not in sent for sent in owning_gateway.sent)


def test_a_running_dashboards_later_request_takes_normal_policy(env, web, monkeypatch):
    """Item 3: the dashboard's start-up operation ends in its real ``start_server`` before it serves, so a later
    independent request with no pending obligation takes normal policy: the profile is made, the every-home
    installer starts once and nothing is owed (a genuinely pending obligation still defers it: the test above)."""
    from hermes_cli import _launchers, default_plugins, maintenance_scope, web_server

    launched, served = [], []
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, module=None: ["fixture-child", module])
    monkeypatch.setattr(default_plugins, "subprocess", SimpleNamespace(
        DEVNULL=subprocess.DEVNULL, Popen=lambda argv, **kwargs: launched.append(argv)))
    monkeypatch.setattr(web_server, "_start_server_runtime", lambda *a, **k: served.append(
        (maintenance_scope._admitted, getattr(sys, maintenance_scope.RELAUNCH_SCOPE_ATTRIBUTE, None))))
    with _scope(env, "carried", monkeypatch):
        assert default_plugins.installs_deferred()  # the dashboard's start-up operation was restricted
        web_server.start_server()
        assert served == [(None, None)], "the dashboard began serving with its start-up restriction"
        response = web.post("/api/profiles", json={"name": "later", "no_skills": True})
    assert response.status_code == 200, response.text
    assert (env.root / "profiles" / "later" / "config.yaml").is_file()
    assert launched == [["fixture-child", "hermes_cli.default_plugins"]] and not _owed(env)


# ── A7.1 (Hermes 27d159d7): only a refusal that proves the owner predates the request permits a local create ──

def _older_owner_refusal(params, lacking):
    """The error an owner whose ``profiles.create`` contract lacks *lacking* answers: its dispatcher's unknown-key
    refusal (``rpc_dispatch``: ``registry.validate_params`` → ``_err(rid, 4000, problem)``, the key only in the text),
    turned into the exception by the client's own boundary (``gateway_client.rpc_error``)."""
    import dataclasses

    from pydantic import create_model

    from hermes_cli import gateway_client
    from tui_gateway.contracts import registry
    from tui_gateway.contracts.profiles_vault_complete_foreign_subagents import ProfilesCreateParams

    fields = {name: (field.annotation, field) for name, field in ProfilesCreateParams.model_fields.items()
              if name not in lacking}
    older = create_model("ProfilesCreateParams", __config__=ProfilesCreateParams.model_config, **fields)
    _, problem = registry.validate_params(dataclasses.replace(registry.METHODS["profiles.create"], params=older),
                                          params)
    assert problem is not None and any(key in problem for key in lacking)
    refusal = gateway_client.rpc_error({"code": 4000, "message": problem})
    assert (str(refusal), refusal.code, refusal.data) == ("request_failed", 4000, {}), "the client kept the text"
    return refusal


def test_an_owner_without_the_method_lets_the_restricted_create_run_here(env, owning_gateway, monkeypatch):
    """An unknown METHOD (-32601, kept by ``rpc_error``) is the attached owner's proof that it predates the request:
    the create runs in this process, with this process's restriction (deferred, owed)."""
    from hermes_cli import gateway_client
    from tui_gateway.contracts import registry

    owning_gateway.refusal = gateway_client.rpc_error(
        {"code": -32601, "message": registry.unknown_method_message("profiles.create")})
    assert owning_gateway.refusal.unknown_method
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        _cli_create("nomethod")
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == [True]
    _assert_deferred_create(env, "nomethod", before)


def test_an_ordinary_create_still_falls_back_on_an_older_owners_4000(env, owning_gateway):
    """All-profile control: an owner older still (3b421578 lacks clone_config, report and sync_imports) refuses the
    ordinary wire with 4000 and the unrestricted create runs here, as before."""
    from hermes_cli import profile_provision_gateway

    owning_gateway.older = ("clone_config", "report", "sync_imports", "defer_default_plugins")
    assert profile_provision_gateway.create_through_gateway(_request("plain-old")) is None
    assert len(owning_gateway.sent) == 1 and "defer_default_plugins" not in owning_gateway.sent[0]
