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
    root.chmod(0o700)  # A7.2: native discovery runs for real here, and refuses a home another user may write
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


class _ControlPeer:
    """A real control socket for *home* answering ``identify`` with *payload* (the gateway's own
    ``GatewayControlServer``, on an event loop of its own): native discovery reads it exactly as it reads a live
    owner's (pattern: ``tests/hermes_cli/test_gateway_runtime.py``)."""

    def __init__(self, home, payload):
        import asyncio
        import threading
        from gateway.control_socket import GatewayControlServer
        self.home, self.loop = home, asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="control-peer", daemon=True)
        self.server = GatewayControlServer(home, verb_handlers={"identify": lambda: payload})
        self.serving = False

    def start(self):
        import asyncio
        self.thread.start()
        assert asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(timeout=30)
        self.serving = True
        pointer = self.home / "gateway.sock.path"
        if pointer.exists():
            pointer.chmod(0o600)

    def stop(self):
        """Stop answering (the socket and its pointer are removed); idempotent."""
        import asyncio
        if self.serving:
            self.serving = False
            asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(timeout=30)

    def close(self):
        self.stop()
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=30)
        self.loop.close()


@pytest.fixture
def owning_gateway(env, monkeypatch):
    """A live owning gateway for this home, reached in-process: the real ``profiles.create`` receiver runs in a
    fresh context with this process's admission and relaunch handoff hidden, as the gateway process sees them.
    ``older`` names the keys an older gateway lacks: it refuses them as its dispatcher does (``extra="forbid"``:
    4000, the key only in the message text), and the client receives that through ``gateway_client.rpc_error``.

    A7.2 (Hermes 49979351): the owner is FOUND through the real discovery boundary: a control socket on this home
    answers ``identify`` with ``payload`` (ready by default; tests change its state or stop the ``peer``), and
    ``dialed`` records the instance each dial named. Only the WebSocket transport is a double."""
    from hermes_cli import gateway_client, maintenance_scope
    from tui_gateway import server

    identity = str(env.root.resolve())
    payload = {"protocol": 1, "runtime_protocol": 1, "instance_id": "owner-fixture", "authority_epoch": 1,
               "hermes_home": identity, "state": "ready", "api_origin": "http://127.0.0.1:41817",
               "supervisor": "none", "served_profiles": [{"profile_id": identity, "home": identity}],
               "capabilities": ["session-authority-v1"]}
    state = SimpleNamespace(sent=[], dialed=[], older=None, attach_error=None, refusal=None, before_answer=None,
                            payload=payload, peer=_ControlPeer(env.root, payload))

    class Client:
        async def rpc(self, method, _timeout=None, **params):
            state.sent.append(params)
            if state.before_answer is not None:  # A7.2: something that happens while the owner answers
                state.before_answer()
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
        state.dialed.append(getattr(endpoint, "instance_id", None))
        if state.attach_error is not None:  # A7: dial, ticket, identity or protocol check fails before attaching
            raise state.attach_error
        yield Client()
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    state.peer.start()
    try:
        yield state
    finally:
        state.peer.close()


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
    from tui_gateway.contracts.profiles_vault_complete_foreign_subagents import ProfilesCreateParams

    if path == "cli-owning-gateway":  # cli-direct: no control socket or start reservation, so discovery: absent
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
        _cli_create("late")  # no control socket or start reservation: discovery confirms absence
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

ATTACH_FAILURES = ("identity", "protocol", "unreachable", "draining", "retiring")
# A7.2: a restricted create's discovery outcomes are its own test below (the native verdict); an ordinary create
# keeps the ready-or-None helper, so for it a raising ``live_owner`` is still one more local-fallback case.
FAILURES = (*ATTACH_FAILURES, "discovery")


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


