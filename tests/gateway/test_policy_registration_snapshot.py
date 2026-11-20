"""Policy plugin identifiers survive private snapshots without exempting credentials."""
from copy import deepcopy

import pytest


def test_native_registration_survives_without_credential_authority():
    from gateway.session_policy import _extract_config_secrets
    config = {"plugins": {"required_tool_policies": {
        "webhook": [{"key": "unattended-guard", "source": "user"}]}}}
    original = deepcopy(config)
    _extract_config_secrets(config, None)
    assert config == original


@pytest.mark.parametrize("entry", [
    {"key": "policy", "source": "user", "api_key": "synthetic-only"},
    {"key": "policy"}, {"key": "policy", "source": "unknown"},
])
def test_malformed_registration_does_not_gain_secret_exemption(entry):
    from gateway.session_policy import _extract_config_secrets
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
    config = {"plugins": {"required_tool_policies": {"acp": [entry]}}}
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        _extract_config_secrets(config, None)


def test_ordinary_plugin_key_still_requires_private_authority():
    from gateway.session_policy import _extract_config_secrets
    config = {"plugins": {"entries": {"fixture": {"key": "synthetic-secret"}}}}
    secrets = {}
    _extract_config_secrets(config, secrets)
    assert config["plugins"]["entries"]["fixture"]["key"] is None
    assert secrets == {("plugins", "entries", "fixture", "key"): "synthetic-secret"}


@pytest.mark.parametrize("flag", ["safe_mode", "ignore_user_config"])
def test_webhook_is_an_explicit_protected_worker_source(flag):
    from agent.safe_worker_policy import validate_worker_bypass
    with pytest.raises(ValueError, match="protected source webhook"):
        validate_worker_bypass("webhook", **{flag: True})


def test_webhook_platform_requires_its_own_policy_declaration(monkeypatch):
    from gateway import session_context, session_policy
    from hermes_cli import config
    from hermes_cli.required_tool_policy import required_policy_keys, RequiredPolicyUnavailable
    monkeypatch.setattr(session_context, "get_session_env",
                        lambda key, default="": "webhook" if key == "HERMES_SESSION_PLATFORM" else default)
    monkeypatch.setattr(session_policy, "active_policy", lambda: None)
    monkeypatch.setattr(config, "load_config_readonly", dict)
    with pytest.raises(RequiredPolicyUnavailable, match="not configured for webhook"):
        required_policy_keys()
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"plugins": {"required_tool_policies": {
        "webhook": [{"key": "unattended-guard", "source": "user"}]}}})
    source, keys = required_policy_keys()
    assert source == "webhook" and keys[0].key == "unattended-guard"
