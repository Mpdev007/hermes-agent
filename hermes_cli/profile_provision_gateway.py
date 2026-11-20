"""``hermes profile create`` through the running gateway: one ``profiles.create`` RPC, one receipt.

The CLI asks the gateway that serves its profile to make the profile (``HERMES_TUI_GATEWAY_URL``, the dial
``connect_gateway`` honours for every client, names a remote one), so a terminal attached to a gateway
creates profiles on the gateway's machine. The gateway answers with the same
:class:`~hermes_cli.profile_provision.ProfileCreateReceipt` a local create builds, so the output does not depend
on where the profile was made.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from hermes_cli.profile_provision import (
    ProfileCreateReceipt, ProfileCreateRequest, message_from, redact_create_text, refusal_from,
)

logger = logging.getLogger(__name__)

#: Skill seeding runs a subprocess (60 s cap) and the multiplexer rescan answer waits up to 8 s.
CREATE_RPC_TIMEOUT = 300
#: Answered before the handler runs, so nothing was made there. 4000: a gateway that predates these
#: fields (unknown param); 5035: one retiring between discovery and the request.
_OLDER_OR_RETIRING = (4000, 5035)
#: Refused before any handler ran, by code AND ``data.reason`` (a handler failure is 5062 whatever its text):
#: the authority socket's routing (4001 ``profile_mismatch``: a profile this gateway doesn't serve, or a
#: profile-bound grant naming another) and ``ws_legacy_fallback`` (4064 ``profile_unavailable``; a bare 4064
#: is not this: the dispatcher also answers it for a config outage raised after the profile was made).
_PROFILE_REFUSED = ((4001, "profile_mismatch"), (4064, "profile_unavailable"))
#: The authority socket's refusal of a draining owner, before routing: like a retiring gateway.
_DRAINING = (4001, "runtime_draining")


def _refusal_key(exc) -> tuple:
    return exc.code, exc.data.get("reason")


class GatewayCreateError(RuntimeError):
    """The named gateway cannot take the create, or took it and its outcome is unknown."""


def _wire(request: ProfileCreateRequest, profile: Optional[str], *, defer_default_plugins: bool = False) -> dict[str, Any]:
    # ``no_alias`` always: the wrapper belongs to the terminal's machine, PATH and ``hermes``
    # (written here when the profile landed in this machine's profiles directory). ``profile``: a URL's
    # connection carries no profile-bound ticket, so the terminal's profile is named (Desktop's routing).
    wire = {"name": request.name, "description": request.description, "clone_from": request.clone_from,
            "clone_all": request.clone_all, "clone_config": request.clone_config,
            "clone_channels": request.clone_channels, "no_skills": request.no_skills,
            "no_alias": True, "sync_imports": request.sync_imports,
            "mirror_credentials": request.mirror_credentials, "share_auth": request.share_auth, "report": True}
    if defer_default_plugins:
        # Port adaptation A5: this invocation's default-only maintenance (explicit, pending or carried) crosses
        # the owning gateway. It only defers that gateway's automatic every-home installer for this create. A
        # gateway that predates the key refuses it (4000) before creating; A7.1: that refusal cannot be told
        # from another unknown key, so the create is unsupported there (local or URL) and the restriction is
        # never dropped. Only an owner without the method lets the create run here, under this process's check.
        wire["defer_default_plugins"] = True
    return {**wire, "profile": profile} if profile else wire


def _where(remote: str) -> str:
    """How messages name the gateway. A URL ``connect_gateway`` would refuse (not ws/wss with a host) or
    whose port is malformed is a :class:`GatewayCreateError` here; any other URL is dialed. It is named as
    ``scheme://host[:port]`` (``safe_proxy_url_for_log``) or, when that can't be shown safely (an ``@`` past
    the authority), by the variable: never with its token, password or path."""
    if not remote:
        return "the running gateway"
    try:
        parts = urlsplit(remote)
        ok = parts.scheme in ("ws", "wss") and bool(parts.hostname) and (parts.port is None or parts.port > 0)
    except ValueError:  # an out-of-range port or a malformed authority; its message quotes the authority
        ok = False
    if not ok:
        raise GatewayCreateError("HERMES_TUI_GATEWAY_URL is not a ws:// or wss:// URL with a host; nothing was created")
    from gateway.platforms.base_log_safety import safe_proxy_url_for_log  # a URL only: it loads gateway.platforms
    named = safe_proxy_url_for_log(remote)
    return f"the gateway at {named}" if named.startswith(("ws://", "wss://")) else "the HERMES_TUI_GATEWAY_URL gateway"


def _local_profile_dir(name: str) -> Optional[Path]:
    from hermes_cli.profiles import get_profile_dir
    try:
        return Path(get_profile_dir(name))
    except ValueError:
        return None


def _is_profile(local: Optional[Path]) -> bool:
    """Whether ``-p <name>``'s directory here is a profile: it carries an identity marker. A shell a deleted
    profile left behind carries none (``create_profile`` replaces it), and nothing else creates one."""
    from hermes_constants import named_profile_has_identity
    return local is not None and local.is_dir() and named_profile_has_identity(local)


def _made_here(made: ProfileCreateReceipt, local: Optional[Path], was_profile: bool) -> bool:
    """Whether this create made the profile ``-p <name>`` selects on this machine, given the gateway is this
    home's owner: not a profile before the request, one now, at the path the gateway reports."""
    from hermes_cli.profiles import profile_matches_home
    return (not was_profile and _is_profile(local)
            and profile_matches_home(local.name, Path(made.path)))