@pytest.mark.parametrize("kind", ATTACH_FAILURES)
def test_a_restricted_create_never_falls_back_without_the_owners_own_refusal(env, owning_gateway, monkeypatch, kind):
    """A failed dial, identity or protocol check and a retiring or draining owner are no refusal of the request by
    the authenticated selected owner: nothing is created anywhere and nothing is replayed."""
    from hermes_cli import profile_provision_gateway
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    _fail(owning_gateway, monkeypatch, kind)
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        with pytest.raises(GatewayCreateError, match="nothing was created"):
            profile_provision_gateway.create_through_gateway(_request("held"))
    assert not (env.root / "profiles" / "held").exists() and env.effects == [] and _tree(env.sibling) == before
    assert len(owning_gateway.sent) == (1 if kind in ("draining", "retiring") else 0)
    assert owning_gateway.dialed == ["owner-fixture"], "the request was not dialed to the one discovered owner"


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


# ── A7.2 (Hermes 49979351): a restricted local create keeps the native discovery verdict ──

def _hold_start_reservation(env) -> int:
    """An active start reservation: this home's ``gateway.lock`` held (``flock``) on a descriptor of its own, before
    any control socket is published (``gateway_runtime_discovery.missing_owner_state``: ``starting``)."""
    import fcntl
    import os
    fd = os.open(env.root / "gateway.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


VERDICTS = ("ready", "absent", "starting", "draining", "restart-wait", "control-timeout")


@pytest.mark.parametrize("verdict", VERDICTS)
def test_a_restricted_local_create_follows_the_native_discovery_verdict(env, owning_gateway, monkeypatch, verdict):
    """Through the real discovery boundary: a ready owner gets the one request; confirmed absence lets this process
    create it (deferred and owed); an active start reservation, a draining owner (a restart's after-turn wait
    included) or a control timeout is no answer, so nothing is created anywhere and nothing is dialed."""
    import os

    from gateway.drain_control import RESTART_WAIT_REASON
    from hermes_cli import gateway_runtime_discovery, profile_provision_gateway
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    lock = None
    if verdict in ("absent", "starting"):
        owning_gateway.peer.stop()  # no control socket answers for this home
        if verdict == "starting":
            lock = _hold_start_reservation(env)
    elif verdict == "draining":
        owning_gateway.payload["state"] = "draining"
    elif verdict == "restart-wait":
        owning_gateway.payload.update(state="draining", drain_reason=RESTART_WAIT_REASON)
    elif verdict == "control-timeout":
        def timed_out(home, *, timeout):
            raise TimeoutError  # what the control-socket read raises past its deadline
        monkeypatch.setattr(gateway_runtime_discovery, "query_identify", timed_out)
    before = _tree(env.sibling)
    try:
        with _scope(env, "carried", monkeypatch):
            if verdict in ("ready", "absent"):
                _cli_create("seen")
            else:
                with pytest.raises(GatewayCreateError, match="nothing was created under default-only maintenance"):
                    profile_provision_gateway.create_through_gateway(_request("seen"))
    finally:
        if lock is not None:
            os.close(lock)
    if verdict in ("ready", "absent"):
        _assert_deferred_create(env, "seen", before)
        assert owning_gateway.dialed == (["owner-fixture"] if verdict == "ready" else [])
        assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == (
            [True] if verdict == "ready" else [])
    else:
        assert not (env.root / "profiles" / "seen").exists() and env.effects == [] and _tree(env.sibling) == before
        assert owning_gateway.dialed == [] and owning_gateway.sent == []


def test_an_ordinary_create_keeps_the_ready_or_none_helper(env, owning_gateway):
    """All-profile control: the same draining owner is still "no owner" for an unrestricted create, which runs here
    as before (``sessions_owner.live_owner`` and its other callers are unchanged)."""
    from hermes_cli import profile_provision_gateway

    owning_gateway.payload["state"] = "draining"
    assert profile_provision_gateway.create_through_gateway(_request("plain-draining")) is None
    assert owning_gateway.dialed == [] and owning_gateway.sent == []


def _unknown_method(code):
    from hermes_cli import gateway_client
    from tui_gateway.contracts import registry
    if code is None:  # an owner that sends only the bounded reason
        return gateway_client.rpc_error({"message": "unknown_method"})
    return gateway_client.rpc_error({"code": code, "message": registry.unknown_method_message("profiles.create")})


@pytest.mark.parametrize("refusal, matched", [
    ("unknown-method", True),           # -32601 to this very request: the owner has no profiles.create
    ("unknown-method-reason-only", False),  # no native owner sends the reason without -32601: unprovable
    ("unknown-key-4000", False),        # the real older-owner frame: which key was refused is unprovable
    ("invalid-params-4001", False),
    ("handler-failure-5062", False),
])
def test_only_a_matched_pre_dispatch_rejection_permits_the_restricted_local_create(env, owning_gateway, monkeypatch,
                                                                                   refusal, matched):
    """The attached, discovered owner's own answer to this one request: only a rejection of the method itself proves
    it predates the request. Anything else leaves nothing created and nothing replayed or redirected."""
    from hermes_cli import gateway_client
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    if refusal == "unknown-key-4000":
        owning_gateway.older = ("defer_default_plugins",)
    else:
        owning_gateway.refusal = {
            "unknown-method": lambda: _unknown_method(-32601),
            "unknown-method-reason-only": lambda: _unknown_method(None),
            "invalid-params-4001": lambda: gateway_client.GatewayRPCError("invalid_params", code=4001,
                                                                          data={"reason": "invalid_params"}),
            "handler-failure-5062": lambda: gateway_client.GatewayRPCError("handler_failed", code=5062),
        }[refusal]()
    before = _tree(env.sibling)
    with _scope(env, "carried", monkeypatch):
        if matched:
            _cli_create("matched")
        else:
            from hermes_cli import profile_provision_gateway
            with pytest.raises(GatewayCreateError):
                profile_provision_gateway.create_through_gateway(_request("matched"))
    assert owning_gateway.dialed == ["owner-fixture"] and len(owning_gateway.sent) == 1
    assert owning_gateway.sent[0].get("defer_default_plugins") is True
    if matched:
        _assert_deferred_create(env, "matched", before)
    else:
        assert not (env.root / "profiles" / "matched").exists() and env.effects == [] and _tree(env.sibling) == before


def test_the_captured_restriction_governs_the_local_create_after_the_marker_clears(env, owning_gateway, monkeypatch):
    """The create is restricted by a pending obligation, read once before dispatch. While the owner answers (it has
    no ``profiles.create``: the matched refusal), the obligation ends and its marker is cleared. The local create and
    its installer decision still defer: the captured answer is carried, not re-read."""
    from hermes_cli.venv_sync import clear_completion

    owning_gateway.refusal = _unknown_method(-32601)
    owning_gateway.before_answer = lambda: clear_completion(env.checkout)
    before = _tree(env.sibling)
    with _scope(env, "pending", monkeypatch):
        _cli_create("roundtrip")
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == [True]
    _assert_deferred_create(env, "roundtrip", before)  # red on A7.1: the local installer re-read the cleared marker


# ── A7.3 (Hermes fa0015f0): a named profile's shared owner is an owner too; a restriction seen anywhere is kept ──

SHARED = ("shared-timeout", "shared-starting", "shared-malformed", "shared-parked",
          "both-absent", "standalone", "shared-unserved", "shared-serves")


@pytest.mark.parametrize("case", SHARED)
def test_a_restricted_named_create_keeps_the_shared_owners_uncertainty(env, owning_gateway, monkeypatch, case):
    """From the named profile ``sibling`` (no socket of its own, its lock free, so discovery reads the direct owner
    absent), the root's shared gateway is the other possible owner. A timeout, an active start reservation, a reply
    that is not a well-formed "not served", or a parked entry is no verdict: nothing is created or dialed. A proven
    standalone profile, both owners confirmed absent, or a valid reply that does not list this home lets the create
    run here; a shared owner that serves it gets the request."""
    import os

    from hermes_cli import gateway_runtime_discovery, profile_provision_gateway
    from hermes_cli.maintenance_scope import deferring_every_home_installs
    from hermes_cli.profile_provision_gateway import GatewayCreateError

    env.sibling.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(env.sibling))
    sibling = str(env.sibling.resolve())
    lock = None
    if case in ("shared-timeout", "standalone"):
        real = gateway_runtime_discovery.query_identify

        def root_times_out(home, *, timeout):
            if Path(home).resolve() == env.root.resolve():
                raise TimeoutError  # the shared owner's control read past its deadline
            return real(home, timeout=timeout)
        monkeypatch.setattr(gateway_runtime_discovery, "query_identify", root_times_out)
        if case == "standalone":
            (env.sibling / "config.yaml").write_text("config_version: 1\ngateway:\n  standalone: true\n")
    elif case in ("shared-starting", "both-absent"):
        owning_gateway.peer.stop()  # the root's socket is gone too
        if case == "shared-starting":
            lock = _hold_start_reservation(env)  # the ROOT's start reservation; the named lock stays absent
    elif case == "shared-malformed":
        owning_gateway.payload["served_profiles"] = "not-a-list"
    elif case == "shared-parked":
        owning_gateway.payload["parked_profiles"] = {"sibling": "unusable store"}
    elif case == "shared-serves":
        from hermes_cli import gateway_client
        owning_gateway.payload["served_profiles"] = [
            *owning_gateway.payload["served_profiles"], {"profile_id": sibling, "home": sibling}]
        # The shared owner's answer is not under test: a handler failure proves the one request reached it.
        owning_gateway.refusal = gateway_client.GatewayRPCError("handler_failed", code=5062)
    try:
        with deferring_every_home_installs(True):  # restricted (the owning gateway's carrier, A5)
            if case in ("shared-timeout", "shared-starting", "shared-malformed", "shared-parked"):
                with pytest.raises(GatewayCreateError, match="nothing was created under default-only maintenance"):
                    profile_provision_gateway.create_through_gateway(_request("named"))
            elif case == "shared-serves":
                with pytest.raises(GatewayCreateError, match="did not finish the create"):
                    profile_provision_gateway.create_through_gateway(_request("named"))
            else:
                assert profile_provision_gateway.create_through_gateway(_request("named")) is None  # created here
    finally:
        if lock is not None:
            os.close(lock)
    assert not (env.root / "profiles" / "named").exists()
    assert owning_gateway.dialed == (["owner-fixture"] if case == "shared-serves" else [])
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == (
        [True] if case == "shared-serves" else [])
    assert env.effects == []


