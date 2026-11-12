"""Pool-owned rotation commit failure: real stores, native persistence and verdicts."""
import concurrent.futures
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent import anthropic_credentials as ac
from agent import anthropic_pool_refresh as apr
from agent import credential_pool as cp
from hermes_cli import auth
from hermes_cli.runtime_provider import resolve_runtime_provider
from tests.agent.test_anthropic_bound_refresh_lifecycle import _native_agent, _pair
from tests.agent.test_anthropic_selected_source_refresh import MODEL, OAUTH, seed_owned

pytest_plugins = ["tests.agent.test_anthropic_selected_source_refresh"]


@pytest.fixture(autouse=True)
def clean_verdicts():
    ac._SPENT_ROTATION_FINGERPRINTS.clear()
    yield
    ac._SPENT_ROTATION_FINGERPRINTS.clear()


def setup_owned(home, monkeypatch, borrowed):
    row = seed_owned(home)
    (home / "config.yaml").write_text(json.dumps({"model": {"provider": "anthropic"}}))
    owner = home / "auth.json"
    profile = home
    if borrowed:
        profile = home / "profiles" / "borrower"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(json.dumps({"model": {"provider": "anthropic"}}))
        monkeypatch.setenv("HERMES_HOME", str(profile))
        assert auth._global_auth_file_path() == owner
    agent = _native_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    assert agent._credential_pool_entry_id == "selected"
    assert ("selected" in agent._credential_pool._borrowed_root_ids) is borrowed
    assert apr.owner_path(agent._credential_pool, agent._credential_pool.current()) == owner
    cp.persist_pool_entries("anthropic", [dict(row, expires_at_ms=1)])
    return agent, row, owner, profile


