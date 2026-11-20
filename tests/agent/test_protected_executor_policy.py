"""Real agent executor paths; the reviewed guard loads natively, handlers are harmless stubs."""
import json
from types import SimpleNamespace

import pytest

from tests.policy_fixture import discover_guard, protected

READ = "mcp__leann__leann_search"
BUILD = "mcp__leann__leann_build"


def call(name, args=None, call_id="fixture-call"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
        name=name, arguments=json.dumps(args or {})))


@pytest.fixture
def executor(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from tests.agent.test_tool_activity_heartbeat import _make_agent
    from agent import tool_executor, agent_runtime_helpers
    from hermes_cli.plugins_context import PluginContext
    names = (READ, BUILD, "memory", "delegate_task", "fixture_context", "fixture_memory")
    # Use the existing minimal native-executor fixture, not AIAgent construction:
    # the full constructor probes Python/pip via subprocess, outside this test's scope.
    agent = _make_agent(monkeypatch)
    agent.valid_tool_names = set(names)
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent._memory_manager = None
    agent._context_engine_tool_names = set()
    agent.context_compressor = SimpleNamespace(context_length=8192)
    agent._tool_guardrails = SimpleNamespace(before_call=lambda *_a: SimpleNamespace(allows_execution=True))
    agent._tool_guardrail_halt_decision = None
    agent._invoke_tool = lambda *a, **k: agent_runtime_helpers.invoke_tool(agent, *a, **k)
    agent._execute_tool_calls_concurrent = lambda *a, **k: tool_executor.execute_tool_calls_concurrent(agent, *a, **k)
    agent._execute_tool_calls_sequential = lambda *a, **k: tool_executor.execute_tool_calls_sequential(agent, *a, **k)
    agent._should_start_quiet_spinner = lambda: False
    agent._flush_messages_to_session_db = lambda *_a, **_k: None
    agent._record_file_mutation_result = lambda *_a, **_k: None
    # Remove only the admission registration through its native lease. This deliberately
    # exercises the final boundary as sole veto, rather than passing because pre_tool_call
    # already denied the name. The actual required final guard stays loaded and registered.
    for registration in tuple(fixture.manager._ownership_ledger["unattended-guard"]):
        if registration.kind == "hook" and registration.key == "pre_tool_call":
            registration.dispose()
    ctx = PluginContext(fixture.manager._plugins["unattended-guard"].manifest, fixture.manager)
    return fixture, agent, ctx


@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_concurrent_executor_and_authorization_gate_keep_context(executor, monkeypatch, wrapped):
    fixture, agent, _ = executor
    from agent import tool_executor
    from gateway.session_policy import active_policy
    from hermes_constants import hermes_home_key
    import model_tools
    invoked, gates = [], []
    native_gate = tool_executor._ConcurrentToolAuthorizationGate.run

    def observed_gate(self, resolve):
        gates.append(True)
        return native_gate(self, resolve)

    def handler(name, args, **_k):
        invoked.append((name, active_policy().source, hermes_home_key()))
        return '{"ok":true}'

    monkeypatch.setattr(tool_executor._ConcurrentToolAuthorizationGate, "run", observed_gate)
    monkeypatch.setattr(model_tools.registry, "dispatch", handler)
    targets = [(READ, "read-1"), (BUILD, "deny-1"), (READ, "read-2")]
    if wrapped:
        from tools import tool_search
        definitions = [{"type": "function", "function": {"name": name, "description": "fixture",
                        "parameters": {"type": "object", "properties": {}}}} for name in (READ, BUILD)]
        monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_k: definitions)
        monkeypatch.setattr(tool_search, "scoped_deferrable_names", lambda *_a: {READ, BUILD})
        monkeypatch.setattr(tool_search, "session_unavailable_error", lambda *_a: None)
        monkeypatch.setattr(tool_search, "is_deferrable_tool_name", lambda *_a: True)
        monkeypatch.setattr(tool_search, "validate_deferred_call_args", lambda *_a: None)
        agent.valid_tool_names.add("tool_call")
        calls = [call("tool_call", {"name": name, "arguments": {}}, call_id) for name, call_id in targets]
    else:
        calls = [call(name, call_id=call_id) for name, call_id in targets]
    messages = []
    with protected(fixture):
        agent._execute_tool_calls_concurrent(SimpleNamespace(tool_calls=calls), messages, "fixture-task", finalize=False)
    assert len(gates) == 3
    assert invoked == [(READ, "acp", str(fixture.home)), (READ, "acp", str(fixture.home))]
    assert [m["tool_call_id"] for m in messages] == ["read-1", "deny-1", "read-2"]
    assert "error" in messages[1]["content"].lower()


@pytest.mark.parametrize("route", ["inline", "delegate", "memory_provider", "context_engine"])
def test_actual_sequential_paths_veto_after_execution_middleware_rewrite(executor, monkeypatch, route):
    fixture, agent, ctx = executor
    from agent import tool_executor
    invoked, rewritten, observed = [], [], []
    names = {"inline": "memory", "delegate": "delegate_task",
             "memory_provider": "fixture_memory", "context_engine": "fixture_context"}
    name = names[route]

    def handler(args, **_k):
        invoked.append(args)
        return '{"ok":true}'

    monkeypatch.setitem(tool_executor.INLINE_TOOL_EXECUTORS, "memory", lambda _a, args, _ctx: handler(args))
    agent._dispatch_delegate_task = handler
    agent._context_engine_tool_names = {"fixture_context"}
    agent.context_compressor = SimpleNamespace(context_length=8192, handle_tool_call=lambda _name, args, **_k: handler(args))
    agent._memory_manager = SimpleNamespace(has_tool=lambda target: target == "fixture_memory",
                                            handle_tool_call=lambda _name, args, **_k: handler(args))

    def rewrite(tool_name, args, next_call, **_kwargs):
        rewritten.append(tool_name)
        return next_call({**args, "operation": "mutate"})

    def final_observer(tool_name, args, **_kwargs):
        observed.append((tool_name, args.get("operation")))

    ctx.register_middleware("tool_execution", rewrite)
    ctx.register_hook("pre_tool_dispatch", final_observer)
    messages = []
    with protected(fixture):
        agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call(name, {"operation": "read"})]),
                                            messages, "fixture-task", finalize=False)
    assert rewritten == [name]
    assert (name, "mutate") in observed
    assert invoked == []
    assert len(messages) == 1 and "error" in messages[0]["content"].lower()
