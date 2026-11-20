"""Malformed saved plugin config is a typed, side-effect-free bootstrap refusal."""
from copy import deepcopy

import pytest

from gateway.session_policy_protection import refresh_required_tool_policies
from persistence.core.errors import AuthorityRefusal as RuntimeStoreError


@pytest.mark.parametrize("plugins", [True, "invalid", ["unattended-guard"]])
@pytest.mark.parametrize("live", [{}, {"plugins": {"required_tool_policies": {
    "acp": [{"key": "unattended-guard", "source": "user"}]}}}])
def test_invalid_saved_plugins_is_never_normalized_into_a_live_allow(plugins, live):
    config = {"plugins": plugins, "model": {"default": "offline-fixture"}}
    saved = deepcopy(config)
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        refresh_required_tool_policies(config, live)
    assert config == saved
