"""One bounded negative QA reproduction; only the public OAuth wire seam is replaced."""
import concurrent.futures
import inspect
from pathlib import Path
import threading
from types import SimpleNamespace

from agent import anthropic_credentials as ac
from agent import anthropic_pool_refresh as apr
from agent.credential_pool import CredentialPool
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_cli.auth_commands import auth_remove_command
from hermes_cli.runtime_provider import resolve_runtime_provider
from tests.agent.test_anthropic_selected_source_refresh import MODEL, make_agent, seed_owned

pytest_plugins = ["tests.agent.test_anthropic_selected_source_refresh"]


def test_failed_exchange_cannot_restore_concurrently_removed_sibling(credential_home, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    for obj in (ac, apr, CredentialPool):
        assert Path(inspect.getfile(obj)).resolve().is_relative_to(root), "wrong source imported"
    row = seed_owned(credential_home)
    sibling = dict(row, id="removed-sibling", access_token=row["access_token"] + "-sibling",
                   refresh_token="public-sibling-refresh", priority=1)
    write_credential_pool("anthropic", [row, sibling])
    agent = make_agent(resolve_runtime_provider(requested="anthropic", target_model=MODEL))
    assert agent._credential_pool_entry_id == "selected"
    write_credential_pool("anthropic", [dict(row, expires_at_ms=1), sibling])
    admitted, release = threading.Event(), threading.Event()
    calls = []

    def wire_failure(token, **kwargs):
        calls.append(token)
        admitted.set()
        assert release.wait(10), "controlled wire failure not released"
        raise RuntimeError("public fixture: token endpoint temporarily unavailable")

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", wire_failure)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            refresh = executor.submit(agent._try_refresh_anthropic_client_credentials)
            try:
                assert admitted.wait(10), "did not reach network boundary"
                removal = executor.submit(auth_remove_command, SimpleNamespace(
                    provider="anthropic", target="removed-sibling"))
                removal.result(timeout=5)
                assert [r["id"] for r in read_credential_pool("anthropic")] == ["selected"]
            finally:
                release.set()
            assert refresh.result(timeout=10) is False
        assert calls == [row["refresh_token"]]
        stored = {r["id"]: r for r in read_credential_pool("anthropic")}
        assert stored["selected"]["last_status"] == "exhausted"
        assert "removed-sibling" not in stored, "failed refresh resurrected a credential removed during HTTP"
    finally:
        agent._anthropic_client.close()
