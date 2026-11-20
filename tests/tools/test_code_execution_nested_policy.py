"""Security A-1 case 1: a tool call made from inside ``execute_code`` meets the required final policy.

The real in-process servicing of one sandbox request runs under an ``acp`` policy, with no sandbox,
kernel child, shell, socket or network:
  remote_poll_loop  ``_rpc_poll_loop`` on a ``propagate_context_to_thread`` worker, as
                    ``tools/code_execution_tool.py`` ``_run_remote_per_call`` starts it
  kernel_rpc        ``_rpc_forever`` on a bare thread -> the current cell's ``CellAuthority.dispatch``
                    (``tools/code_kernel.py``: authority captured per cell, ``ctx.run`` per call)
Both reach ``model_tools.handle_function_call`` -> ``_dispatch`` -> ``final_tool_block``; only the
registry handler and the transports are stubs. The two production lines that create the carrier (the
``propagate_context_to_thread`` call and ``CellAuthority(...)`` in ``_run_cell``) are reproduced here, not run.
"""
# Product modules are imported inside functions, after ``discover_guard`` binds the fixture home.
import base64
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest

from tests.policy_fixture import discover_guard, protected

READ = "mcp__leann__leann_search"
BUILD = "mcp__leann__leann_build"
TOKEN = "fixture-rpc-token"
# Synthetic grant: SANDBOX_ALLOWED_TOOLS holds neither name, and the RPC allow-list runs before any
# dispatch (code_execution_rpc._handle_rpc_request). The policy, not the grant, is under test.
GRANT = frozenset({READ, BUILD})
IDS = {"task_id": "fixture-task", "session_id": "fixture-session", "turn_id": "fixture-turn",
       "conversation_id": "fixture-conv"}
UNAVAILABLE = ("BLOCKED: required tool policy unavailable: "
               "required policy is absent, disabled or failed to load")
CORE_VETOES = ("BLOCKED: required tool policy unavailable", "BLOCKED: final tool policy")


@pytest.fixture
def gate(tmp_path, monkeypatch, request):
    state = getattr(request, "param", "ready")
    for name in ("HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(name, raising=False)  # only the propagated policy may protect the worker
    fixture = discover_guard(tmp_path, monkeypatch, state)
    # As tests/agent/test_protected_executor_policy.py: dispose only the guard's admission hook, so the
    # final boundary is the sole veto (nested dispatch passes no skip flags).
    for registration in tuple(fixture.manager._ownership_ledger.get("unattended-guard", ())):
        if registration.kind == "hook" and registration.key == "pre_tool_call":
            registration.dispose()
    import model_tools
    from gateway.session_policy import active_policy
    from hermes_cli import tool_dispatch_policy
    from hermes_constants import hermes_home_key
    invoked, decisions = [], []
    real_final = tool_dispatch_policy.final_tool_block

    def final_spy(tool_name, args, **kwargs):
        verdict = real_final(tool_name, args, **kwargs)
        decisions.append((tool_name, verdict, getattr(active_policy(), "source", None), threading.get_ident()))
        return verdict

    def handler(name, args, **_kwargs):
        invoked.append((name, getattr(active_policy(), "source", None), hermes_home_key(), threading.get_ident()))
        return '{"ok":true}'

    # model_tools._dispatch imports final_tool_block at call time, so the spy is what runs.
    monkeypatch.setattr(tool_dispatch_policy, "final_tool_block", final_spy)
    monkeypatch.setattr(model_tools.registry, "dispatch", handler)
    return SimpleNamespace(fixture=fixture, state=state, invoked=invoked, decisions=decisions)


def _request(name, **extra):
    return {"token": TOKEN, "tool": name, "args": {}, **extra}


def _bare_thread(target, *args):
    """Start a plain Thread from an EMPTY Context: the worker sees the policy only through the route's
    own carrier, even on a build where new threads inherit their creator's context."""
    def start():
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread
    return contextvars.Context().run(start)


class _PollEnv:
    """The remote backend's run-to-completion ``env.execute`` answered in memory. Never raises:
    ``_rpc_poll_loop`` swallows errors and re-lists."""

    def __init__(self, request, stop_event, rpc_dir="/remote/hermes_exec_fixture/rpc"):
        self.rpc_dir, self.request, self.stop_event = rpc_dir, json.dumps(request), stop_event
        self.writes, self.unexpected = [], []

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        if stdin_data is not None:  # the res_ file ship (_remote_write)
            self.writes.append((command, base64.b64decode(stdin_data).decode("utf-8")))
        elif command.startswith("ls -1 "):
            listing = "" if self.stop_event.is_set() else f"{self.rpc_dir}/req_000001\n"
            return {"output": listing, "returncode": 0}
        elif command.startswith("cat "):
            return {"output": self.request, "returncode": 0}
        elif command.startswith("rm -f "):
            self.stop_event.set()  # the request was answered and removed: end the loop
        else:
            self.unexpected.append(command)
        return {"output": "", "returncode": 0}


def _remote_poll_loop(name):
    from tools.code_execution_rpc import _rpc_poll_loop
    from tools.thread_context import propagate_context_to_thread
    stop_event, log, counter = threading.Event(), [], [0]
    env = _PollEnv(_request(name, seq=1), stop_event)
    worker = propagate_context_to_thread(_rpc_poll_loop)  # snapshot taken on the policy-bound thread
    thread = _bare_thread(worker, env, env.rpc_dir, IDS["task_id"], log, counter, 5, GRANT, stop_event,
                          TOKEN, IDS["session_id"], IDS["turn_id"], IDS["conversation_id"])
    thread.join(timeout=10)
    stop_event.set()
    assert not thread.is_alive() and env.unexpected == [] and len(env.writes) == 1
    command, response = env.writes[0]
    assert f"{env.rpc_dir}/res_000001" in command
    return SimpleNamespace(response=response, calls=counter[0], errors=[e["error"] for e in log if e.get("error")])


class _KernelListener:
    """Duck-typed listener AND accepted connection for ``_rpc_server_loop``: one request line, released by
    ``go``; later accepts idle until the kernel's stop_event ends ``_rpc_forever``. Raises only
    TimeoutError, which the accept loop expects."""

    def __init__(self, kernel, request):
        self.kernel = kernel
        self.go, self.replied = threading.Event(), threading.Event()
        self.chunks, self.sent, self.pending = [json.dumps(request).encode() + b"\n"], [], True

    def settimeout(self, _seconds):
        pass

    def accept(self):
        if self.pending and self.go.wait(0.05):
            self.pending = False
            return self, ("fixture", 0)
        self.kernel.stop_event.wait(0.05)
        raise TimeoutError

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)
        self.replied.set()

    def close(self):
        pass


