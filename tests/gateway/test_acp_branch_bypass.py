"""Security A-1 case 4 (branch): an ACP ``branch`` never restores, adopts or prepares from a stored
``acp`` configuration bypass, whether stored as ``acp`` or peer-refreshed to ``local`` (P-1).

``gateway/session_mutations._project_committed('branch')`` restores the child through
``restore_local_session``, which refuses before compression adoption (its first write);
``gateway/session_local_mcp.prepare_branch_editor`` restores the parent before the branch's ``gate``.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.gateway.test_protected_policy_admission import _peer_refreshed_receipt, environment  # noqa: F401

DETAIL = "protected unattended sources require policy loading"
CASES = [(label, safe, "safe_mode" if safe else "ignore_user_config")
         for label in ("acp", "local") for safe in (True, False)]


def _bypass(config, tmp_path, label, safe):
    stored = _peer_refreshed_receipt(config, tmp_path, "acp", safe_mode=safe, ignore_user_config=True)
    stored["source"] = label
    return stored


@pytest.mark.parametrize("label, safe, field", CASES)
def test_branch_projection_refuses_a_bypass_child_before_adoption(environment, tmp_path, monkeypatch, label, safe, field):  # noqa: F811
    from gateway import session_local_recovery as recovery
    from gateway.session_mutations import _project_committed
    _, config, _ = environment
    receipt = {"policy": _bypass(config, tmp_path, label, safe)}
    before = json.dumps(receipt, sort_keys=True)
    asked, writes = [], []
    monkeypatch.setattr(recovery, "owned_receipt",
                        lambda _a, sid: asked.append(sid) or (receipt, SimpleNamespace(chat_id=sid), "route", None))

    def prohibited(*args):
        writes.append(args)
        raise AssertionError("compression adoption must not run")

    monkeypatch.setattr(recovery, "_with_untracked_compression", prohibited)
    with pytest.raises(recovery._ReceiptRefused) as refused:
        _project_committed(SimpleNamespace(runner=SimpleNamespace(session_store=None)),
                           SimpleNamespace(session_id="parent"), "branch", {"branched_session_id": "child"})
    assert (refused.value.reason, refused.value.field, refused.value.detail) == ("invalid_params", field, DETAIL)
    assert asked == ["child"] and writes == [] and json.dumps(receipt, sort_keys=True) == before


@pytest.mark.parametrize("label, safe, field", CASES)
def test_branch_refuses_a_bypass_parent_before_its_gate(environment, tmp_path, label, safe, field):  # noqa: F811
    from gateway.session_local_mcp import prepare_branch_editor
    from persistence.core.errors import AuthorityRefusal
    _, config, _ = environment
    saved = SimpleNamespace(policy=_bypass(config, tmp_path, label, safe), profile_id="p", principal_id="u")
    reads, gated = [], []
    authority = SimpleNamespace(persistence=SimpleNamespace(bindings=SimpleNamespace(
        receipt=lambda sid: reads.append(sid) or saved)))

    async def gate():
        gated.append(True)
        raise AssertionError("the branch's prepare/commit must not run")

    with pytest.raises(AuthorityRefusal) as refused:
        asyncio.run(prepare_branch_editor(authority, SimpleNamespace(session_id="parent"), None, gate))
    assert (refused.value.reason, refused.value.field, refused.value.detail) == ("invalid_params", field, DETAIL)
    assert reads == ["parent"] and gated == []
