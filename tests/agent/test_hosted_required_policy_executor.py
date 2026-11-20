"""Security A-1 case 5: a hosted scope and a required tool policy both hold, with zero handler calls.

Agent loop: ``tool_executor._run_agent_tool_execution_middleware`` refuses a hosted-refused name before
relay and middleware, and ``_dispatch_authorized_once`` consults ``final_tool_block`` only when nothing
refused first. A name the hosted allowlist admits (``write_file``) still meets the final gate, which the
loaded guard vetoes (test_required_tool_policy::test_real_guard_discovery_denies_forbidden_targets).
``registry.dispatch`` is a recording stub throughout, so nothing is ever written.

The fixture's ``acp`` policy uses ACP's existing "accept edits" mode (``workspace_session``) and every
``write_file`` call names a real target under the fixture's working directory: the default ``ask`` mode,
or a call without a path, is refused by ACP edit approval before the final gate is reached, which would
test the edit guard instead of the policy gate (QA failure, Security reconciliation ``ca8cac6d`` §2).
"""
import contextlib
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.agent.test_protected_executor_policy import BUILD, call, executor  # noqa: F401  (fixture)
from tests.policy_fixture import protected

VAULT, VAULT_ARGS = "browser_vault", {"action": "list"}
WRITE = "write_file"  # on HOSTED_TOOL_ALLOWLIST; vetoed by the loaded guard
CORE_VETOES = ("BLOCKED: required tool policy unavailable", "BLOCKED: final tool policy")
EDIT_REFUSAL = "Edit approval denied"  # acp_adapter/edit_approval.py: an edit refusal, never a policy verdict


def write_args(fixture):
    """A real, absent target under the fixture's working directory; the stubbed handler never writes it."""
    return {"path": str(Path(fixture.policy.cwd) / "a1-hosted.txt"), "content": "a1"}


@contextlib.contextmanager
def hosted_protected(fixture):
    """``protected`` FIRST: its terminal-policy bind is refused inside an already bound non-local scope."""
    from agent.runtime_scope import bind_state
    from hermes_scope import Principal, Scope
    hosted = Scope("acme", "agent-a", Principal("u1"), owner=Principal("u1"), can_administer=True)  # as test_hosted_tool_refusals
    # ACP "accept edits" for this synthetic acp session only: a non-sensitive edit under the session's
    # working directory is approved without a prompt, so each write_file call reaches the final gate.
    with protected(fixture, replace(fixture.policy, edit_approval_policy="workspace_session")), bind_state(hosted):
        yield


def hosted_text(name):
    from tools.hosted_tool_policy import HOSTED_TOOL_ALLOWLIST
    return (f"{name} is not available to a hosted agent: only tools that act solely through the agent's "
            f"sandbox are enabled. A hosted agent's tools are {', '.join(sorted(HOSTED_TOOL_ALLOWLIST))}, "
            "all run in its own sandbox.")


@pytest.fixture
def hosted(executor, monkeypatch):  # noqa: F811
    fixture, agent, ctx = executor
    import tools.browser_vault_tool  # noqa: F401  registers browser_vault for the real inline wrapper
    from agent import inline_tool_executors as ite
    from hermes_cli import tool_dispatch_policy
    from tools.registry import registry
    seen = SimpleNamespace(gates=[], entered=[], calls=[], rewritten=[], real_gate=tool_dispatch_policy.final_tool_block)
    real_checks = ite.call_with_arg_checks

    def gate(name, args, **kwargs):
        verdict = seen.real_gate(name, args, **kwargs)
        seen.gates.append((name, verdict))
        return verdict

    def checks(entry, args, fn):
        seen.entered.append(getattr(entry, "name", None))
        return real_checks(entry, args, fn)

    def dispatch(name, args, **kwargs):
        seen.calls.append((name, dict(args)))
        return '{"ok": true}'

    def rewrite(tool_name, args, next_call, **_kwargs):  # parameter names matter: kwargs are narrowed by name
        seen.rewritten.append(tool_name)
        return next_call(args)

    monkeypatch.setattr(tool_dispatch_policy, "final_tool_block", gate)
    monkeypatch.setattr(ite, "call_with_arg_checks", checks)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    ctx.register_middleware("tool_execution", rewrite)
    agent.valid_tool_names |= {VAULT, WRITE}
    agent._append_guardrail_observation = lambda _name, _args, result, *_a, **_k: result
    return fixture, agent, seen


