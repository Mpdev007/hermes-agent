"""Admission and persisted-policy validation for explicit protected sources."""
from agent.safe_worker_policy import bypass_field
from persistence.core.errors import AuthorityRefusal as RuntimeStoreError
from copy import deepcopy


def validate_protected_bypass(source, safe_mode=False, ignore_user_config=False):
    if field := bypass_field(source, safe_mode, ignore_user_config):
        raise RuntimeStoreError("invalid_params", field=field,
                                detail="protected unattended sources require policy loading")


def refresh_required_tool_policies(config, live):
    """Carry current owner security requirements into a private worker without editing its receipt."""
    plugins = config.get("plugins")
    if plugins is None:
        plugins = config["plugins"] = {}
    if not isinstance(plugins, dict):
        # Do not normalize malformed frozen state and then copy a valid live
        # declaration into it. Refuse bootstrap before any worker can start.
        raise RuntimeStoreError("invalid_params", field="plugins",
                                detail="invalid plugin configuration snapshot")
    current = live.get("plugins")
    if isinstance(current, dict) and "required_tool_policies" in current:
        plugins["required_tool_policies"] = deepcopy(current["required_tool_policies"])
    else:
        plugins.pop("required_tool_policies", None)