# ── A7.4 (Hermes 8c2c45fd finding 1): "does not serve it" needs a valid ready root and a complete served list ──

ROOT_REPLIES = ("starting-empty", "draining-root-only", "incomplete-row", "duplicate-unrelated", "no-authority-epoch",
                "no-session-capability", "ready-unserved", "ready-serves")


@pytest.mark.parametrize("case", ROOT_REPLIES)
def test_the_cli_named_create_needs_a_valid_ready_root_before_an_omission_counts(env, owning_gateway, monkeypatch,
                                                                                 case):
    """``hermes profile create`` from the named profile ``sibling`` under default-only maintenance (the normal CLI
    entry). Its own owner is absent, so the root's shared gateway answers for it, over the real control socket. An
    omitted target is "not served" only when that reply is a ready root that passes the native endpoint check for
    its own row and lists a complete, unambiguous set of homes. A root starting with no rows, or draining with only
    its own row (both as the native identify producer emits them), an incomplete or a duplicated unrelated row, or a
    root without its authority epoch or session capability leaves the profile uncreated: no provision, no installer
    effect, no create RPC (red on A7.3: each was read as "not served" and provisioned here). Positive controls: a
    valid ready root that omits the target lets the create run here; one serving it exactly once gets the request."""
    from hermes_cli import gateway_client, profile_provision

    class Provisioned(Exception):
        """The local create was reached (the provision itself is not under test here)."""

    provisions: list[str] = []

    def provision(request):
        provisions.append(request.name)
        raise Provisioned
    monkeypatch.setattr(profile_provision, "provision_profile", provision)
    env.sibling.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(env.sibling))
    payload = owning_gateway.payload
    root_row = dict(payload["served_profiles"][0])
    sibling, other = str(env.sibling.resolve()), str((env.root / "profiles" / "other").resolve())
    if case == "starting-empty":
        payload.update(state="starting", served_profiles=[], capabilities=[])  # gateway/run_runtime.py:196-199
    elif case == "draining-root-only":
        payload.update(state="draining")  # gateway/run_bootstrap.py:302-303
    elif case == "incomplete-row":
        payload["served_profiles"] = [root_row, {"home": other}]
    elif case == "duplicate-unrelated":
        payload["served_profiles"] = [root_row, {"profile_id": other, "home": other},
                                      {"profile_id": other, "home": other}]
    elif case == "no-authority-epoch":
        del payload["authority_epoch"]
    elif case == "no-session-capability":
        payload["capabilities"] = []
    elif case == "ready-serves":
        payload["served_profiles"] = [root_row, {"profile_id": sibling, "home": sibling}]
        # The shared owner's answer is not under test: a handler failure proves the one request reached it.
        owning_gateway.refusal = gateway_client.GatewayRPCError("handler_failed", code=5062)
    with _scope(env, "explicit", monkeypatch):
        with pytest.raises(Provisioned if case == "ready-unserved" else SystemExit):
            _cli_create("named")
    assert provisions == (["named"] if case == "ready-unserved" else [])
    assert owning_gateway.dialed == (["owner-fixture"] if case == "ready-serves" else [])
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == (
        [True] if case == "ready-serves" else [])
    assert env.effects == []
    assert not (env.root / "profiles" / "named").exists()


