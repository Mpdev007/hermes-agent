"""Selected-source races and native rebinding; public fixtures, no provider I/O."""

import concurrent.futures
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent import anthropic_credentials as ac
from agent.credential_pool import CredentialPool
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_cli.auth_commands import auth_remove_command
from hermes_cli.runtime_provider import resolve_runtime_provider
from tests.agent.test_anthropic_selected_source_refresh import (
    API_KEY, MODEL, OAUTH, make_agent, seed_owned,
)

pytest_plugins = ["tests.agent.test_anthropic_selected_source_refresh"]


def _pair(suffix):
    return {"access_token": OAUTH + suffix, "refresh_token": "public-refresh" + suffix,
            "expires_at_ms": int(time.time() * 1000) + 3_600_000}


def _remove(target="selected"):
    auth_remove_command(SimpleNamespace(provider="anthropic", target=target))


def _expired_agent(home, monkeypatch):
    row = seed_owned(home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    agent = make_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    write_credential_pool("anthropic", [dict(row, expires_at_ms=1)])
    return agent, row


@pytest.mark.parametrize("change", ["remove", "source", "auth_type", "endpoint", "peer"])
def test_native_change_between_preflight_and_refresh_lock(credential_home, monkeypatch, change):
    agent, row = _expired_agent(credential_home, monkeypatch)
    original = CredentialPool.try_refresh_matching
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-exchange")

    def interleave(pool, *args, **kwargs):
        # Preflight has accepted this exact row, but native refresh has not locked it.
        if change == "remove":
            _remove()
        else:
            changes = {
                "source": {"source": "manual:another-account"},
                "auth_type": {"auth_type": "api_key", "access_token": API_KEY},
                "endpoint": {"base_url": "https://other.example.invalid"},
                "peer": _pair("-peer"),
            }
            write_credential_pool("anthropic", [dict(row, **changes[change])])
        return original(pool, *args, **kwargs)

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(CredentialPool, "try_refresh_matching", interleave)
    try:
        changed = agent._try_refresh_anthropic_client_credentials()
        stored = {r["id"]: r for r in read_credential_pool("anthropic")}
        assert calls == []
        if change == "peer":
            assert changed is True
            assert agent.api_key == _pair("-peer")["access_token"]
        else:
            assert changed is False
            assert agent.api_key == OAUTH
            if change == "remove":
                assert "selected" not in stored
            else:
                assert stored["selected"]["access_token"] != _pair("-exchange")["access_token"]
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("change", ["remove", "replace", "peer"])
def test_native_change_during_admitted_exchange_is_authoritative(credential_home, monkeypatch, change):
    agent, row = _expired_agent(credential_home, monkeypatch)
    admitted, release = threading.Event(), threading.Event()
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        admitted.set()
        assert release.wait(15), "test did not release the controlled exchange"
        return _pair("-exchange")

    def change_store():
        if change == "remove":
            _remove()
        else:
            update = _pair("-peer") if change == "peer" else {
                **_pair("-replacement"), "source": "manual:another-account",
            }
            write_credential_pool("anthropic", [dict(row, **update)])

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(agent._try_refresh_anthropic_client_credentials)
            try:
                assert admitted.wait(10), "native refresh did not reach exchange"
                mutation = executor.submit(change_store)
                # Removal uses auth.lock, not a refresh-only lock. It must not wait on HTTP.
                mutation.result(timeout=5)
            finally:
                release.set()
            changed = refresh.result(timeout=10)
        stored = {r["id"]: r for r in read_credential_pool("anthropic")}
        assert calls == [row["refresh_token"]]  # an already-admitted exchange cannot be undone
        if change == "peer":
            assert changed is True
            assert agent.api_key == stored["selected"]["access_token"] == _pair("-peer")["access_token"]
        else:
            assert changed is False
            assert agent.api_key == OAUTH
            if change == "remove":
                assert "selected" not in stored
            else:
                assert stored["selected"]["source"] == "manual:another-account"
                assert stored["selected"]["access_token"] == _pair("-replacement")["access_token"]
    finally:
        agent._anthropic_client.close()


def test_native_remove_after_refresh_commit_before_adoption(credential_home, monkeypatch):
    agent, row = _expired_agent(credential_home, monkeypatch)
    original = CredentialPool.try_refresh_matching
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-exchange")

    def interleave(pool, *args, **kwargs):
        refreshed = original(pool, *args, **kwargs)
        assert refreshed is not None
        _remove()
        return refreshed

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(CredentialPool, "try_refresh_matching", interleave)
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert agent.api_key == OAUTH
        assert calls == [row["refresh_token"]]
        assert not any(r["id"] == "selected" for r in read_credential_pool("anthropic"))
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("source", ["hermes_pkce", "claude_code"])
def test_native_singleton_remove_between_preflight_and_lock(credential_home, monkeypatch, source):
    write = ac._write_hermes_oauth_credentials if source == "hermes_pkce" else ac._write_claude_code_credentials
    write(OAUTH, "public-singleton-refresh", int(time.time() * 1000) + 3_600_000)
    (credential_home / "config.yaml").write_text(json.dumps({"model": {"provider": "anthropic"}}))
    agent = make_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    selected_id = agent._credential_pool_entry_id
    write(OAUTH, "public-singleton-refresh", 1)
    original = CredentialPool.try_refresh_matching
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-exchange")

    def interleave(pool, *args, **kwargs):
        _remove(selected_id)
        return original(pool, *args, **kwargs)

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    monkeypatch.setattr(CredentialPool, "try_refresh_matching", interleave)
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert agent.api_key == OAUTH
        assert calls == []
        assert not any(r["id"] == selected_id for r in read_credential_pool("anthropic"))
    finally:
        agent._anthropic_client.close()


def test_concurrent_native_refreshers_spend_the_grant_once(credential_home, monkeypatch):
    first, row = _expired_agent(credential_home, monkeypatch)
    # Build a second live binding while still valid, then expire the stored row.
    write_credential_pool("anthropic", [row])
    second = make_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    write_credential_pool("anthropic", [dict(row, expires_at_ms=1)])
    admitted, second_ready, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        admitted.set()
        assert release.wait(10)
        return _pair("-exchange")

    def refresh_second():
        second_ready.set()
        return second._try_refresh_anthropic_client_credentials()

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(first._try_refresh_anthropic_client_credentials)
            try:
                assert admitted.wait(10)
                two = executor.submit(refresh_second)
                assert second_ready.wait(10)
            finally:
                release.set()
            assert one.result(timeout=10) is True
            assert two.result(timeout=10) is True
        assert calls == [row["refresh_token"]]
        assert first.api_key == second.api_key == _pair("-exchange")["access_token"]
    finally:
        first._anthropic_client.close()
        second._anthropic_client.close()


def _native_agent(runtime):
    from run_agent import AIAgent

    holder = make_agent(runtime)
    agent = AIAgent.__new__(AIAgent)
    agent.__dict__.update(holder.__dict__)
    agent.base_url = holder.base_url
    agent.context_compressor = None
    agent._client_kwargs = {}
    agent.client = None
    return agent


@pytest.mark.parametrize("rotation", ["peer", "expired"])
def test_refresh_native_same_provider_rebind_then_refresh_again(credential_home, monkeypatch, rotation):
    row = seed_owned(credential_home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    agent = _native_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    pool = agent._credential_pool
    other = next(e for e in pool.entries() if e.id != "selected")
    pool.acquire_lease(other.id)
    cursor, leases = pool._current_id, dict(pool._active_leases)
    calls = []

    def exchange(token, **kwargs):
        calls.append(token)
        return _pair("-first" if token == row["refresh_token"] else "-second")

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    write_credential_pool("anthropic", [dict(row, **_pair("-first")) if rotation == "peer"
                                        else dict(row, expires_at_ms=1)])
    clients = [agent._anthropic_client]
    try:
        assert agent._try_refresh_anthropic_client_credentials() is True
        clients.append(agent._anthropic_client)
        assert agent.api_key == _pair("-first")["access_token"]
        # Execute the native switch including the actual SDK builder and exact-key ID sync.
        agent.switch_model(MODEL, "anthropic", api_mode="anthropic_messages")
        clients.append(agent._anthropic_client)
        assert agent._credential_pool is pool
        assert agent._credential_pool_entry_id == "selected"
        assert pool._current_id == cursor
        assert pool._active_leases == leases
        stored = next(r for r in read_credential_pool("anthropic") if r["id"] == "selected")
        write_credential_pool("anthropic", [dict(stored, expires_at_ms=1)])
        assert agent._try_refresh_anthropic_client_credentials() is True
        assert agent.api_key == _pair("-second")["access_token"]
        assert agent._credential_pool_entry_id == "selected"
        assert calls == ([row["refresh_token"]] if rotation == "expired" else []) + [_pair("-first")["refresh_token"]]
    finally:
        for client in [*clients, agent._anthropic_client]:
            client.close()


@pytest.mark.parametrize("key,expected_id", [(API_KEY, "ambient"), ("public-unknown-key", None)])
def test_intentional_native_rebind_does_not_pin_previous_account(credential_home, monkeypatch, key, expected_id):
    seed_owned(credential_home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    agent = _native_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    ambient = next(e for e in agent._credential_pool.entries() if e.runtime_api_key == API_KEY)
    old_client = agent._anthropic_client
    try:
        agent.switch_model(MODEL, "anthropic", api_key=key, api_mode="anthropic_messages")
        assert agent.api_key == key
        assert agent._credential_pool_entry_id == (ambient.id if expected_id else None)
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert agent.api_key == key
    finally:
        old_client.close()
        agent._anthropic_client.close()


@pytest.mark.parametrize("rotated", [False, True])
def test_selected_env_key_survives_preflight_and_native_rebind(credential_home, monkeypatch, rotated):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    agent = _native_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    selected_id = agent._credential_pool_entry_id
    expected = API_KEY + "-rotated" if rotated else API_KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", expected)
    clients = [agent._anthropic_client]
    try:
        assert agent._try_refresh_anthropic_client_credentials() is rotated
        clients.append(agent._anthropic_client)
        assert agent.api_key == expected
        agent.switch_model(MODEL, "anthropic", api_mode="anthropic_messages")
        clients.append(agent._anthropic_client)
        assert agent._credential_pool_entry_id == selected_id
        assert agent._credential_pool.entry_id_for_api_key(expected) == selected_id
    finally:
        for client in [*clients, agent._anthropic_client]:
            client.close()