def _kernel_rpc(name):
    from tools.code_kernel import CellAuthority, SessionKernel, _rpc_forever
    kernel = SessionKernel(("fixture-owner", "strict", "python", "", tuple(sorted(GRANT))))  # starts nothing
    kernel.rpc_token = TOKEN
    kernel.teardown = kernel.stop_event.set  # never reach the process-group kill
    listener = kernel.server_sock = _KernelListener(kernel, _request(name))
    # As _run_cell: the cell's authority is captured on the policy-bound thread, retired when the cell settles.
    kernel.cell_authority = CellAuthority(IDS["task_id"], IDS["session_id"], IDS["turn_id"], IDS["conversation_id"])
    thread = _bare_thread(_rpc_forever, kernel, 5, GRANT)  # the RPC server thread carries no context
    listener.go.set()
    try:
        listener.replied.wait(timeout=10)
    finally:
        kernel.cell_authority.retire()
        kernel.stop_event.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and len(listener.sent) == 1
    return SimpleNamespace(response=listener.sent[0].decode("utf-8").rstrip("\n"), calls=kernel.tool_call_counter[0],
                           errors=[e["error"] for e in kernel.tool_call_log if e.get("error")])


ROUTES = {"remote_poll_loop": _remote_poll_loop, "kernel_rpc": _kernel_rpc}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_nested_call_vetoed_by_the_required_policy_never_reaches_its_handler(gate, route):
    with protected(gate.fixture):
        outcome = ROUTES[route](BUILD)
    assert gate.invoked == []
    [(name, verdict, source, ident)] = gate.decisions
    assert (name, source) == (BUILD, "acp") and ident != threading.get_ident()
    # The guard's own decision (its wording lives outside this tree), not an unavailability veto.
    assert isinstance(verdict, str) and verdict and not verdict.startswith(CORE_VETOES)
    assert json.loads(outcome.response) == {"error": verdict}
    assert outcome.calls == 1 and outcome.errors == [verdict[:300]]


@pytest.mark.parametrize("gate", ["absent", "import_failed"], indirect=True)
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_nested_call_without_a_loaded_required_policy_is_refused(gate, route):
    plugins = gate.fixture.manager._plugins
    if gate.state == "absent":
        assert "unattended-guard" not in plugins
    else:
        assert plugins["unattended-guard"].error
    with protected(gate.fixture):
        outcome = ROUTES[route](READ)  # the name a loaded guard allows: only the missing policy refuses
    assert gate.invoked == []
    [(name, verdict, source, ident)] = gate.decisions
    assert (name, verdict, source) == (READ, UNAVAILABLE, "acp") and ident != threading.get_ident()
    assert json.loads(outcome.response) == {"error": UNAVAILABLE}
    assert outcome.calls == 1 and outcome.errors == [UNAVAILABLE]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_nested_call_allowed_by_the_required_policy_reaches_its_handler_once(gate, route):
    with protected(gate.fixture):
        outcome = ROUTES[route](READ)
    assert [entry[:3] for entry in gate.invoked] == [(READ, "acp", str(gate.fixture.home))]
    assert gate.invoked[0][3] != threading.get_ident()
    assert [entry[:3] for entry in gate.decisions] == [(READ, None, "acp")]
    assert json.loads(outcome.response) == {"ok": True}
    assert outcome.calls == 1 and outcome.errors == []