def _bounded_code(value) -> Optional[str]:
    return value if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 64 else None


def _live_owner_or_none():
    """An unrestricted create's owner: the ready-or-None helper ``sessions_owner.live_owner``, where a failed
    discovery is also None (the create then runs here, as it always has)."""
    from hermes_cli.sessions_owner import live_owner
    try:
        return live_owner()
    except Exception:  # health: allow BLE001 -- an ordinary create keeps its local fallback on any discovery failure
        logger.debug("profile create: gateway discovery failed", exc_info=True)
        return None


def _restricted_local_owner():
    """Port adaptation A7.2 (Hermes 49979351): the native discovery verdict for this home, where ordinary creates
    and ``sessions prune`` keep the ready-or-None helper (``sessions_owner.live_owner``). A ready owner's endpoint;
    ``None`` only for confirmed absence. A starting (an active start reservation), draining, incompatible,
    conflicting or inaccessible owner (a control timeout or refused access included) is uncertainty: a
    :class:`GatewayCreateError` naming its bounded state and code (never the detail), with nothing created."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_constants import get_hermes_home
    try:
        found = discover_gateway_endpoint(get_hermes_home().resolve())
    except Exception as exc:  # health: allow BLE001 -- discovery raised: no verdict, so never a local create
        raise GatewayCreateError("could not tell whether a running gateway owns this profile directory "
                                 "(discovery_failed), so nothing was created under default-only maintenance; "
                                 "retry") from exc
    if found.state == "ready" and found.endpoint is not None:
        return found.endpoint
    if found.state == "absent":
        return _shared_owner_endpoint(get_hermes_home().resolve())
    code = _bounded_code(found.reason_code)
    raise GatewayCreateError(f"the gateway for this profile directory is {found.state}{f' ({code})' if code else ''}, "
                             "so nothing was created under default-only maintenance; retry")


def _shared_owner_endpoint(profile_home: Path):
    """Port adaptation A7.3 (Hermes fa0015f0): after native discovery reads *profile_home* absent, the other owner a
    named profile can have. A ``<root>/profiles/<name>`` home may be served by the root's shared (multiplexing) gateway,
    and discovery reads a root timeout or error as "not served" and then decides from the named home's lock alone.
    A restricted create keeps that uncertainty: a local create needs a proven standalone profile, a ready and valid
    shared root whose complete served list omits this home (A7.4), or both possible owners confirmed absent. A shared owner that serves it now is
    returned (the create goes there); anything else is a :class:`GatewayCreateError` with nothing created. Ordinary
    creates and native discovery are unchanged."""
    from hermes_cli.gateway_runtime import _canonical_home, _endpoint, _overlay_owner_home
    from hermes_cli.gateway_runtime_discovery import missing_owner_state, query_identify
    from hermes_cli.gateway_runtime_multiplex import multiplexer_root_for
    home = _overlay_owner_home(Path(_canonical_home(profile_home)))  # as discover_gateway_endpoint resolves it
    root = multiplexer_root_for(home)
    if root is None:
        return None  # not a named profile: no shared owner, the direct verdict stands
    from hermes_cli.gateway_multiplex_mode import host_serves_named_profile
    if host_serves_named_profile(root, home) is False:
        return None  # a proven standalone profile (gateway.standalone: true) is never served by the root
    uncertain = ("could not tell whether the shared gateway for these profiles serves this one ({}), so nothing was "
                 "created under default-only maintenance; retry")
    try:
        payload = query_identify(root, timeout=2.0)
    except (FileNotFoundError, ConnectionRefusedError):
        state = missing_owner_state(root)  # absent, starting (an active start reservation) or inaccessible
        if state == "absent":
            return None  # both possible owners confirmed absent
        raise GatewayCreateError(uncertain.format(f"shared_{state}")) from None
    except Exception as exc:  # health: allow BLE001 -- a timeout, refusal or invalid reply is no verdict
        raise GatewayCreateError(uncertain.format("shared_unreadable")) from exc
    served = _endpoint(payload, home, control_home=root)
    if served.state == "ready" and served.endpoint is not None:
        return served.endpoint
    # Port adaptation A7.4 (Hermes 8c2c45fd finding 1): an omitted home is "does not serve it" only from an owner
    # proven ready first. ``_endpoint`` looks the requested home up before it validates state, authority or origin,
    # so an omitted home read ``profile_mismatch`` from ANY reply: a root starting with no rows, or draining with only
    # its own row (the native identify producer emits both). The root's own row is validated by the same native
    # check (gateway/run_runtime.py:196-213 and session_authorities.py:135-136 list the launch home first), then the
    # served list must be complete and unambiguous, and the parked check still applies.
    owner = _endpoint(payload, root, control_home=root)
    if owner.state != "ready" or owner.endpoint is None:
        code = _bounded_code(owner.reason_code)
        raise GatewayCreateError(uncertain.format(f"shared_root_{owner.state}{f'_{code}' if code else ''}"))
    if (served.state == "inaccessible" and served.reason_code == "profile_mismatch"
            and _served_list_omits(payload.get("served_profiles"), home) and not _parked_here(payload, home)):
        return None
    code = _bounded_code(served.reason_code)
    raise GatewayCreateError(uncertain.format(f"shared_{served.state}{f'_{code}' if code else ''}"))


def _served_list_omits(profiles, home: Path) -> bool:
    """A7.4: a complete, unambiguous served list that names *home* nowhere. Every row is an object with a non-empty
    ``home`` and ``profile_id`` string (the native shape, session_authorities.py:135-136) and no two rows name one
    home; an unreadable row is no verdict."""
    from hermes_cli.gateway_runtime import _canonical_home
    if not isinstance(profiles, list):
        return False
    seen: set[str] = set()
    for row in profiles:
        if not (isinstance(row, dict) and isinstance(row.get("home"), str) and row["home"]
                and isinstance(row.get("profile_id"), str) and row["profile_id"]):
            return False
        try:
            canonical = _canonical_home(row["home"])
        except (OSError, RuntimeError, ValueError):
            return False
        if canonical in seen:
            return False
        seen.add(canonical)
    return _canonical_home(home) not in seen


def _parked_here(payload: dict, home: Path) -> bool:
    """Whether the shared owner names this home among the profiles it could not serve (``parked_profiles``)."""
    from hermes_cli.profiles import normalize_profile_name
    parked = payload.get("parked_profiles")
    if parked is None:
        return False
    if not isinstance(parked, dict):
        return True  # malformed: unproven, so uncertain
    name = normalize_profile_name(home.name)
    return any(normalize_profile_name(str(n)) == name for n in parked)


async def _described_instance(client) -> Optional[str]:
    """The ``instance_id`` the connected gateway reports (``runtime.describe``), or None when it doesn't."""
    try:
        described = await client.rpc("runtime.describe")
    except Exception:  # an older or refusing gateway: its identity is unproven, so no wrapper
        logger.debug("profile create: runtime.describe failed", exc_info=True)
        return None
    instance = described.get("instance_id") if isinstance(described, dict) else None
    return instance if isinstance(instance, str) and instance else None


