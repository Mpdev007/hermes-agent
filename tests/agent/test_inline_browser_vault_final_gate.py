"""Security A-1 case 2: a vetoed ``browser_vault`` call never enters the real inline executor.

``agent/inline_tool_executors.py`` ``_browser_vault`` calls ``registry.dispatch`` with no final gate of its
own; both in-tree callers of ``INLINE_TOOL_EXECUTORS`` gate first:
  sequential   ``tool_executor._dispatch_authorized_once``: ``final_tool_block`` before ``execute(ref.args)``
  invoke_tool  ``agent_runtime_helpers.invoke_tool._execute``: ``final_tool_block`` before ``inline_executor``
The table entry stays the real ``_coerced`` wrapper; only ``registry.dispatch`` is a recording stub.
The vetoes are core required-policy refusals with exact text, so they do not depend on the guard's allowlist.
"""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.agent.test_protected_executor_policy import call, executor  # noqa: F401  (fixture)
from tests.policy_fixture import discover_guard, protected

NAME = "browser_vault"
ARGS = {"action": "list"}  # the required argument; never takes the preview-default branch
INCOMPLETE = "BLOCKED: required tool policy unavailable: policy discovery is incomplete"
UNAVAILABLE = ("BLOCKED: required tool policy unavailable: "
               "required policy is absent, disabled or failed to load")
INVOKE_FLAGS = {
    "direct": {},
    # exactly what the concurrent worker passes to invoke_tool
    "concurrent_worker": dict(pre_tool_block_checked=True, skip_tool_request_middleware=True,
                              skip_tool_execution_middleware=True, tool_request_middleware_trace=[]),
}


def _native_agent(monkeypatch):
    """The template's minimal native executor (test_protected_executor_policy.executor) for a profile
    whose guard is not loaded."""
    from tests.agent.test_tool_activity_heartbeat import _make_agent
    from agent import agent_runtime_helpers, tool_executor
    agent = _make_agent(monkeypatch)
    agent.valid_tool_names = {NAME}
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent._memory_manager = None
    agent._context_engine_tool_names = set()
    agent.context_compressor = SimpleNamespace(context_length=8192)
    agent._tool_guardrails = SimpleNamespace(before_call=lambda *_a: SimpleNamespace(allows_execution=True))
    agent._tool_guardrail_halt_decision = None
    agent._invoke_tool = lambda *a, **k: agent_runtime_helpers.invoke_tool(agent, *a, **k)
    agent._execute_tool_calls_sequential = lambda *a, **k: tool_executor.execute_tool_calls_sequential(agent, *a, **k)
    agent._should_start_quiet_spinner = lambda: False
    agent._flush_messages_to_session_db = lambda *_a, **_k: None
    agent._record_file_mutation_result = lambda *_a, **_k: None
    return agent


def _probes(monkeypatch):
    """Observe, never replace, the gate and the real ``_coerced`` body; stub only the registry handler."""
    import tools.browser_vault_tool  # noqa: F401  registers browser_vault for the real wrapper's lookup
    from agent import inline_tool_executors as ite
    from hermes_cli import tool_dispatch_policy
    from tools.registry import registry
    seen = SimpleNamespace(gates=[], entered=[], calls=[])
    real_gate, real_checks = tool_dispatch_policy.final_tool_block, ite.call_with_arg_checks

    def gate(name, args, **kwargs):
        verdict = real_gate(name, args, **kwargs)
        seen.gates.append((name, verdict))
        return verdict

    def checks(entry, args, fn):  # the first call inside _coerced after its hosted check
        seen.entered.append(getattr(entry, "name", None))
        return real_checks(entry, args, fn)

    def dispatch(name, args, **kwargs):
        seen.calls.append((name, dict(args), kwargs))
        return '{"ok": true}'

    monkeypatch.setattr(tool_dispatch_policy, "final_tool_block", gate)
    monkeypatch.setattr(ite, "call_with_arg_checks", checks)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    from agent import tool_executor
    assert tool_executor.INLINE_TOOL_EXECUTORS[NAME] is ite.INLINE_TOOL_EXECUTORS[NAME]
    assert ite.INLINE_TOOL_EXECUTORS[NAME].__qualname__ == "_coerced.<locals>._exec"  # the real table entry
    return seen


