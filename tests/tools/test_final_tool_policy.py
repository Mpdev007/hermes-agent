"""Final policy must see the actual target after admission and middleware rewrites."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


READ = "mcp__fixture__read"
BUILD = "mcp__fixture__build"


@pytest.fixture
def dispatch(monkeypatch):
    import model_tools
    from hermes_cli import lifecycle, plugins
    from tools import tool_search

    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
    monkeypatch.setattr(lifecycle, "_observe", lambda *_a, **_k: None)
    names = (READ, BUILD)
    defs = [{"type": "function", "function": {"name": name, "description": name,
             "parameters": {"type": "object", "properties": {}}}} for name in names]
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_k: defs)
    monkeypatch.setattr(tool_search, "scoped_deferrable_names", lambda *_a: set(names))
    monkeypatch.setattr(tool_search, "session_unavailable_error", lambda *_a: None)
    monkeypatch.setattr(tool_search, "is_deferrable_tool_name", lambda *_a: True)
    monkeypatch.setattr(tool_search, "validate_deferred_call_args", lambda *_a: None)
    calls = []
    monkeypatch.setattr(model_tools.registry, "dispatch",
                        lambda name, args, **_k: calls.append((name, args)) or '{"ok":true}')

    def final_policy(tool_name, args=None, **_k):
        target = args.get("name") if tool_name == "tool_call" else tool_name
        if target not in (READ, "tool_describe", "tool_search"):
            return {"action": "block", "message": "fixture policy denied final target"}

    manager._hooks["pre_tool_dispatch"] = [final_policy]
    return SimpleNamespace(manager=manager, model=model_tools, calls=calls, policy=final_policy)


@pytest.mark.parametrize("guard_first", [True, False])
def test_agent_hook_rewrite_cannot_select_unlisted_deferred_handler(dispatch, monkeypatch, guard_first):
    from agent import tool_executor as executor

    def admission(tool_name, args, **_k):
        if args.get("name") != READ:
            return {"action": "block", "message": "admission denied"}

    def modifier(**_k):
        return {"action": "modify", "args": {"name": BUILD}}

    dispatch.manager._hooks["pre_tool_call"] = ([admission, modifier] if guard_first else [modifier, admission])
    monkeypatch.setattr(executor, "_begin_tool_execution", lambda *_a: None)
    monkeypatch.setattr(executor, "_run_with_activity_heartbeat", lambda _a, _n, fn: fn())
    agent = SimpleNamespace(session_id="fixture", tool_progress_callback=None,
                            _tool_guardrails=SimpleNamespace(before_call=lambda *_a: SimpleNamespace(allows_execution=True)))
    result = executor._run_agent_tool_execution_middleware(
        agent, function_name="tool_call", function_args={"name": READ, "arguments": {}},
        effective_task_id="fixture", tool_call_id="fixture",
        execute=lambda args: dispatch.model.handle_function_call(
            "tool_call", args, skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True, skip_tool_execution_middleware=True))
    assert dispatch.calls == []
    assert "denied final target" in str(result.result)


@pytest.mark.parametrize("skip_pre", [False, True])
def test_execution_middleware_rewrite_is_checked_after_unwrap(dispatch, monkeypatch, skip_pre):
    from hermes_cli import middleware

    def rewrite(name, args, next_call, **_k):
        return next_call({**args, "operation": "mutate"})

    def policy(tool_name, args, **_k):
        if args.get("operation") == "mutate":
            return {"action": "block", "message": "final arguments denied"}

    dispatch.manager._hooks["pre_tool_dispatch"] = [policy]
    monkeypatch.setattr(middleware, "run_tool_execution_middleware", rewrite)
    result = dispatch.model.handle_function_call(
        "tool_call", {"name": READ, "arguments": {}}, skip_pre_tool_call_hook=skip_pre)
    assert dispatch.calls == []
    assert "final arguments denied" in result


def test_allowed_single_mcp_dispatches_once_and_multi_mcp_still_refuses(dispatch):
    assert json.loads(dispatch.model.handle_function_call(
        "tool_call", {"name": READ, "arguments": {}}))["ok"]
    assert dispatch.calls == [(READ, {})]
    dispatch.calls.clear()
    result = dispatch.model.handle_function_call("tool_call", {"calls": [
        {"name": READ, "arguments": {}}, {"name": READ, "arguments": {}}]})
    assert "exactly one" in result
    assert dispatch.calls == []


def test_concurrent_calls_keep_forbidden_handler_count_zero(dispatch):
    def call(name):
        return dispatch.model.handle_function_call(
            "tool_call", {"name": name, "arguments": {}}, skip_pre_tool_call_hook=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call, [READ, BUILD, READ, BUILD]))
    assert [name for name, _ in dispatch.calls] == [READ, READ]
    assert sum("denied final target" in r for r in results) == 2


@pytest.mark.parametrize("caller", ["agent", "helper", "model"])
def test_pre_hook_infrastructure_failure_never_reaches_handler(dispatch, monkeypatch, caller):
    from agent import inline_tool_executors, tool_executor
    from hermes_cli import plugins

    def broken(*_a, **_k):
        raise RuntimeError("fixture delivery unavailable")

    monkeypatch.setattr(plugins, "_dispatch_pre_tool_call_hooks", broken)
    if caller == "agent":
        ref = SimpleNamespace(name=READ, args={}, task_id="fixture", call_id="fixture", trace=[])
        block, _ = tool_executor._pre_tool_block(SimpleNamespace(), ref)
    elif caller == "helper":
        block, _ = inline_tool_executors._pre_tool_block_message(SimpleNamespace(), READ, {}, "fixture", "fixture", [])
    else:
        result = dispatch.model.handle_function_call(READ, {})
        block = json.loads(result).get("error")
    assert block
    assert dispatch.calls == []


def test_final_hook_delivery_failure_never_reaches_handler(dispatch, monkeypatch):
    from hermes_cli import lifecycle
    original = lifecycle.invoke_hook

    def broken(name, **kwargs):
        if name == "pre_tool_dispatch":
            raise RuntimeError("fixture final delivery unavailable")
        return original(name, **kwargs)

    monkeypatch.setattr(lifecycle, "invoke_hook", broken)
    result = dispatch.model.handle_function_call(READ, {}, skip_pre_tool_call_hook=True)
    assert dispatch.calls == []
    assert "policy delivery" in result


@pytest.mark.parametrize("decision", ["modify", "approve", "invalid", "callback_error", "mutate_input"])
def test_final_validators_cannot_rewrite_approve_or_fail_open(dispatch, decision):
    def invalid(args, **_kwargs):
        if decision == "callback_error":
            raise RuntimeError("fixture callback failure")
        if decision == "mutate_input":
            args["operation"] = "mutate"
            return None
        if decision == "invalid":
            return "allow"
        return {"action": decision, "args": {"operation": "mutate"}}

    dispatch.manager._hooks["pre_tool_dispatch"] = [invalid]
    original = {"operation": "read"}
    result = dispatch.model.handle_function_call(READ, original, skip_pre_tool_call_hook=True)
    assert dispatch.calls == []
    assert original == {"operation": "read"}
    assert "error" in json.loads(result)


def test_inline_executor_is_checked_after_execution_middleware(dispatch, monkeypatch):
    from agent import agent_runtime_helpers, inline_tool_executors
    from hermes_cli import middleware

    calls = []
    monkeypatch.setattr(inline_tool_executors, "resolve_invoke_tool_executor",
                        lambda *_a: lambda *_args: calls.append("inline handler"))
    monkeypatch.setattr(middleware, "run_tool_execution_middleware",
                        lambda _name, args, next_call, **_k: next_call({**args, "operation": "mutate"}))

    def policy(args, **_kwargs):
        if args.get("operation") == "mutate":
            return {"action": "block", "message": "inline final veto"}

    dispatch.manager._hooks["pre_tool_dispatch"] = [policy]
    result = agent_runtime_helpers.invoke_tool(SimpleNamespace(session_id="fixture"), READ, {}, "fixture",
                                               pre_tool_block_checked=True, skip_tool_request_middleware=True)
    assert "inline final veto" in result
    assert calls == []


def test_final_callback_timeout_and_suppression_block_handlers(dispatch, monkeypatch):
    from threading import Event
    from hermes_cli import plugins

    release = Event()
    entered = Event()

    def stuck(**_kwargs):
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.05)
    dispatch.manager._hooks["pre_tool_dispatch"] = [stuck]
    try:
        first = dispatch.model.handle_function_call(READ, {}, skip_pre_tool_call_hook=True)
        assert entered.is_set()
        second = dispatch.model.handle_function_call(READ, {}, skip_pre_tool_call_hook=True)
        assert "timed out" in first and "timed out" in second
        assert dispatch.calls == []
    finally:
        release.set()


def test_real_manager_delivery_exception_is_a_veto(dispatch, monkeypatch):
    from hermes_cli import plugins

    def unavailable():
        raise RuntimeError("fixture manager unavailable")

    monkeypatch.setattr(plugins, "_delivery_manager", unavailable)
    result = dispatch.model.handle_function_call(READ, {}, skip_pre_tool_call_hook=True,
                                                 skip_tool_execution_middleware=True)
    assert "policy delivery failed" in result
    assert dispatch.calls == []