def _restricted_from_the_dispatch_reading(env, monkeypatch):
    """The caller's reading before dispatch is ordinary; the pending default-only obligation is armed just before the
    dispatcher reads again (a second reading of the same operation sees it)."""
    from hermes_cli import default_plugins
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion

    real, calls = default_plugins.installs_deferred, []

    def readings():
        calls.append(len(calls))
        if len(calls) == 2:
            arm_completion(env.checkout, maintenance_scope=MaintenanceScope.select("default"))
        return real()
    monkeypatch.setattr(default_plugins, "installs_deferred", readings)
    return calls


@pytest.mark.parametrize("route", ["matched-refusal", "confirmed-absence"])
def test_a_restriction_seen_only_by_the_dispatcher_governs_the_local_create(env, owning_gateway, monkeypatch, route):
    """Late deferral: only the dispatcher's reading is restricted, and the obligation ends (its marker withdrawn)
    before the local create. The create still defers and records the request owed: the dispatcher's own reading is
    carried through ``provision_profile`` and its installer decision (red on A7.2: the CLI's ordinary reading was
    carried and the installer re-read the cleared marker)."""
    from hermes_cli import profile_provision_gateway
    from hermes_cli.venv_sync import clear_completion

    calls = _restricted_from_the_dispatch_reading(env, monkeypatch)
    if route == "matched-refusal":
        owning_gateway.refusal = _unknown_method(-32601)
        owning_gateway.before_answer = lambda: clear_completion(env.checkout)
    else:
        owning_gateway.peer.stop()
        real_owner = profile_provision_gateway._restricted_local_owner

        def absent_then_withdrawn():
            owner = real_owner()
            clear_completion(env.checkout)  # the obligation ends before the local create
            return owner
        monkeypatch.setattr(profile_provision_gateway, "_restricted_local_owner", absent_then_withdrawn)
    before = _tree(env.sibling)
    _cli_create("late")
    assert len(calls) >= 2
    assert [sent.get("defer_default_plugins") for sent in owning_gateway.sent] == (
        [True] if route == "matched-refusal" else [])
    _assert_deferred_create(env, "late", before)