@pytest.mark.parametrize("borrowed", [False, True])
@pytest.mark.parametrize("remove", [False, True])
def test_commit_failure_quarantine_preserves_sibling_changes(credential_home, monkeypatch, borrowed, remove):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tests.agent.test_anthropic_bound_refresh_lifecycle import _remove

    agent, row, owner, _profile = setup_owned(credential_home, monkeypatch, borrowed)
    sibling = dict(row, id="sibling", priority=1, **_pair("-sibling"))
    context = set_hermes_home_override(owner.parent)
    try:
        auth.write_credential_pool("anthropic", [sibling])
    finally:
        reset_hermes_home_override(context)
    calls, failures = [], []
    before = {}
    armed = False
    real_replace = os.replace

    def mutate():
        context = set_hermes_home_override(owner.parent)
        try:
            if remove:
                _remove("sibling")
            else:
                auth.write_credential_pool("anthropic", [dict(sibling, **_pair("-sibling-peer"))])
        finally:
            reset_hermes_home_override(context)

    def exchange(token, **kwargs):
        nonlocal armed
        calls.append(token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(mutate).result(timeout=5)
        before.update({r["id"]: r for r in auth.read_credential_pool("anthropic")})
        armed = True
        return _pair("-exchange")

    def fail(src, dst):
        if armed and not failures and Path(dst) == owner:
            failures.append(str(dst))
            raise OSError("public first auth commit failure after sibling mutation")
        return real_replace(src, dst)

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(os, "replace", fail)
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        after = {r["id"]: r for r in auth.read_credential_pool("anthropic")}
        assert failures and calls == [row["refresh_token"]]
        assert after.get("sibling") == before.get("sibling")
        assert after["selected"]["last_status"] == "dead"
        assert after["selected"]["last_error_reason"] == cp.CREDENTIAL_PERSIST_FAILED_REASON
        assert cp.load_pool("anthropic").try_refresh_matching(credential_id="selected") is None
        assert calls == [row["refresh_token"]]
        assert {r["id"]: r for r in auth.read_credential_pool("anthropic")}.get("sibling") == before.get("sibling")
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("borrowed", [False, True])
def test_unexpired_spent_pair_cannot_be_leased(credential_home, monkeypatch, borrowed):
    agent, row, owner, _profile = setup_owned(credential_home, monkeypatch, borrowed)
    cp.persist_pool_entries("anthropic", [row])
    pool = cp.load_pool("anthropic")
    calls, failures = [], []
    real_replace = os.replace

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-exchange")

    def fail(src, dst):
        if Path(dst) == owner:
            failures.append(str(dst))
            raise OSError("public persistent auth commit failure")
        return real_replace(src, dst)

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(os, "replace", fail)
    try:
        try:
            pool.try_refresh_matching(credential_id="selected")
        except OSError:
            pass  # The rejected implementation propagates local commit failure.
        assert calls == [row["refresh_token"]] and failures
        ac._SPENT_ROTATION_FINGERPRINTS.clear()
        assert ac.refresh_selected_anthropic_token(pool, "selected", model=MODEL) is None
        assert cp.load_pool("anthropic").select() is None
        assert calls == [row["refresh_token"]]
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("borrowed", [False, True], ids=["local-raised", "borrowed-None"])
@pytest.mark.parametrize("persistent", [False, True], ids=["first-write", "all-auth-writes"])
def test_spent_owned_pair_never_replayed_after_failed_commit(credential_home, monkeypatch, borrowed, persistent):
    root = Path(__file__).resolve().parents[2]
    for module in (ac, apr, cp, auth):
        assert Path(inspect.getfile(module)).resolve().is_relative_to(root)
    agent, row, owner, profile = setup_owned(credential_home, monkeypatch, borrowed)
    calls, write_failures, persistence_outcomes = [], [], []
    real_replace, real_persist = os.replace, cp.persist_pool_entries
    armed = True

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-exchange")

    def fail_auth_commit(src, dst):
        if armed and Path(dst) == owner and (persistent or not write_failures):
            write_failures.append(str(dst))
            raise OSError("public injected auth-store atomic commit failure")
        return real_replace(src, dst)

    def observe_persist(*args, **kwargs):
        try:
            result = real_persist(*args, **kwargs)
        except OSError:
            persistence_outcomes.append("raised")
            raise
        persistence_outcomes.append("None" if result is None else "written")
        return result

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(os, "replace", fail_auth_commit)
    monkeypatch.setattr(cp, "persist_pool_entries", observe_persist)
    clients = [agent._anthropic_client]
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert calls == [row["refresh_token"]]
        assert write_failures
        assert ("None" if borrowed else "raised") in persistence_outcomes
        assert agent.api_key == OAUTH
        # Same live binding, then a new pool with the in-memory verdict removed.
        agent._try_refresh_anthropic_client_credentials()
        armed = False
        ac._SPENT_ROTATION_FINGERPRINTS.clear()
        if persistent:
            assert json.loads(owner.read_text())["credential_pool"]["anthropic"][0].get("last_status") != "dead"
            # This child has no inherited in-memory verdict and the auth row was
            # never quarantined. The native durable fingerprint is decisive.
            code = """
from agent import anthropic_credentials as ac
from agent.credential_pool import load_pool
assert not ac._SPENT_ROTATION_FINGERPRINTS
calls = []
def forbidden(*args, **kwargs):
    calls.append(args)
    raise AssertionError('child replayed consumed refresh')
ac.refresh_anthropic_oauth_pure = forbidden
pool = load_pool('anthropic')
assert pool.select() is None
assert pool.try_refresh_matching(credential_id='selected') is None
assert not calls
print('native owner-store consumed verdict refused child lease and POST')
"""
            child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15, check=False)
            assert child.returncode == 0, child.stdout + child.stderr
            assert "refused child lease and POST" in child.stdout
        fresh = cp.load_pool("anthropic")
        fresh.try_refresh_matching(credential_id="selected")
        assert calls == [row["refresh_token"]], "spent pool-owned refresh token was replayed"
        stored = {r["id"]: r for r in auth.read_credential_pool("anthropic")}
        assert stored["selected"]["last_status"] == "dead"
        assert stored["selected"]["last_error_reason"] == cp.CREDENTIAL_PERSIST_FAILED_REASON
        assert stored["selected"]["refresh_token"] == row["refresh_token"]
        assert ac.is_rotation_consumed_uncommitted(row["refresh_token"], source_path=owner)
        if borrowed:
            local = json.loads((profile / "auth.json").read_text()) if (profile / "auth.json").exists() else {}
            assert not local.get("credential_pool", {}).get("anthropic")
            assert not ac._spent_rotation_sidecar_path(profile / "auth.json").exists()
        # A real new generation is not condemned by an old pair's verdict.
        cp.persist_pool_entries("anthropic", [dict(row, **_pair("-peer"))], status_cleared_ids=["selected"])
        assert agent._try_refresh_anthropic_client_credentials() is True
        clients.append(agent._anthropic_client)
        agent.switch_model(MODEL, "anthropic", api_mode="anthropic_messages")
        clients.append(agent._anthropic_client)
        assert agent._credential_pool_entry_id == "selected"
        cp.persist_pool_entries("anthropic", [dict(row, **dict(_pair("-peer"), expires_at_ms=1))])
        assert agent._try_refresh_anthropic_client_credentials() is True
        assert calls == [row["refresh_token"], _pair("-peer")["refresh_token"]]
    finally:
        for client in [*clients, agent._anthropic_client]:
            client.close()
