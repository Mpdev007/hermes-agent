"""Security A-1 case 4a/4b: ``hermes acp`` builds the gateway viewer, and the owner refuses an ``acp``
``session.create`` carrying a configuration bypass before any store access or policy build.

4a: ``acp_adapter/entry.py`` ``main`` constructs ``HermesACPAgent()`` (checked from its source, without
importing the entry module); with no session manager that returns ``GatewayACPAgent``, which opens no
transport at construction.
4b: a real ``AuthorityConnection.dispatch`` over a stub authority whose persistence fails on any use; the
refusal is ``gateway/session_local.stage_local_session``'s ``validate_protected_bypass``.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

DETAIL = "protected unattended sources require policy loading"


def test_acp_entry_constructs_the_viewer_without_a_session_manager():
    source = (Path(__file__).resolve().parents[2] / "acp_adapter" / "entry.py").read_text(encoding="utf-8")
    main = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "main")
    built = [node for node in ast.walk(main) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "HermesACPAgent"]
    assert len(built) == 1 and not built[0].args and not built[0].keywords


def test_bare_constructor_is_the_gateway_viewer():
    from acp_adapter.gateway_server import GatewayACPAgent
    from acp_adapter.server import HermesACPAgent
    agent = HermesACPAgent()
    assert type(agent) is GatewayACPAgent and not isinstance(agent, HermesACPAgent)
    assert not hasattr(agent, "session_manager")
    assert agent._gateway is None and agent._connection is None and agent._event_task is None


class _NoStore:
    """Any use is a persistence touch; the refusal must come first."""

    def __init__(self, touched):
        self.touched = touched

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"persistence touched before refusal: {name}")


@pytest.mark.parametrize("flags, field", [({"safe_mode": True, "ignore_user_config": True}, "safe_mode"),
                                          ({"safe_mode": True}, "safe_mode"),
                                          ({"ignore_user_config": True}, "ignore_user_config")])
def test_owner_create_refuses_acp_bypass_before_store_or_policy_build(tmp_path, monkeypatch, flags, field):
    from gateway import session_local, session_policy
    from gateway.session_controls import AuthorityConnection
    touched, built = [], []
    monkeypatch.setattr(session_policy, "build_policy", lambda *a, **k: built.append(a))
    monkeypatch.setattr(session_local, "_bypass_policy", lambda *a, **k: built.append(a))
    authority = SimpleNamespace(instance_id="first", profile_id=str(tmp_path), epoch=1, events={}, runner=None,
                                sessions={}, retiring=False, persistence=_NoStore(touched))
    connection = AuthorityConnection(authority, object(), {"user_id": "human"})  # identity as test_session_policy
    reply = asyncio.run(connection.dispatch({"id": 1, "method": "session.create", "params": {
        "request_id": "a1-4b", "source": "acp", "cwd": str(tmp_path), "model": "offline-fixture",
        "provider": "openrouter", "editor": {"mcp_servers": [], "edit_approval_policy": "ask"}, **flags}}))
    assert reply["error"] == {"code": 4001, "message": "invalid_params",
                              "data": {"reason": "invalid_params", "field": field, "detail": DETAIL}}
    assert touched == [] and built == []
