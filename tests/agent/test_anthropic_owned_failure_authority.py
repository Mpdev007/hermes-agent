"""Native pool failure transactions preserve concurrent owner-store changes."""
import concurrent.futures
import threading

import pytest

from agent import anthropic_credentials as ac
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.agent.test_anthropic_bound_refresh_lifecycle import _pair, _remove
from tests.agent.test_anthropic_owned_commit_failure import setup_owned

pytest_plugins = ["tests.agent.test_anthropic_selected_source_refresh"]


@pytest.mark.parametrize("borrowed", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("change", ["sibling_remove", "sibling_peer", "remove", "replace", "peer"])
def test_failed_exchange_respects_current_membership(credential_home, monkeypatch, borrowed, terminal, change):
    agent, row, owner, _profile = setup_owned(credential_home, monkeypatch, borrowed)
    sibling = dict(row, id="sibling", priority=1, **_pair("-sibling"))
    context = set_hermes_home_override(owner.parent)
    try:
        write_credential_pool("anthropic", [sibling])
    finally:
        reset_hermes_home_override(context)
    assert {r["id"] for r in read_credential_pool("anthropic")} == {"selected", "sibling"}
    admitted, release = threading.Event(), threading.Event()
    calls = []

    def fail(token, **kwargs):
        calls.append(token)
        admitted.set()
        assert release.wait(10)
        if terminal:
            raise ac.AnthropicOAuthError(400, "invalid_grant", "public fixture", what="refresh")
        raise RuntimeError("public transient endpoint failure")

    def mutate():
        context = set_hermes_home_override(owner.parent)
        try:
            mutations = {
                "sibling_remove": lambda: _remove("sibling"),
                "remove": _remove,
                "replace": lambda: write_credential_pool("anthropic", [dict(row, source="manual:replacement", **_pair("-replacement"))]),
                "peer": lambda: write_credential_pool("anthropic", [dict(row, **_pair("-peer"))]),
                "sibling_peer": lambda: write_credential_pool("anthropic", [dict(sibling, **_pair("-sibling-peer"))]),
            }
            mutations[change]()
        finally:
            reset_hermes_home_override(context)

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", fail)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(agent._try_refresh_anthropic_client_credentials)
            try:
                assert admitted.wait(10)
                executor.submit(mutate).result(timeout=5)
                after_change = {r["id"]: r for r in read_credential_pool("anthropic")}
            finally:
                release.set()
            assert refresh.result(timeout=10) is (change == "peer")
        stored = {r["id"]: r for r in read_credential_pool("anthropic")}
        assert calls == [row["refresh_token"]]
        if change.startswith("sibling"):
            assert stored.get("sibling") == after_change.get("sibling")
            assert stored["selected"]["last_status"] == ("dead" if terminal else "exhausted")
        else:
            assert stored == after_change
            if change == "peer":
                assert agent.api_key == _pair("-peer")["access_token"]
    finally:
        agent._anthropic_client.close()
