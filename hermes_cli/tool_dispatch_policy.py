"""Non-transforming, fail-closed plugin policy at the final handler boundary."""
from __future__ import annotations

from copy import deepcopy
from functools import partial
from hashlib import sha256
import marshal
from typing import Any
from hermes_cli.required_tool_policy import RequiredPolicyUnavailable


class ToolDispatchPolicyBlocked(PermissionError):
    """A final policy veto, distinct from a handler failure."""


def callback_code_fingerprint(callback: Any) -> str | None:
    """Hash the registered Python code object, not a potentially newer file on disk.

    Python version and compile filename affect this identity; compare under the same build.
    Bound policy data has its own fingerprint, supplied by the policy's owner.
    """
    while isinstance(callback, partial):
        callback = callback.func
    code = getattr(callback, "__code__", None)
    return sha256(marshal.dumps(code)).hexdigest() if code is not None else None


def final_tool_block(tool_name: str, args: dict[str, Any], *, expected_profile: str | None = None,
                     **hook_ids: Any) -> str | None:
    """Validate the final name/arguments without lending policy callbacks mutable dispatch data.

    Admission hooks still fire once. These final validators may allow with None or veto with
    a block directive; rewriting, approval and malformed decisions cannot authorize a send.
    """
    try:
        from hermes_cli.lifecycle import invoke_hook

        observed = deepcopy(args)
        original = deepcopy(observed)
        from hermes_cli.required_tool_policy import required_policy_keys, invoke_required_policies
        required = required_policy_keys()
        if required is None:
            results = invoke_hook("pre_tool_dispatch", tool_name=tool_name, args=observed, **hook_ids)
        else:
            from hermes_constants import hermes_home_key
            protected_source, keys = required
            if expected_profile is not None and hermes_home_key(expected_profile) != hermes_home_key():
                raise RequiredPolicyUnavailable("dispatch profile differs from the protected context")
            hook_ids.pop("protected_source", None)
            results = invoke_required_policies(keys, protected_source, tool_name=tool_name, args=observed, **hook_ids)
        if observed != original or not isinstance(results, list):
            return "BLOCKED: final tool policy changed arguments or returned invalid delivery data"
        for result in results:
            if (not isinstance(result, dict) or result.get("action") != "block"
                    or not isinstance(result.get("message"), str) or not result["message"]):
                return "BLOCKED: final tool policy returned an invalid decision"
            return result["message"]
    except RequiredPolicyUnavailable as exc:
        return f"BLOCKED: required tool policy unavailable: {exc}"
    except (Exception, SystemExit):  # health: allow BLE001 -- arbitrary policy delivery failures must return a veto
        return "BLOCKED: final tool policy delivery failed"
    return None
