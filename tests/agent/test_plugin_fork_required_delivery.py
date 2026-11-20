"""Security A-1 case 3: a plugin session fork inside an ``acp`` turn meets required delivery.

Real: ``plugin_session_fork._launch`` (worker built in the captured turn context), ``_run_job``,
``_pin_capture``, the fork's thread tool whitelist, the concurrent executor, ``final_tool_block`` and
required delivery. Synthetic: the fork object (the template's native executor stub, returned by a stubbed
``build_cache_parity_fork``) and ``own_tools={READ, BUILD}``, so the fork whitelist admits both names and
the required policy is the only veto. The provider refusal check is stubbed (not under test).
"""
import contextvars
import weakref
from types import SimpleNamespace

import pytest

from tests.agent.test_protected_executor_policy import BUILD, READ, call, executor  # noqa: F401  (fixture)
from tests.policy_fixture import protected

PLUGIN = "a1-fixture"
ORIGIN = f"plugin_fork:{PLUGIN}"
INCOMPLETE = "BLOCKED: required tool policy unavailable: policy discovery is incomplete"
CORE_VETOES = ("BLOCKED: required tool policy unavailable", "BLOCKED: final tool policy")


class _Parent(SimpleNamespace):
    """Weak-referenceable parent (as tests/agent/test_plugin_session_fork.py)."""


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


def _parent():
    # The fork's valid_tool_names come from these captured tools (_pin_capture).
    return _Parent(_persist_disabled=False, _cached_system_prompt="sys", _cached_system_prompt_static=None,
                   tools=[_tool(READ), _tool(BUILD)], session_id="sess", _current_turn_id="turn-1",
                   model="m", provider="openrouter", base_url="https://openrouter.ai/api/v1",
                   api_mode="chat_completions", reasoning_config=None)


def _job(parent):
    """test_plugin_session_fork._job_holding; built INSIDE protected(), so the capture is the acp turn."""
    from agent import background_review, plugin_session_fork
    from agent.side_question_request import SideQuestionCapture, runtime_key
    names = frozenset({READ, BUILD})
    capture = SideQuestionCapture(parent_ref=weakref.ref(parent), messages=[], system_prompt="sys",
                                  system_prompt_static=None, tools=parent.tools, session_id="sess",
                                  turn_id="turn-1", runtime=runtime_key(parent), context=contextvars.copy_context())
    job = plugin_session_fork._ForkJob(PLUGIN, capture, "q", names, 1, None, {}, None,
                                       background_review._BackgroundReviewRun(), lambda: names)
    job.handle = plugin_session_fork.PluginForkHandle(PLUGIN, lambda: None)
    return job


@pytest.fixture
def fork(executor, monkeypatch):  # noqa: F811
    fixture, agent, _ = executor
    import model_tools
    from agent import background_review, side_question_request
    from agent.plugin_session_fork import current_turn_origin
    from gateway.session_policy import active_policy
    from hermes_cli import tool_dispatch_policy
    seen = SimpleNamespace(invoked=[], gate=[], built=[], messages=[])
    native_gate = tool_dispatch_policy.final_tool_block  # every caller imports it at call time

    def observed_gate(tool_name, args, **kwargs):
        verdict = native_gate(tool_name, args, **kwargs)
        seen.gate.append((tool_name, current_turn_origin(), getattr(active_policy(), "source", None), verdict))
        return verdict

    def handler(name, args, **kwargs):
        seen.invoked.append((name, getattr(active_policy(), "source", None), current_turn_origin(), kwargs.get("turn_origin")))
        return '{"ok":true}'

    def run_conversation(user_message, conversation_history=None):
        calls = [call(READ, call_id="read-1"), call(BUILD, call_id="deny-1")]
        agent._execute_tool_calls_concurrent(SimpleNamespace(tool_calls=calls), seen.messages, "fork-task", finalize=False)
        return {"final_response": "done", "messages": []}

    def build(parent, task_cfg=None, *, max_iterations, write_origin="background_review"):
        seen.built.append(write_origin)
        return agent, {}, False  # not routed: the runtime check and _pin_capture run

    agent._review_input_token_budget = None
    agent.run_conversation = run_conversation
    monkeypatch.setattr(tool_dispatch_policy, "final_tool_block", observed_gate)
    monkeypatch.setattr(model_tools.registry, "dispatch", handler)
    monkeypatch.setattr(background_review, "build_cache_parity_fork", build)
    monkeypatch.setattr(background_review, "release_cache_parity_fork", lambda *_a, **_k: {})
    monkeypatch.setattr(side_question_request, "_agent_provider_refusal", lambda _parent: None)
    return fixture, agent, seen


def _run(fixture):
    from agent import plugin_session_fork
    parent = _parent()  # strong reference: the capture holds it weakly
    with protected(fixture):
        job = _job(parent)
        plugin_session_fork._launch(job)
        assert job.handle.wait(30), job.handle
    return job.handle


def test_forked_call_meets_required_delivery(fork):
    from agent.plugin_session_fork import current_turn_origin
    fixture, agent, seen = fork
    handle = _run(fixture)
    assert handle.outcome == "ok", handle
    assert seen.built == [ORIGIN] and agent.valid_tool_names == {READ, BUILD}
    assert seen.invoked == [(READ, "acp", ORIGIN, ORIGIN)]
    assert {(name, origin, source) for name, origin, source, _ in seen.gate} == {(READ, ORIGIN, "acp"), (BUILD, ORIGIN, "acp")}
    assert all(verdict is None for name, _, _, verdict in seen.gate if name == READ)
    build_verdicts = [verdict for name, _, _, verdict in seen.gate if name == BUILD]
    assert build_verdicts and all(isinstance(v, str) and v and not v.startswith(CORE_VETOES) for v in build_verdicts)
    assert [m["tool_call_id"] for m in seen.messages] == ["read-1", "deny-1"]
    denied = seen.messages[1]["content"]
    assert "error" in denied.lower() and "may only call" not in denied  # the policy refused, not the whitelist
    assert current_turn_origin() is None


def test_forked_call_is_vetoed_while_discovery_is_incomplete(fork, monkeypatch):
    fixture, _, seen = fork
    # Not unload(): that also clears _discovered, and admission delivery would re-run discovery first.
    monkeypatch.setattr(fixture.manager, "_discovery_complete", False)
    handle = _run(fixture)
    assert handle.outcome == "ok", handle
    assert seen.invoked == []
    assert {(name, verdict) for name, _, _, verdict in seen.gate} == {(READ, INCOMPLETE), (BUILD, INCOMPLETE)}
    assert all(origin == ORIGIN and source == "acp" for _, origin, source, _ in seen.gate)
    assert len(seen.messages) == 2 and all(INCOMPLETE in m["content"] for m in seen.messages)
    assert fixture.manager._discovered is True
