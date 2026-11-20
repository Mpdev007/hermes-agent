"""Observation is gateway-only, declared and bounded; no worker attestation is inferred."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.policy_fixture import discover_guard


def test_declared_policy_observation_excludes_worker_attestation(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from gateway.session_discovery import list_plugins
    authority = SimpleNamespace(profile_id=str(fixture.home))
    actor = SimpleNamespace(profile_id=str(fixture.home), capabilities={"session:read"})
    result = asyncio.run(list_plugins(authority, actor, {"observe_only": True}))
    assert result["policy_observation"]["workers_attested"] is False
    assert result["policy_observation"]["discovery_complete"] is True
    row = next(r for r in result["plugins"] if r["name"] == "unattended-guard")
    assert all(p["policy_fingerprint_kind"] == "plugin_declared" for p in row["policy_evidence"])


def test_retained_rows_after_incomplete_discovery_are_explicitly_unqualified(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from gateway.session_discovery import list_plugins
    fixture.manager._discovery_complete = False
    authority = SimpleNamespace(profile_id=str(fixture.home))
    actor = SimpleNamespace(profile_id=str(fixture.home), capabilities={"session:read"})
    result = asyncio.run(list_plugins(authority, actor, {"observe_only": True}))
    assert any(row["key"] == "unattended-guard" for row in result["plugins"])
    assert result["policy_observation"]["discovery_complete"] is False
    assert result["policy_observation"]["workers_attested"] is False


def test_inflight_discovery_observation_refuses_within_a_bounded_wait(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from gateway.session_discovery import list_plugins
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    authority = SimpleNamespace(profile_id=str(fixture.home))
    actor = SimpleNamespace(profile_id=str(fixture.home), capabilities={"session:read"})
    entered, release = Event(), Event()

    def hold():
        with fixture.manager._discovery_lock:
            entered.set()
            release.wait(timeout=5)

    thread = Thread(target=hold, daemon=True)
    thread.start()
    assert entered.wait(timeout=3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(lambda: asyncio.run(list_plugins(authority, actor, {"observe_only": True})))
        try:
            with pytest.raises(RuntimeStoreError, match="policy_observation_unavailable"):
                read.result(timeout=3)
        finally:
            release.set()
            thread.join(timeout=3)
