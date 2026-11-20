"""Required final-policy delivery for core-selected unattended sources.

Requirements come from the owning profile (its owner snapshot in a worker), never
tool-call arguments, plugin presence or a declared fingerprint. Empty config vetoes.
"""
from agent.safe_worker_policy import PROTECTED_SOURCES, bypass_field
from dataclasses import dataclass


# A brief wait tolerates an observation reader; exhaustion is still a veto. This
# does not bound discovery itself or the callback's independently configured timeout.
POLICY_LOCK_WAIT_SECONDS = 0.25


@dataclass(frozen=True)
class RequiredPolicy:
    key: str
    source: str


def _requirements(entries, source):
    if not isinstance(entries, list) or not entries or len(entries) > 8:
        raise RequiredPolicyUnavailable(f"required tool policies not configured for {source}")
    required = []
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"key", "source"}
                or not isinstance(entry["key"], str) or not entry["key"]
                or entry["key"] != entry["key"].strip() or len(entry["key"]) > 128
                or entry["source"] not in ("bundled", "user", "project", "entrypoint")):
            raise RequiredPolicyUnavailable("required policy needs an explicit key and native source pin")
        required.append(RequiredPolicy(entry["key"], entry["source"]))
    if len({entry.key for entry in required}) != len(required):
        raise RequiredPolicyUnavailable("required policy keys must be unique")
    return tuple(required)


class RequiredPolicyUnavailable(PermissionError):
    pass


def required_policy_keys():
    from agent.safe_worker_policy import worker_source, worker_bypass_enabled
    from gateway.session_policy import active_policy
    from gateway.session_context import get_session_env

    policy = active_policy()
    worker = worker_source()
    source = getattr(policy, "source", None) or worker
    if worker in PROTECTED_SOURCES and policy is None:
        raise RequiredPolicyUnavailable("protected worker has no active session policy")
    if (worker is not None and policy is not None and worker != source
            and (worker in PROTECTED_SOURCES or source in PROTECTED_SOURCES)):
        raise RequiredPolicyUnavailable("worker and session protection sources differ")
    truthy = {"1", "true", "yes", "on"}
    if str(get_session_env("HERMES_CRON_SESSION", "")).strip().lower() in truthy:
        source = "cron"
    elif str(get_session_env("HERMES_SINGLE_QUERY_SESSION", "")).strip().lower() in truthy:
        source = "oneshot"
    elif source not in PROTECTED_SOURCES:
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        if platform in PROTECTED_SOURCES:
            source = platform
    if source not in PROTECTED_SOURCES:
        return None
    if (bypass_field(source, getattr(policy, "safe_mode", False), getattr(policy, "ignore_user_config", False))
            or worker_bypass_enabled()):
        raise RequiredPolicyUnavailable("protected source refuses configuration bypass")
    # Security requirements belong to the owning profile, not a session's old launch
    # snapshot. In a managed worker this reader uses the owner's bootstrap snapshot.
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    plugins = config.get("plugins")
    required = plugins.get("required_tool_policies") if isinstance(plugins, dict) else None
    entries = required.get(source) if isinstance(required, dict) else None
    return source, _requirements(entries, source)


def invoke_required_policies(keys, protected_source, **payload):
    """Run a complete profile's actual owned callbacks and require successful delivery.

    No discovery is started here. A startup/reload that has not finished is a veto,
    including re-entrant calls from inside a plugin's own register() function.
    """
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.middleware import OBSERVER_SCHEMA_VERSION
    from hermes_constants import hermes_home_key
    payload["protected_source"] = protected_source
    payload.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)

    manager = get_plugin_manager()
    if (manager.scope_key != hermes_home_key()
            or not manager._discovery_lock.acquire(timeout=POLICY_LOCK_WAIT_SECONDS)):
        raise RequiredPolicyUnavailable("policy scope unavailable or discovery in progress")
    try:
        if not manager._discovery_complete:
            raise RequiredPolicyUnavailable("policy discovery is incomplete")
        callbacks = tuple(manager._hooks.get("pre_tool_dispatch", ()))
        required = []
        owners = {}
        for requirement in keys:
            key = requirement.key
            plugin = manager._plugins.get(key)
            owned = [r for r in manager._ownership_ledger.get(key, ())
                     if r.active and r.kind == "hook" and r.key == "pre_tool_dispatch"
                     and r.callback is not None and any(r.callback is cb for cb in callbacks)]
            if plugin is None or not plugin.enabled or plugin.error or plugin.deferred or not owned:
                raise RequiredPolicyUnavailable("required policy is absent, disabled or failed to load")
            # source is assigned by native directory/entry-point discovery, not the
            # manifest's self-declared policy fingerprint or its displayed name.
            if plugin.manifest.source != requirement.source:
                raise RequiredPolicyUnavailable("required policy native source differs from its pin")
            required.extend(owned)
            owners[key] = (plugin, requirement.source)
    finally:
        manager._discovery_lock.release()
    called = []
    results = manager._run_hook_callbacks("pre_tool_dispatch", callbacks, payload, called=called)
    if not all(any(r.callback is ran for ran in called) for r in required):
        raise RequiredPolicyUnavailable("required policy did not complete its decision")
    if not manager._discovery_lock.acquire(timeout=POLICY_LOCK_WAIT_SECONDS):
        raise RequiredPolicyUnavailable("policy changed during delivery")
    try:
        if (not manager._discovery_complete
                or any(manager._plugins.get(key) is not owner or not owner.enabled or owner.error
                       or owner.deferred or owner.manifest.source != source
                       for key, (owner, source) in owners.items())
                or any(not r.active or not any(r.callback is cb for cb in manager._hooks.get("pre_tool_dispatch", ())) for r in required)):
            raise RequiredPolicyUnavailable("required policy was removed during delivery")
    finally:
        manager._discovery_lock.release()
    return results