def _vetoed_setup(variant, executor, tmp_path, monkeypatch):  # noqa: F811
    if variant == "discovery_incomplete":
        fixture, agent, _ = executor
        monkeypatch.setattr(fixture.manager, "_discovery_complete", False)
        return fixture, agent, INCOMPLETE
    fixture = discover_guard(tmp_path / "absent", monkeypatch, "absent")
    return fixture, _native_agent(monkeypatch), UNAVAILABLE


@pytest.fixture
def absent_root(tmp_path):
    (tmp_path / "absent").mkdir()
    return tmp_path


@pytest.mark.parametrize("variant", ["discovery_incomplete", "policy_absent"])
def test_sequential_veto_never_enters_the_real_inline_executor(executor, absent_root, monkeypatch, variant):  # noqa: F811
    fixture, agent, veto = _vetoed_setup(variant, executor, absent_root, monkeypatch)
    seen = _probes(monkeypatch)
    agent.valid_tool_names.add(NAME)
    messages = []
    with protected(fixture):
        agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call(NAME, ARGS, "vault-seq")]),
                                             messages, "fixture-task", finalize=False)
    assert seen.gates == [(NAME, veto)]
    assert seen.entered == [] and seen.calls == []
    assert len(messages) == 1 and messages[0]["tool_call_id"] == "vault-seq" and veto in messages[0]["content"]


@pytest.mark.parametrize("flags", INVOKE_FLAGS.values(), ids=INVOKE_FLAGS.keys())
@pytest.mark.parametrize("variant", ["discovery_incomplete", "policy_absent"])
def test_invoke_tool_veto_never_enters_the_real_inline_executor(executor, absent_root, monkeypatch, variant, flags):  # noqa: F811
    fixture, agent, veto = _vetoed_setup(variant, executor, absent_root, monkeypatch)
    seen = _probes(monkeypatch)
    with protected(fixture):
        result = agent._invoke_tool(NAME, dict(ARGS), "fixture-task", "vault-inv", messages=[], **flags)
    assert seen.gates == [(NAME, veto)]
    assert json.loads(result) == {"error": veto}
    assert seen.entered == [] and seen.calls == []


@pytest.mark.parametrize("route", ["sequential", "invoke_tool"])
def test_control_an_allowing_gate_reaches_the_stub_through_the_real_executor(tmp_path, monkeypatch, route):
    """Attended ``cli`` needs no required policy (test_required_tool_policy::test_attended_cli_does_not_require_a_plugin)."""
    fixture = discover_guard(tmp_path, monkeypatch, "absent")
    agent = _native_agent(monkeypatch)
    # The real call passes (name, args, result, ...); the shared heartbeat stub returns its second argument.
    agent._append_guardrail_observation = lambda _name, _args, result, *_a, **_k: result
    seen = _probes(monkeypatch)
    with protected(fixture, replace(fixture.policy, source="cli", platform="cli")):
        if route == "sequential":
            messages = []
            agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call(NAME, ARGS, "vault-ok")]),
                                                 messages, "fixture-task", finalize=False)
            assert len(messages) == 1 and '{"ok": true}' in messages[0]["content"]  # the stub's body, possibly wrapped
        else:
            assert json.loads(agent._invoke_tool(NAME, dict(ARGS), "fixture-task", "vault-ok", messages=[])) == {"ok": True}
    assert seen.gates == [(NAME, None)]
    assert seen.entered == [NAME]
    [(name, args, kwargs)] = seen.calls
    assert (name, args, kwargs["task_id"]) == (NAME, ARGS, "fixture-task") and "preview_callback" in kwargs
