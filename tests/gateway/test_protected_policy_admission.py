"""Protected bypass admission/resume/worker regressions; no session or worker is started."""
import asyncio
from dataclasses import asdict, replace
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from gateway.session_policy import build_policy
    config = {"platform_toolsets": {"cli": ["terminal"], "tui": ["terminal"], "acp": ["terminal"]}}
    ordinary = build_policy({"source": "cli", "cwd": str(tmp_path), "model": "offline-fixture"}, config)
    return home, config, ordinary


@pytest.mark.parametrize("source", ["acp", "oneshot"])
@pytest.mark.parametrize("flag", ["safe_mode", "ignore_user_config"])
def test_protected_create_refuses_bypass(environment, source, flag):
    from gateway.session_policy import build_policy
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    home, config, _ = environment
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        build_policy({"source": source, "cwd": str(home), "model": "offline-fixture", flag: True}, config)


@pytest.mark.parametrize("source", ["cli", "tui"])
@pytest.mark.parametrize("flag", ["safe_mode", "ignore_user_config"])
def test_attended_troubleshooting_remains_available(environment, source, flag):
    from gateway.session_policy import build_policy
    home, config, _ = environment
    policy = build_policy({"source": source, "cwd": str(home), "model": "offline-fixture", flag: True}, config)
    assert policy.ignore_user_config is True
    assert policy.safe_mode is (flag == "safe_mode")


@pytest.mark.parametrize("safe", [True, False])
def test_stored_acp_bypass_is_refused_without_rewriting_receipt(environment, safe):
    from gateway.session_policy import restore_policy
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, _, ordinary = environment
    saved = {"session_id": "do-not-replace", "request_id": "do-not-replay", "policy": asdict(replace(
        ordinary, source="acp", platform="acp", safe_mode=safe, ignore_user_config=True))}
    before = json.dumps(saved, sort_keys=True)
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        restore_policy(saved["policy"])
    assert json.dumps(saved, sort_keys=True) == before


def test_resume_refuses_before_compression_adoption_or_state_changes(environment, monkeypatch):
    from gateway import session_local_recovery as recovery
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, _, ordinary = environment
    receipt = {"policy": asdict(replace(ordinary, source="acp", platform="acp", safe_mode=True, ignore_user_config=True))}
    monkeypatch.setattr(recovery, "owned_receipt", lambda *_a: (receipt, SimpleNamespace(chat_id="saved"), "route", None))
    mutations = []

    def prohibited(*_a):
        mutations.append("compression adoption")
        raise AssertionError("must refuse before adoption")

    monkeypatch.setattr(recovery, "_with_untracked_compression", prohibited)
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        recovery.restore_local_session(SimpleNamespace(runner=SimpleNamespace(session_store=None)), "saved")
    assert mutations == []


@pytest.mark.parametrize("safe", [True, False])
@pytest.mark.parametrize("top_level", [True, False])
def test_worker_bootstrap_refuses_top_level_or_persisted_bypass(environment, monkeypatch, safe, top_level):
    from agent.managed_worker import bind_worker_policy
    from agent import safe_worker_policy
    _, _, ordinary = environment
    policy = replace(ordinary, source="acp", platform="acp", safe_mode=safe if not top_level else False,
                     ignore_user_config=not top_level)
    bound = []

    def prohibited(*_a, **_k):
        bound.append(True)
        raise AssertionError("must not bind a bypass worker")

    monkeypatch.setattr(safe_worker_policy, "_bind_safe_worker_policy", prohibited)
    monkeypatch.setattr(safe_worker_policy, "_bind_worker_config", prohibited)
    with pytest.raises(ValueError, match="protected source"):
        bind_worker_policy({"policy": asdict(policy), "safe_mode": safe if top_level else False,
                            "ignore_user_config": top_level})
    assert bound == []


def test_worker_admission_refuses_before_spawning_bookkeeping(environment, monkeypatch):
    from gateway import session_managed_worker as worker
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, _, ordinary = environment
    policy = replace(ordinary, source="acp", platform="acp", safe_mode=True, ignore_user_config=True)
    marks = []

    def prohibited(*_a):
        marks.append(True)
        raise AssertionError("must refuse before worker bookkeeping")

    monkeypatch.setattr(worker, "_mark_spawning", prohibited)
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        asyncio.run(worker.execute_managed(SimpleNamespace(), None, {}, policy))
    assert marks == []


