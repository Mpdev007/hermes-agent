"""``plugins.list`` wire contract: the owner's ``CANONICAL_METHODS`` override matches the authenticated,
profile-bound handler (``gateway/session_discovery.list_plugins``) for normal and observe-only reads, and
the standalone ``METHODS`` wire is unchanged. Separate file, so a failure here cannot take the existing
observation cases (``tests/hermes_cli/test_tool_policy_observation.py``) with it."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_tool_policy_observation import policy_manager  # noqa: F401  (fixture)


class _NoStore:
    """``plugins.list`` reads the plugin manager only: any persistence use fails the case."""

    def __getattr__(self, name):
        raise AssertionError(f"plugins.list touched persistence: {name}")


def _dispatch(home, tmp_path, params):
    """A real ``AuthorityConnection.dispatch`` (contract checks, owner scope, handler) over a stub authority:
    no session store is opened, so no store lock/holder code runs under the offline fence."""
    from gateway.session_controls import AuthorityConnection
    authority = SimpleNamespace(instance_id="owner", profile_id=str(home), epoch=1, events={}, runner=None,
                                sessions={}, retiring=False, persistence=_NoStore())
    connection = AuthorityConnection(authority, object(), {"user_id": "viewer"})
    return asyncio.run(connection.dispatch({"id": 1, "method": "plugins.list", "params": params}))


@pytest.mark.parametrize("params", [{}, {"observe_only": True}])
def test_plugins_list_dispatch_conforms_to_its_strict_wire_contract(policy_manager, tmp_path, monkeypatch, params):  # noqa: F811
    """Through ``AuthorityConnection.dispatch`` with the contract's params/result checks raising, not logging."""
    from tui_gateway.contracts import registry
    monkeypatch.setattr(registry, "STRICT", True)
    home, _, _ = policy_manager
    reply = _dispatch(home, tmp_path, params)
    assert "result" in reply, reply
    result = reply["result"]
    row = next(p for p in result["plugins"] if p["name"] == "policy-fixture")
    assert row["policy_evidence"][0]["policy_sha256"] == "a" * 64
    assert result["policy_observation"]["schema"] == "hermes.plugin-policy-observation/v1"
    assert result["policy_observation"]["start_time"] == 12345


@pytest.mark.parametrize("params", [{"observe_only": "true"}, {"extra": True}])
def test_plugins_list_dispatch_refuses_what_the_handler_refuses(policy_manager, tmp_path, monkeypatch, params):  # noqa: F811
    """Same code, message and reason as the handler's own refusal; the override only adds ``fields``."""
    from tui_gateway.contracts import registry
    monkeypatch.setattr(registry, "STRICT", True)
    home, _, _ = policy_manager
    error = _dispatch(home, tmp_path, params)["error"]
    assert (error["code"], error["message"], error["data"]["reason"]) == (4001, "invalid_params", "invalid_params")


@pytest.mark.parametrize("params", [{"observe_only": None}, {"observe_only": "true"}, {"observe_only": 1},
                                    {"observe_only": True, "extra": True}, {"profile": "x"}])
def test_plugins_list_owner_contract_is_as_strict_as_the_handler(params):
    from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems
    assert canonical_param_problems(CANONICAL_METHODS["plugins.list"], params)
    for accepted in ({}, {"observe_only": True}, {"observe_only": False}):
        assert canonical_param_problems(CANONICAL_METHODS["plugins.list"], accepted) == []


def test_standalone_plugins_list_wire_is_not_widened():
    """The TUI sidecar's ``plugins.list`` (``tui_gateway/methods_tools.py``) still answers ``4000`` for the key."""
    from tui_gateway.contracts.registry import METHODS, validate_params
    from tui_gateway.contracts.tools_mcp_plugins import PluginsListParams, PluginsListResult
    assert METHODS["plugins.list"].params is PluginsListParams
    assert METHODS["plugins.list"].result is PluginsListResult
    params, problem = validate_params(METHODS["plugins.list"], {"observe_only": True})
    assert params is None and "observe_only" in problem
    assert set(PluginsListParams.model_fields) == set()


def test_plugins_list_owner_result_shape_is_exact():
    """Pure model check, no gateway: the exact keys ``list_plugins`` returns validate, and nothing more."""
    from pydantic import ValidationError
    from tui_gateway.contracts.registry import CANONICAL_METHODS
    model = CANONICAL_METHODS["plugins.list"].result
    row = {"name": "p", "key": "user:p", "version": "1", "source": "user", "enabled": True, "tools": 0, "hooks": 1,
           "commands": 0, "error": "", "policy_evidence": [{"hook": "pre_tool_dispatch", "policy_sha256": "a" * 64,
                                                             "policy_fingerprint_kind": "plugin_declared",
                                                             "callback_code_sha256": "b" * 64}]}
    observation = {"schema": "hermes.plugin-policy-observation/v1", "pid": 1, "start_time": None,
                   "discovery_complete": True, "profile_id": "/x", "workers_attested": False}
    result = {"plugins": [row], "policy_observation": observation, "toolsets": [], "portable_mcp_servers": []}
    model.model_validate(result)
    for bad in ({**result, "policy_observation": {**observation, "extra": 1}},
                {**result, "plugins": [{**row, "policy_evidence": [
                    {**row["policy_evidence"][0], "policy_fingerprint_kind": "other"}]}]},
                {**result, "plugins": [{**row, "extra": 1}]},
                {key: value for key, value in result.items() if key != "policy_observation"}):
        with pytest.raises(ValidationError):
            model.model_validate(bad)
