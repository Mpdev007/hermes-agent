"""Bounded lock waiting and distinct direct dispatch IDs preserve fail-closed delivery."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from threading import Event, Lock, Thread

from tests.policy_fixture import discover_guard, protected


def test_short_lived_reader_contention_waits_instead_of_vetoing(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    underlying = fixture.manager._discovery_lock
    entered, attempted = Event(), Event()

    class ObservedLock:
        def acquire(self, *args, **kwargs):
            attempted.set()
            return underlying.acquire(*args, **kwargs)

        def release(self):
            underlying.release()

    def reader():
        with underlying:
            entered.set()
            assert attempted.wait(timeout=3)

    thread = Thread(target=reader, daemon=True)
    thread.start()
    assert entered.wait(timeout=3)
    # Only required-policy acquisition uses this wrapper; callbacks do not
    # register/unload in the fixture while this temporary reader owns the lock.
    monkeypatch.setattr(fixture.manager, "_discovery_lock", ObservedLock())
    try:
        with protected(fixture):
            assert final_tool_block("mcp__leann__leann_search", {}) is None
    finally:
        attempted.set()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_long_lived_reader_exhausts_bounded_wait_and_vetoes(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    entered, release = Event(), Event()

    def reader():
        with fixture.manager._discovery_lock:
            entered.set()
            release.wait(timeout=5)

    thread = Thread(target=reader, daemon=True)
    thread.start()
    assert entered.wait(timeout=3)
    try:
        with protected(fixture), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(copy_context().run, final_tool_block, "mcp__leann__leann_search", {})
            assert "discovery in progress" in result.result(timeout=3)
    finally:
        release.set()
        thread.join(timeout=3)


def test_concurrent_direct_calls_have_distinct_timeout_gate_identities(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli import plugins
    from hermes_cli.plugins_context import PluginContext
    from tools.registry import registry
    manifest = fixture.manager._plugins["unattended-guard"].manifest
    ctx = PluginContext(manifest, fixture.manager)
    first, both, release, lock = Event(), Event(), Event(), Lock()
    seen, dispatched = [], []

    def overlap(*, tool_call_id, **_kwargs):
        with lock:
            seen.append(tool_call_id)
            first.set()
            if len(seen) == 2:
                both.set()
        assert release.wait(timeout=8)

    ctx.register_hook("pre_tool_dispatch", overlap)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 5)
    monkeypatch.setattr(registry, "dispatch", lambda *_a, **_k: dispatched.append(True) or '{"ok":true}')
    with protected(fixture), ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(copy_context().run, ctx.dispatch_tool, "mcp__leann__leann_search", {})
        try:
            assert first.wait(timeout=3)
            b = pool.submit(copy_context().run, ctx.dispatch_tool, "mcp__leann__leann_search", {})
            assert both.wait(timeout=3)
        finally:
            release.set()
        assert json.loads(a.result(timeout=3))["ok"]
        assert json.loads(b.result(timeout=3))["ok"]
    assert len(seen) == 2 and seen[0] != seen[1]
    assert dispatched == [True, True]