@pytest.mark.parametrize("present", [True, False])
def test_worker_receives_current_owner_requirement_without_changing_saved_config(environment, monkeypatch, present):
    from gateway import run
    from gateway.session_managed_worker import _worker_config
    home, _, _ = environment
    saved = {"plugins": {"enabled": ["existing"], "required_tool_policies": {"acp": ["old-policy"]}}, "model": "frozen-model"}
    live = {"plugins": {"required_tool_policies": {"acp": ["current-policy"]}}} if present else {}
    before = deepcopy(saved)
    policy = SimpleNamespace(ignore_user_config=False, config=lambda *_a: deepcopy(saved))
    monkeypatch.setattr(run, "_load_gateway_config", lambda *_a: live)
    result = _worker_config(SimpleNamespace(profile_id=str(home)), policy)
    assert result["plugins"].get("required_tool_policies") == live.get("plugins", {}).get("required_tool_policies")
    assert result["plugins"]["enabled"] == ["existing"] and result["model"] == "frozen-model"
    assert saved == before


def _peer_refreshed_receipt(config, tmp_path, source, built=None, **stored_flags):
    """A receipt as a peer refresh stores it: ``source='local'``, platform kept (#148473). ``built`` flags go
    through ``build_policy`` (a genuine attended receipt); ``stored_flags`` model a legacy receipt written
    before protected admission existed, which ``build_policy`` would now refuse to create."""
    from gateway.session_policy import build_policy
    stored = asdict(build_policy({"source": source, "cwd": str(tmp_path), "model": "offline-fixture",
                                  **(built or {})}, config))
    stored.update(source="local", **stored_flags)
    if source == "acp":
        stored["edit_approval_policy"] = "workspace_session"  # as the upstream round-trip case stores it
    return stored


@pytest.mark.parametrize("safe", [True, False])
def test_peer_refreshed_acp_bypass_receipt_is_refused_without_rewriting(environment, tmp_path, safe):
    from gateway.session_policy import restore_policy
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, config, _ = environment
    saved = {"session_id": "do-not-replace", "request_id": "do-not-replay",
             "policy": _peer_refreshed_receipt(config, tmp_path, "acp", safe_mode=safe, ignore_user_config=True)}
    before = json.dumps(saved, sort_keys=True)
    with pytest.raises(RuntimeStoreError, match="invalid_params") as refused:
        restore_policy(saved["policy"])
    assert refused.value.field == ("safe_mode" if safe else "ignore_user_config")
    assert json.dumps(saved, sort_keys=True) == before


@pytest.mark.parametrize("safe", [True, False])
def test_peer_refreshed_acp_bypass_resume_refuses_before_compression_adoption(environment, tmp_path, monkeypatch, safe):
    from gateway import session_local_recovery as recovery
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, config, _ = environment
    receipt = {"policy": _peer_refreshed_receipt(config, tmp_path, "acp", safe_mode=safe, ignore_user_config=True)}
    before = json.dumps(receipt, sort_keys=True)
    monkeypatch.setattr(recovery, "owned_receipt", lambda *_a: (receipt, SimpleNamespace(chat_id="saved"), "route", None))
    mutations = []

    def prohibited(*_a):
        mutations.append("compression adoption")
        raise AssertionError("must refuse before adoption")

    monkeypatch.setattr(recovery, "_with_untracked_compression", prohibited)
    with pytest.raises(RuntimeStoreError, match="invalid_params") as refused:
        recovery.restore_local_session(SimpleNamespace(runner=SimpleNamespace(session_store=None)), "saved")
    assert refused.value.field == ("safe_mode" if safe else "ignore_user_config")
    assert mutations == []
    assert json.dumps(receipt, sort_keys=True) == before


