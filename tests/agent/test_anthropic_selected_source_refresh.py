"""Anthropic preflight must renew the selected source, not re-run discovery.

All credentials are public synthetic fixtures. The selector, pool persistence,
client lifecycle and SDK client construction execute without inference/HTTP.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_init_fallback import _init_fallback_chain
from agent.client_lifecycle import ClientLifecycleMixin
from hermes_cli.runtime_provider import resolve_runtime_provider


OAUTH = "sk-ant-" + "oat01-public-selected-fixture"
API_KEY = "public-ambient-api-key-fixture"
MODEL = "claude-opus-5-5"


@pytest.fixture
def credential_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-test"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def seed_owned(home, token=OAUTH, *, auth_type="oauth", source="manual:hermes_pkce"):
    row = {
        "id": "selected", "source": source, "auth_type": auth_type,
        "access_token": token, "refresh_token": "public-selected-refresh-fixture",
        "expires_at_ms": int(time.time() * 1000) + 3_600_000, "priority": 0,
    }
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [row]}}))
    return row


def make_agent(runtime):
    agent = ClientLifecycleMixin()
    agent.provider = runtime["provider"]
    agent.api_mode = runtime["api_mode"]
    agent.api_key = runtime["api_key"]
    agent.base_url = runtime["base_url"]
    agent.model = MODEL
    agent.quiet_mode = True
    agent._credential_pool = runtime.get("credential_pool")
    _init_fallback_chain(agent, None)
    agent._anthropic_api_key = agent.api_key
    agent._anthropic_base_url = agent.base_url
    agent._is_anthropic_oauth = agent._anthropic_oauth_flag(agent.api_key)
    agent._anthropic_client = agent._build_direct_anthropic_client(agent.api_key, agent.base_url)
    agent.context_compressor = SimpleNamespace(api_key=agent.api_key)
    agent._primary_runtime = {
        "api_key": agent.api_key, "anthropic_api_key": agent.api_key,
        "compressor_api_key": agent.api_key, "is_anthropic_oauth": agent._is_anthropic_oauth,
    }
    return agent


@pytest.mark.parametrize(
    "selected,auth_type,source,ambient_name,ambient",
    [(OAUTH, "oauth", "manual:hermes_pkce", "ANTHROPIC_API_KEY", API_KEY),
     (API_KEY, "api_key", "manual", "CLAUDE_CODE_OAUTH_TOKEN", OAUTH)],
)
def test_preflight_preserves_selected_source(
    credential_home, monkeypatch, selected, auth_type, source, ambient_name, ambient,
):
    seed_owned(credential_home, selected, auth_type=auth_type, source=source)
    monkeypatch.setenv(ambient_name, ambient)
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODEL)
    assert runtime["source"] == source
    assert runtime["api_key"] == selected
    agent = make_agent(runtime)
    old_client = agent._anthropic_client
    try:
        changed = agent._try_refresh_anthropic_client_credentials()
        assert agent.api_key == selected
        assert changed is False
        assert agent._anthropic_client is old_client
        assert not old_client.is_closed()
        assert agent._is_anthropic_oauth == (auth_type == "oauth")
        assert agent.context_compressor.api_key == selected
        assert agent._primary_runtime["anthropic_api_key"] == selected
        assert agent._credential_pool_entry_id == "selected"
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("rotation", ["peer", "expired"])
def test_same_source_oauth_rotation_updates_all_holders(credential_home, monkeypatch, rotation):
    from agent import anthropic_credentials as ac
    from agent.auxiliary_client import _RUNTIME_MAIN_CONTEXT
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    row = seed_owned(credential_home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODEL)
    agent = make_agent(runtime)
    old_client = agent._anthropic_client
    # A shared pool's cursor is not the live client's identity.
    other = next(entry for entry in agent._credential_pool.entries() if entry.id != "selected")
    agent._credential_pool.acquire_lease(other.id)
    new_token = OAUTH + "-rotated"
    refreshed_pair = {
        "access_token": new_token, "refresh_token": "public-rotated-refresh-fixture",
        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
    }
    calls = []

    def exchange(refresh_token, **kwargs):
        calls.append(refresh_token)
        assert refresh_token == row["refresh_token"]
        assert kwargs == {"use_json": True}
        return refreshed_pair

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    updated = dict(row, **refreshed_pair) if rotation == "peer" else dict(row, expires_at_ms=1)
    unrelated = dict(row, id="unrelated", priority=3, access_token=OAUTH + "-unrelated",
                     refresh_token="public-unrelated-refresh-fixture", expires_at_ms=1)
    write_credential_pool("anthropic", [updated, unrelated])
    published = {"api_key": OAUTH}
    context_token = _RUNTIME_MAIN_CONTEXT.set(published)
    try:
        assert agent._try_refresh_anthropic_client_credentials() is True
        assert agent.api_key == new_token
        assert agent._anthropic_api_key == new_token
        assert agent._is_anthropic_oauth
        assert old_client.is_closed()
        assert agent._anthropic_client is not old_client
        assert agent._anthropic_client.auth_token == new_token
        assert not agent._anthropic_client.api_key
        assert agent.context_compressor.api_key == new_token
        assert published["api_key"] == new_token
        assert agent._primary_runtime == {
            "api_key": new_token, "anthropic_api_key": new_token,
            "compressor_api_key": new_token, "is_anthropic_oauth": True,
        }
        persisted = {entry["id"]: entry for entry in read_credential_pool("anthropic")}
        assert persisted["selected"]["access_token"] == new_token
        assert persisted["selected"]["refresh_token"] == refreshed_pair["refresh_token"]
        assert persisted["unrelated"]["access_token"] == unrelated["access_token"]
        assert agent._credential_pool_entry_id == "selected"
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert calls == ([row["refresh_token"]] if rotation == "expired" else [])
    finally:
        _RUNTIME_MAIN_CONTEXT.reset(context_token)
        agent._anthropic_client.close()


@pytest.mark.parametrize("change", ["no_pool", "no_id", "unknown_id", "removed", "source", "auth_type", "endpoint", "cooldown"])
def test_missing_or_changed_binding_never_discovers_another_source(credential_home, monkeypatch, change):
    from hermes_cli.auth import write_credential_pool

    row = seed_owned(credential_home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    agent = make_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    old_client = agent._anthropic_client
    binding_changes = {
        "no_pool": {"_credential_pool": None},
        "no_id": {"_credential_pool_entry_id": None},
        "unknown_id": {"_credential_pool_entry_id": "missing-id"},
    }
    if change in binding_changes:
        for attribute, value in binding_changes[change].items():
            setattr(agent, attribute, value)
    elif change == "removed":
        write_credential_pool("anthropic", [], removed_ids=["selected"])
    else:
        changes = {
            "source": {"source": "manual:other-account"},
            "auth_type": {"auth_type": "api_key", "access_token": API_KEY},
            "endpoint": {"base_url": "https://other.example.invalid/anthropic"},
            "cooldown": {"last_status": "exhausted", "cooldown_until": time.time() + 300},
        }
        write_credential_pool("anthropic", [dict(row, **changes[change])])
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert agent.api_key == OAUTH
        assert agent.context_compressor.api_key == OAUTH
        assert agent._primary_runtime["api_key"] == OAUTH
        assert agent._anthropic_client is old_client
        assert not old_client.is_closed()
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("source", ["hermes_pkce", "claude_code"])
def test_bound_singleton_oauth_can_still_renew(credential_home, monkeypatch, source):
    from agent import anthropic_credentials as ac

    write = ac._write_hermes_oauth_credentials if source == "hermes_pkce" else ac._write_claude_code_credentials
    read = ac.read_hermes_oauth_credentials if source == "hermes_pkce" else ac.read_claude_code_credentials
    refresh_token = "public-singleton-refresh-fixture"
    write(OAUTH, refresh_token, int(time.time() * 1000) + 3_600_000)
    (credential_home / "config.yaml").write_text(json.dumps({"model": {"provider": "anthropic"}}))
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODEL)
    assert runtime["source"] == source
    agent = make_agent(runtime)
    write(OAUTH, refresh_token, 1)
    calls = []

    def exchange(value, **kwargs):
        calls.append((value, kwargs))
        return {"access_token": OAUTH + "-renewed", "refresh_token": refresh_token + "-new",
                "expires_at_ms": int(time.time() * 1000) + 3_600_000}

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", exchange)
    try:
        assert agent._try_refresh_anthropic_client_credentials() is True
        assert agent.api_key == OAUTH + "-renewed"
        assert read()["accessToken"] == agent.api_key
        assert calls == [(refresh_token, {"use_json": source == "hermes_pkce"})]
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("route", ["explicit", "foreign", "key_cmd"])
def test_unpooled_static_or_command_credentials_remain_authoritative(credential_home, monkeypatch, route):
    seed_owned(credential_home)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    kwargs = {"requested": "anthropic", "target_model": MODEL}
    if route == "key_cmd":
        (credential_home / "config.yaml").write_text(json.dumps({
            "providers": {"command": {"base_url": "https://api.anthropic.com",
                "api_mode": "anthropic_messages", "key_cmd": "printf public-command-fixture"}},
        }))
        kwargs["requested"] = "custom:command"
    else:
        kwargs["explicit_api_key"] = "public-explicit-fixture"
        if route == "foreign":
            kwargs["explicit_base_url"] = "https://foreign.example.invalid/anthropic"
    runtime = resolve_runtime_provider(**kwargs)
    selected = runtime["api_key"]
    if route == "key_cmd":
        assert callable(selected)
        assert selected() == "public-command-fixture"
    else:
        assert selected == "public-explicit-fixture"
    assert runtime.get("credential_pool") is None
    agent = make_agent(runtime)
    old_client = agent._anthropic_client
    try:
        assert agent._try_refresh_anthropic_client_credentials() is False
        assert agent.api_key is selected
        assert agent._anthropic_client is old_client
        assert not old_client.is_closed()
    finally:
        agent._anthropic_client.close()