def create_or_provision(request: ProfileCreateRequest, provision) -> ProfileCreateReceipt:
    """The profile create the CLI runs: the owning gateway's (:func:`create_through_gateway`), else *provision*
    (``profile_provision.provision_profile``) here.

    Port adaptation A7.3 (Hermes fa0015f0): a restriction observed by either reading of this one operation (the
    caller's, before dispatch, or the dispatcher's own) holds through the local provision and its installer decision,
    so it cannot be lost when the pending obligation ends during the round trip. A restriction armed later still
    applies (``default_plugins.installs_deferred`` keeps reading it). One dispatch, no retry."""
    from hermes_cli.default_plugins import installs_deferred
    from hermes_cli.maintenance_scope import deferring_every_home_installs
    observation: dict = {}
    with deferring_every_home_installs(installs_deferred()):
        made = _dispatch_create(request, observation)
        if made is not None:
            return made
        with deferring_every_home_installs(observation.get("deferred") is True):
            return ProfileCreateReceipt.of(request, provision(request))


def create_through_gateway(request: ProfileCreateRequest) -> Optional[ProfileCreateReceipt]:
    """:func:`_dispatch_create` without reporting its deferral reading (callers that only dispatch)."""
    return _dispatch_create(request, {})


def _dispatch_create(request: ProfileCreateRequest, observation: dict) -> Optional[ProfileCreateReceipt]:
    """The receipt of the gateway's create for *request*, or None to create here. Its own deferral reading is put in
    ``observation["deferred"]`` before any owner is chosen (A7.3: :func:`create_or_provision` carries it).

    ``HERMES_TUI_GATEWAY_URL`` set: that gateway makes the profile, in this terminal's profile, and anything
    short of its answer is a :class:`GatewayCreateError`, never a local create. Otherwise a live gateway for
    this profile (discovery only; one is never started for this) makes it; with none, or one that predates
    the fields this sends, the caller creates locally. A refused create raises the gateway's own
    ``ValueError`` / ``FileExistsError`` / ``FileNotFoundError``. When this create made the profile in this
    machine's profiles directory, the terminal's ``~/.local/bin`` wrapper is written here.
    """
    remote = os.environ.get("HERMES_TUI_GATEWAY_URL", "").strip()
    where = _where(remote)
    endpoint, profile = None, None
    if remote:
        # The profile this terminal runs in, resolved like the local dial (an overlay task home is served by
        # the profile its lock names); a home outside the profiles tree has no name a gateway can resolve.
        from hermes_cli.gateway_runtime import gateway_profile_home
        from hermes_constants import get_hermes_home, profile_name_for_home
        profile = profile_name_for_home(gateway_profile_home(get_hermes_home()))
        if not profile:
            raise GatewayCreateError(f"this terminal's HERMES_HOME is not a named profile, so {where} cannot "
                                     "create from it; nothing was created")
    from hermes_cli.default_plugins import installs_deferred
    # A5: read here, in the invocation that holds the scope; A7.3: reported to :func:`create_or_provision`, which
    # carries it (or the caller's earlier reading) across a local create and its installer decision.
    defer = installs_deferred()
    observation["deferred"] = defer
    if not remote and defer:
        endpoint = _restricted_local_owner()  # None only for confirmed absence
    elif not remote or request.alias:
        endpoint = _live_owner_or_none()
    if not remote and endpoint is None:
        return None  # this process creates it (a restricted create: under its own restriction)
    from hermes_cli.gateway_client import GatewayRPCError, NOT_SENT, connect_gateway
    machine = " on the gateway's machine" if remote else ""
    unknown = f"{where} may have created '{request.name}'; check `hermes profile list`{machine} before retrying"
    local = _local_profile_dir(request.name)
    was_profile = _is_profile(local)
    # A discovered owner serves this home. A URL's gateway is this home's owner only if it reports the
    # instance this home's discovery names: path equality is no proof (another machine with the same home
    # layout reports the same path while a local create of the name runs), nor is a loopback URL.
    ours = not remote
    owner_here = endpoint.instance_id if remote and endpoint is not None else None
    attached = False

    async def _run():
        nonlocal attached, ours
        async with connect_gateway(None if remote else endpoint) as client:
            attached = True
            if owner_here is not None:
                ours = await _described_instance(client) == owner_here
            return await client.rpc("profiles.create", _timeout=CREATE_RPC_TIMEOUT,
                                    **_wire(request, profile, defer_default_plugins=defer))

    try:
        answer = asyncio.run(_run())
    except KeyboardInterrupt:
        if not attached:
            raise
        raise GatewayCreateError(f"interrupted; {unknown}") from None
    except GatewayRPCError as exc:
        draining = _refusal_key(exc) == _DRAINING
        unknown_method = exc.unknown_method and exc.code in (None, -32601)
        if exc.code in _OLDER_OR_RETIRING or unknown_method or draining:
            # Port adaptation A7/A7.1: under the restriction only a refusal that PROVES the selected, authenticated
            # owner predates this request lets it run here: an unknown method. A 4000 (unknown key) reaches this
            # client as ``(request_failed, 4000, {})`` (``rpc_error`` keeps no free text), so it cannot show the
            # refused key is the restriction's rather than one an older owner also lacks (``clone_config``,
            # ``report``, ``sync_imports``): it is unsupported. A retiring or draining owner proves nothing either.
            predates = exc.code == 4000 or unknown_method
            if remote:
                if defer and predates:
                    # The restriction is never dropped, replayed without it, or sent to another destination.
                    raise GatewayCreateError(
                        f"{where} does not support the default-only maintenance restriction this create carries "
                        "(unsupported); nothing was created and nothing was retried elsewhere") from exc
                why = "is restarting; retry" if exc.code == 5035 or draining else (
                    "predates this version; update it, or unset HERMES_TUI_GATEWAY_URL to create locally")
                raise GatewayCreateError(f"{where} cannot create profiles for this terminal (it {why})") from exc
            # A7.2: the matched rejection is the -32601 every native owner answers for a method it lacks
            # (tui_gateway/rpc_dispatch.py:23, gateway/session_controls.py:387-392); a reason with no code is not
            # one any owner sends, so it proves nothing either.
            if defer and (exc.code == 4000 or (unknown_method and exc.code != -32601)):
                raise GatewayCreateError(
                    f"{where} does not support the default-only maintenance restriction this create carries "
                    "(unsupported); nothing was created; update it, or create the profile after the maintenance"
                ) from exc
            if defer and not unknown_method:
                raise GatewayCreateError(f"{where} is restarting; nothing was created; retry") from exc
            return None  # an unknown method (proof), or an unrestricted create: this process creates it
        refused = refusal_from(exc.data)
        if refused is not None:
            raise refused from exc
        if _refusal_key(exc) in _PROFILE_REFUSED:
            raise GatewayCreateError(f"{where} cannot serve this terminal's profile "
                                     f"({profile or 'its profile'}); nothing was created") from exc
        # Not a refusal of the request: the handler failed, possibly after the profile was made.
        reason = message_from(exc.data) or redact_create_text(exc)
        raise GatewayCreateError(f"{where} did not finish the create ({reason}); {unknown}") from exc
    except Exception as exc:
        if not attached or str(exc) == NOT_SENT:  # the dial, ticket or handshake failed: nothing was sent
            if remote:
                raise GatewayCreateError(f"could not reach {where} ({type(exc).__name__})") from exc
            if defer:
                # A7: a failed dial, ticket, identity or protocol check is not the owner's refusal; no local create.
                raise GatewayCreateError(f"could not reach {where} ({type(exc).__name__}); nothing was created "
                                         "under default-only maintenance; retry") from exc
            logger.info("profile create: the running gateway is unreachable (%s); creating locally", exc)
            return None
        raise GatewayCreateError(f"lost {where} during the create ({type(exc).__name__}); {unknown}") from exc
    made = ProfileCreateReceipt.from_wire(answer.get("receipt") if isinstance(answer, dict) else None)
    if made is None:
        # It made a profile without a full, well-typed receipt (``--clone`` / ``--sync-imports`` may be ignored).
        raise GatewayCreateError(f"{where} answered without the details this version needs; {unknown}")
    made = dataclasses.replace(made, warnings=[redact_create_text(w) for w in made.warnings])
    here = request.alias and ours and _made_here(made, local, was_profile)
    return _with_local_alias(made, request) if here else made


def _with_local_alias(made: ProfileCreateReceipt, request: ProfileCreateRequest) -> ProfileCreateReceipt:
    from hermes_cli.profile_provision import write_alias
    try:
        path, collision = write_alias(request.name)
    except Exception as exc:  # the profile exists: a wrapper failure is a warning, as for a local create
        logger.warning("profile %s: alias wrapper failed", request.name, exc_info=True)
        return dataclasses.replace(made, warnings=[*made.warnings, f"alias wrapper: {exc}"])
    return dataclasses.replace(made, alias_path=str(path) if path else None, alias_collision=collision)