@pytest.mark.parametrize("source, flags", [
    ("acp", {}),                                             # protected surface, no bypass: restores as acp
    ("cli", {"ignore_user_config": True}),                   # attended troubleshooting stays restorable
    ("cli", {"safe_mode": True, "ignore_user_config": True}),
])
def test_peer_refreshed_receipts_without_protected_bypass_still_restore(environment, tmp_path, source, flags):
    from gateway.session_policy import restore_policy
    _, config, _ = environment
    stored = _peer_refreshed_receipt(config, tmp_path, source, built=flags)
    restored = restore_policy(stored)
    assert (restored.source, restored.platform) == (source, source)
    assert (restored.safe_mode, restored.ignore_user_config) == (
        flags.get("safe_mode", False), flags.get("ignore_user_config", False))


@pytest.mark.parametrize("params, trusted", [
    ({"source": "acp", "ignore_user_config": True}, False),
    ({"source": "oneshot", "safe_mode": True}, False),
    ({}, True),                                              # an owner-supplied (trusted) policy is checked too
])
def test_local_admission_refuses_protected_bypass_before_staging(environment, params, trusted):
    from gateway.session_local import stage_local_session
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    _, _, ordinary = environment
    trusted_policy = replace(ordinary, source="acp", platform="acp", ignore_user_config=True) if trusted else None
    owner = SimpleNamespace(profile_id="owner")
    actor = SimpleNamespace(profile_id="owner", capabilities={"session:create"})
    with pytest.raises(RuntimeStoreError, match="invalid_params") as refused:
        stage_local_session(owner, actor, params, trusted_policy=trusted_policy)
    assert refused.value.field in {"safe_mode", "ignore_user_config"}


@pytest.mark.parametrize("label, safe", [("acp", True), ("acp", False), ("local", True), ("local", False)])
def test_stored_protected_bypass_is_never_adopted_before_restore(environment, tmp_path, monkeypatch, label, safe):
    """The dispatcher's pre-handler catch-up and the startup sweep (``adopt_before_restore``) write nothing
    for a stored protected bypass receipt, as stored (``acp``) or peer-refreshed (``local``)."""
    from gateway import session_local_adoption as adoption
    from gateway import session_local_recovery as recovery
    from gateway import session_persistence
    _, config, _ = environment
    policy = _peer_refreshed_receipt(config, tmp_path, "acp", safe_mode=safe, ignore_user_config=True)
    policy["source"] = label
    receipt = {"profile_id": "owner", "principal_id": "p", "request_id": "r", "session_id": "saved",
               "route": "route", "entry": None, "policy": policy}
    before = json.dumps(receipt, sort_keys=True)
    adopted = []
    bindings = SimpleNamespace(compression_adoptable=lambda _binding: True,
                               adopt_untracked_compression=lambda **kw: adopted.append(kw))
    monkeypatch.setattr(recovery, "owned_receipt", lambda *_a: (receipt, SimpleNamespace(chat_id="saved"), "route", None))
    monkeypatch.setattr(session_persistence, "authority_persistence", lambda _authority: SimpleNamespace(bindings=bindings))
    asyncio.run(adoption.adopt_before_restore(SimpleNamespace(epoch=1), "saved", tracked=False))
    assert adopted == []
    assert json.dumps(receipt, sort_keys=True) == before


def test_ordinary_receipt_catch_up_still_adopts(environment, tmp_path, monkeypatch):
    """Control for the case above: the same seams adopt an ordinary (attended, non-bypass) receipt."""
    from gateway import session_local_adoption as adoption
    from gateway import session_local_recovery as recovery
    from gateway import session_persistence
    _, config, _ = environment
    receipt = {"profile_id": "owner", "principal_id": "p", "request_id": "r", "session_id": "saved",
               "route": "route", "entry": None, "policy": _peer_refreshed_receipt(config, tmp_path, "acp")}
    adopted = []
    bindings = SimpleNamespace(compression_adoptable=lambda _binding: True,
                               adopt_untracked_compression=lambda **kw: adopted.append(kw))
    monkeypatch.setattr(recovery, "owned_receipt", lambda *_a: (receipt, SimpleNamespace(chat_id="saved"), "route", None))
    monkeypatch.setattr(session_persistence, "authority_persistence", lambda _authority: SimpleNamespace(bindings=bindings))
    asyncio.run(adoption.adopt_before_restore(SimpleNamespace(epoch=1), "saved", tracked=False))
    assert [kw["session_id"] for kw in adopted] == ["saved"]
