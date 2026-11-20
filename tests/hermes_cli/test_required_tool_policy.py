"""Same behavioral cases run against the frozen prior candidate and next candidate."""
import asyncio
from dataclasses import replace
import json
import os
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.policy_fixture import discover_guard, protected

READ = "mcp__leann__leann_search"


@pytest.mark.parametrize("state", ["absent", "disabled", "import_failed", "manifest_failed", "missing_declaration"])
def test_missing_required_policy_never_reaches_registry(tmp_path, monkeypatch, state):
    fixture = discover_guard(tmp_path, monkeypatch, state)
    import model_tools
    calls = []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *_a, **_k: calls.append(True) or '{"ok":true}')
    with protected(fixture):
        result = model_tools.handle_function_call(READ, {}, skip_pre_tool_call_hook=True,
                                                  skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    assert calls == []
    assert "required tool policy" in result


def test_real_plugin_none_decision_is_distinguished_from_no_callback(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    import model_tools
    from hermes_cli.tool_dispatch_policy import final_tool_block
    calls = []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda name, args, **_k: calls.append(name) or '{"ok":true}')
    with protected(fixture):
        result = model_tools.handle_function_call(READ, {}, skip_pre_tool_call_hook=True,
                                                  skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
        assert json.loads(result)["ok"]
        fixture.manager._hooks["pre_tool_dispatch"].clear()
        # The declared fingerprint and ownership record still exist. Neither proves delivery.
        assert fixture.manager.policy_evidence("unattended-guard")
        assert final_tool_block(READ, {}) is not None
    assert calls == [READ]


def test_required_delivery_preserves_native_hook_schema_payload(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.middleware import OBSERVER_SCHEMA_VERSION
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.tool_dispatch_policy import final_tool_block
    seen = []

    def policy(*, telemetry_schema_version, **_kwargs):
        seen.append(telemetry_schema_version)

    ctx = PluginContext(fixture.manager._plugins["unattended-guard"].manifest, fixture.manager)
    ctx.register_hook("pre_tool_dispatch", policy)
    with protected(fixture):
        assert final_tool_block(READ, {}) is None
    assert seen == [OBSERVER_SCHEMA_VERSION]


@pytest.mark.parametrize("name", ["terminal", "mcp__leann__leann_build", "write_file"])
def test_real_guard_discovery_denies_forbidden_targets(tmp_path, monkeypatch, name):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    with protected(fixture):
        assert final_tool_block(name, {}) is not None
        assert final_tool_block(READ, {}) is None


def test_actual_inflight_discovery_cannot_look_like_an_allow(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    entered, release = Event(), Event()

    def held_scan():
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(fixture.manager, "_discover_and_load_inner", held_scan)
    worker = Thread(target=lambda: fixture.manager.discover_and_load(force=True), daemon=True)
    worker.start()
    assert entered.wait(timeout=3)
    try:
        with protected(fixture):
            assert final_tool_block(READ, {}) is not None
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()


@pytest.mark.parametrize("safe", [True, False])
def test_synthetic_worker_bypass_cannot_suppress_required_policy(tmp_path, monkeypatch, safe):
    fixture = discover_guard(tmp_path, monkeypatch, "absent")
    from agent import safe_worker_policy
    from hermes_cli.tool_dispatch_policy import final_tool_block
    # Exercise the real safe/config snapshot readers, without starting a worker or
    # binding the process-wide production bootstrap irreversibly in a test process.
    private = SimpleNamespace(pid=os.getpid(), safe_mode=safe, ignore_user_config=True,
                              config_json=json.dumps({"plugins": {"enabled": []}}), source="acp")
    monkeypatch.setattr(safe_worker_policy, "_policy", private)
    from hermes_cli.config import load_config
    assert load_config()["plugins"]["enabled"] == []
    with protected(fixture):
        assert final_tool_block(READ, {}) is not None


def test_direct_plugin_context_dispatch_uses_same_required_check(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.plugins_context import PluginContext
    from tools.registry import registry
    manifest = fixture.manager._plugins["unattended-guard"].manifest
    ctx = PluginContext(manifest, fixture.manager)
    calls = []
    monkeypatch.setattr(registry, "dispatch", lambda name, args, **_k: calls.append(name) or '{"ok":true}')
    with protected(fixture):
        assert "error" in json.loads(ctx.dispatch_tool("terminal", {}))
        assert json.loads(ctx.dispatch_tool(READ, {}))["ok"]
        fixture.manager.unload("unattended-guard")
        assert "error" in json.loads(ctx.dispatch_tool(READ, {}))
    assert calls == [READ]


def test_attended_cli_does_not_require_a_plugin(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch, "absent")
    from hermes_cli.tool_dispatch_policy import final_tool_block
    with protected(fixture, replace(fixture.policy, source="cli", platform="cli")):
        assert final_tool_block("terminal", {}) is None


def test_plugin_dispatch_cannot_change_profile_under_a_protected_context(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_context import PluginContext
    from tools.registry import registry
    foreign = PluginManager(scope_key=str(tmp_path / "other-profile"))
    ctx = PluginContext(fixture.manager._plugins["unattended-guard"].manifest, foreign)
    calls = []
    monkeypatch.setattr(registry, "dispatch", lambda *_a, **_k: calls.append(True) or '{"ok":true}')
    with protected(fixture):
        assert "error" in json.loads(ctx.dispatch_tool(READ, {}))
    assert calls == []


@pytest.mark.parametrize("source", ["acp", "a2a", "api_server", "cron", "oneshot", "webhook"])
def test_each_explicit_protected_source_requires_a_loaded_policy(tmp_path, monkeypatch, source):
    fixture = discover_guard(tmp_path, monkeypatch, "absent")
    from hermes_cli.tool_dispatch_policy import final_tool_block
    with protected(fixture, replace(fixture.policy, source=source, platform="cli" if source == "oneshot" else source)):
        assert final_tool_block(READ, {}) is not None


def test_webhook_delivery_keeps_the_existing_default_allowlist(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    with protected(fixture, replace(fixture.policy, source="webhook", platform="webhook")):
        assert final_tool_block("web_search", {}) is None
        assert final_tool_block("terminal", {}) is not None
        assert final_tool_block(READ, {}) is not None


@pytest.mark.parametrize("context", ["absent", "mismatch"])
def test_protected_worker_context_cannot_be_missing_or_downgraded(tmp_path, monkeypatch, context):
    fixture = discover_guard(tmp_path, monkeypatch)
    from agent import safe_worker_policy
    from gateway import session_policy
    from hermes_cli.tool_dispatch_policy import final_tool_block
    private = SimpleNamespace(pid=os.getpid(), safe_mode=False, ignore_user_config=False,
                              config_json=json.dumps(fixture.config), source="acp")
    monkeypatch.setattr(safe_worker_policy, "_policy", private)
    policy = None if context == "absent" else replace(fixture.policy, source="cli", platform="cli")
    token = session_policy._ACTIVE_POLICY.set(policy)
    try:
        assert final_tool_block("terminal", {}) is not None
    finally:
        session_policy._ACTIVE_POLICY.reset(token)


def test_registered_policy_with_silently_skipped_delivery_is_not_an_allow(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    assert fixture.manager.policy_evidence("unattended-guard")
    monkeypatch.setattr(fixture.manager, "_run_hook_callbacks", lambda *_a, **_k: [])
    with protected(fixture):
        assert final_tool_block(READ, {}) is not None


def test_policy_removed_during_its_decision_does_not_allow_handler(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.tool_dispatch_policy import final_tool_block
    ctx = PluginContext(fixture.manager._plugins["unattended-guard"].manifest, fixture.manager)

    def remove(**_kwargs):
        fixture.manager.unload("unattended-guard")

    ctx.register_hook("pre_tool_dispatch", remove)
    with protected(fixture):
        assert final_tool_block(READ, {}) is not None


def test_legacy_attended_flag_cannot_downgrade_trusted_acp_context(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    import model_tools
    calls = []
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *_a, **_k: calls.append(True) or '{"ok":true}')
    with protected(fixture):
        result = model_tools.handle_function_call("mcp__leann__leann_build", {}, skip_pre_tool_call_hook=True,
                                                  skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    assert calls == []
    assert "error" in json.loads(result)


def test_old_session_uses_current_profile_requirement_without_receipt_rewrite(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    old = json.loads(fixture.policy.config_json)
    old["plugins"].pop("required_tool_policies")
    policy = replace(fixture.policy, config_json=json.dumps(old))
    saved = policy.config_json
    with protected(fixture, policy):
        assert final_tool_block(READ, {}) is None
        fixture.manager.unload("unattended-guard")
        assert final_tool_block(READ, {}) is not None
    assert policy.config_json == saved


def test_old_snapshot_cannot_replace_a_missing_current_requirement(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch, "missing_declaration")
    from hermes_cli.tool_dispatch_policy import final_tool_block
    old = json.loads(fixture.policy.config_json)
    old["plugins"]["required_tool_policies"] = {"acp": ["unattended-guard"]}
    with protected(fixture, replace(fixture.policy, config_json=json.dumps(old))):
        assert final_tool_block(READ, {}) is not None


def test_final_async_timeout_reports_the_correct_policy_phase(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli import plugins
    from hermes_cli.plugins_context import PluginContext
    async def stuck(**_kwargs):
        await asyncio.Event().wait()

    ctx = PluginContext(fixture.manager._plugins["unattended-guard"].manifest, fixture.manager)
    ctx.register_hook("pre_tool_dispatch", stuck)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.05)
    with protected(fixture):
        decisions = asyncio.run(fixture.manager.ainvoke_hook("pre_tool_dispatch", tool_name=READ, args={}))
    blocks = [d["message"] for d in decisions if isinstance(d, dict) and d.get("action") == "block"]
    assert blocks and all("pre_tool_dispatch" in text and "pre_tool_call" not in text for text in blocks)
    assert "pre_tool_dispatch plugin callback timed out or is still running" in blocks


def test_full_unload_marks_discovery_incomplete_and_required_policy_vetoes(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    assert fixture.manager._discovery_complete is True
    fixture.manager.unload()
    assert fixture.manager._discovery_complete is False
    assert fixture.manager.policy_evidence("unattended-guard") == []
    with protected(fixture):
        assert final_tool_block(READ, {}) is not None