def run_sequential(agent, name, args, call_id):
    messages = []
    agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call(name, args, call_id)]),
                                         messages, "fixture-task", finalize=False)
    assert len(messages) == 1 and messages[0]["tool_call_id"] == call_id
    return messages[0]["content"]


@pytest.mark.parametrize("name, args", [(BUILD, {}), (VAULT, VAULT_ARGS)], ids=["registry", "inline"])
def test_hosted_refusal_precedes_the_final_gate_on_the_agent_loop(hosted, name, args):
    fixture, agent, seen = hosted
    from tools.hosted_tool_policy import hosted_tool_refusal
    with hosted_protected(fixture):
        assert hosted_tool_refusal(name) == hosted_text(name)
        content = run_sequential(agent, name, args, "hosted")
    assert hosted_text(name) in content
    assert seen.gates == [] and seen.rewritten == [] and seen.entered == [] and seen.calls == []


def test_invoke_tool_in_a_hosted_scope_runs_no_handler(hosted):
    fixture, agent, seen = hosted
    with hosted_protected(fixture):
        build = agent._invoke_tool(BUILD, {}, "fixture-task", "inv-build", messages=[])
        assert seen.gates == []  # handle_function_call's hosted refusal comes before its final gate
        vault = agent._invoke_tool(VAULT, dict(VAULT_ARGS), "fixture-task", "inv-vault", messages=[])
    assert json.loads(build) == {"error": hosted_text(BUILD)}
    # invoke_tool's inline route gates first; whichever refuses (the guard, or _coerced's hosted check),
    # the real inline body and the handler never run.
    [(gated, verdict)] = seen.gates
    assert gated == VAULT
    assert json.loads(vault) == {"error": verdict if verdict is not None else hosted_text(VAULT)}
    assert seen.entered == [] and seen.calls == []


def test_hosted_allowed_tool_is_still_vetoed_by_the_final_gate(hosted):
    fixture, agent, seen = hosted
    from tools.hosted_tool_policy import hosted_tool_refusal
    args = write_args(fixture)
    with protected(fixture):  # the guard's verdict for write_file, outside any hosted scope
        assert seen.real_gate(WRITE, dict(args)) is not None
    with hosted_protected(fixture):
        assert hosted_tool_refusal(WRITE) is None
        content = run_sequential(agent, WRITE, dict(args), "w-seq")
        [(gated, verdict)] = seen.gates
        seen.gates.clear()
        result = agent._invoke_tool(WRITE, dict(args), "fixture-task", "w-inv", messages=[])
    assert gated == WRITE and isinstance(verdict, str) and verdict and not verdict.startswith(CORE_VETOES)
    assert EDIT_REFUSAL not in verdict and EDIT_REFUSAL not in content and EDIT_REFUSAL not in result
    assert verdict in content or json.dumps(verdict, ensure_ascii=False)[1:-1] in content  # raw or JSON-encoded
    assert seen.gates == [(WRITE, verdict)]  # handle_function_call._dispatch, inside the hosted scope
    assert json.loads(result) == {"error": verdict}
    assert seen.calls == [] and not Path(args["path"]).exists()


def test_control_with_the_final_gate_open_the_hosted_allowed_tool_reaches_its_handler(hosted, monkeypatch):
    """Same hosted scope and call: only the final gate stood between write_file and its handler."""
    fixture, agent, seen = hosted
    from hermes_cli import tool_dispatch_policy
    monkeypatch.setattr(tool_dispatch_policy, "final_tool_block",
                        lambda name, args, **_k: seen.gates.append((name, None)))
    args = write_args(fixture)
    with hosted_protected(fixture):
        content = run_sequential(agent, WRITE, dict(args), "w-open")
    assert EDIT_REFUSAL not in content  # the edit was auto-approved, so the handler stub is reached
    assert seen.calls == [(WRITE, args)] and (WRITE, None) in seen.gates
    assert not Path(args["path"]).exists()  # the stub records the call; nothing is written
