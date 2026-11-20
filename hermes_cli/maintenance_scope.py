"""Profile-data maintenance selection, NOT a permission or dependency boundary.

The checkout, products and PM dependency union are always shared. A default-only
selection is bound to the actual default home, serialized across native handoffs,
and retained in the native pending obligation. Context-local propagation covers
in-process maintenance children without changing the meaning of HERMES_HOME.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import sys

# Target-tree capability: understood by source updates before moving any code.
MAINTENANCE_SCOPE_SCHEMA = 1

# Port adaptation A4: the record venv_sync.relaunch_command carries into the managed-interpreter
# child it starts (beside ``sys._hermes_relaunched``; never an environment variable, so processes
# that child spawns are separate starts).
RELAUNCH_SCOPE_ATTRIBUTE = "_hermes_maintenance_scope"
RELAUNCH_SCOPE_SCHEMA = 1


class ScopeSafetyRefusal(ValueError):
    """Invalid or unprovable maintenance authority; never a dependency fallback.

    This module and this type are bootstrap-safe (stdlib imports only).
    """


@dataclass(frozen=True)
class MaintenanceScope:
    mode: str = 'all'
    home: str | None = None

    @classmethod
    def select(cls, mode: str = 'all') -> MaintenanceScope:
        home = None
        if mode == 'default':
            from hermes_constants import get_default_hermes_root
            home = str(get_default_hermes_root().resolve())
        scope = cls(mode, home)
        scope.validate()
        return scope

    @classmethod
    def from_dict(cls, value: dict) -> MaintenanceScope:
        if not isinstance(value, dict) or set(value) != {'mode', 'home'}:
            raise ScopeSafetyRefusal('invalid maintenance scope record')
        scope = cls(value['mode'], value['home'])
        scope.validate()
        return scope

    def validate(self) -> None:
        if self.mode not in ('all', 'default'):
            raise ScopeSafetyRefusal('maintenance scope must be all or default')
        if self.mode == 'all':
            if self.home is not None:
                raise ScopeSafetyRefusal('all-profile scope cannot bind an arbitrary home')
            return
        import sys
        if sys.platform == "win32":
            raise ScopeSafetyRefusal("default-only maintenance is not supported by Windows recursive ACL repair")
        from hermes_constants import get_default_hermes_root, get_process_hermes_home, get_hermes_home
        from pm.environments import dependency_home_root
        root = get_default_hermes_root().resolve()
        if (self.home != str(root) or get_process_hermes_home().resolve() != root
                or get_hermes_home().resolve() != root or dependency_home_root().resolve() != root):
            raise ScopeSafetyRefusal('default maintenance requires the canonical default profile and matching data root')

    def to_dict(self) -> dict:
        return {'mode': self.mode, 'home': self.home}

    def homes(self, homes) -> list[Path]:
        self.validate()
        return [h for h in homes if self.mode == 'all' or Path(h).resolve() == Path(self.home)]

    def cli_args(self) -> list[str]:
        return ['--maintenance-scope', self.mode]


_current: ContextVar[MaintenanceScope] = ContextVar('update_maintenance_scope', default=MaintenanceScope())


def current_scope() -> MaintenanceScope:
    scope = _current.get()
    scope.validate()
    return scope


@contextmanager
def maintenance_scope(scope: MaintenanceScope):
    scope.validate()
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


def pending_scope(root: Path) -> MaintenanceScope | None:
    from hermes_cli.venv_sync import completion_pending_path
    try:
        text = completion_pending_path(root).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ScopeSafetyRefusal("cannot read pending maintenance scope") from exc
    # The only old marker payload. Corruption is never interpreted as permission
    # to widen a scoped obligation.
    if text == 'source update tail not finished\n':
        return MaintenanceScope()
    try:
        value = json.loads(text)
        if value['schema'] != 1:
            raise ScopeSafetyRefusal('unsupported pending maintenance schema')
        return MaintenanceScope.from_dict(value['maintenance_scope'])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ScopeSafetyRefusal('invalid pending maintenance scope') from exc


def publication_scope(root: Path, requested=None) -> MaintenanceScope:
    """Resolve the restriction without interpreting a journal outside its lock.

    Used at the native PM request seam and again inside the install lock. A
    carried default restriction cannot be widened by a missing/changed marker.
    """
    pending = pending_scope(root)
    if requested is None:
        active = current_scope()
        scope = active if active.mode == "default" else (pending or active)
    elif isinstance(requested, MaintenanceScope):
        scope = requested
    elif isinstance(requested, dict):
        scope = MaintenanceScope.from_dict(requested)
    else:
        scope = MaintenanceScope.select(requested)
    scope.validate()
    if scope.mode == "default":
        from pm.environments import owning_home_root
        if owning_home_root(root) is not None:
            raise ScopeSafetyRefusal("default-only maintenance cannot mutate a borrowed checkout")
    if pending is not None and pending.mode == 'default' and pending != scope:
        raise ScopeSafetyRefusal('cannot broaden a pending default-only maintenance obligation')
    if current_scope().mode == 'default' and current_scope() != scope:
        raise ScopeSafetyRefusal('cannot broaden active default-only maintenance')
    return scope


def resolve_scope(root: Path, requested=None) -> MaintenanceScope:
    scope = publication_scope(root, requested)
    if scope.mode == 'default':
        from pm.environments import install_state_dir
        if (install_state_dir(root) / 'publication.json').exists():
            raise ScopeSafetyRefusal('pending shared dependency publication needs recovery before scoped maintenance')
    return scope


# Port adaptation A4: the default-only scope this invocation was admitted under (hermes_bootstrap's
# preflight). Process-wide and narrowing-only: never set to all-profile, never cleared.
_admitted: MaintenanceScope | None = None


def _carried_scope(root: Path) -> MaintenanceScope | None:
    """The default-only record a managed-interpreter relaunch carried into this process, validated
    again here: exact shape, this checkout, the canonical default home and matching data roots.
    Anything else refuses; a malformed or foreign handoff never reads as all-profile permission."""
    carried = getattr(sys, RELAUNCH_SCOPE_ATTRIBUTE, None)
    if carried is None:
        return None
    if (not isinstance(carried, dict) or set(carried) != {"schema", "root", "maintenance_scope"}
            or type(carried["schema"]) is not int or carried["schema"] != RELAUNCH_SCOPE_SCHEMA
            or not isinstance(carried["root"], str) or carried["root"] != str(Path(root).resolve())):
        raise ScopeSafetyRefusal("invalid maintenance scope handoff")
    scope = MaintenanceScope.from_dict(carried["maintenance_scope"])
    if scope.mode != "default":
        raise ScopeSafetyRefusal("a maintenance scope handoff can only narrow")
    return scope


def _invocation_floor(root: Path) -> MaintenanceScope | None:
    """The default-only scope this invocation already validated, or None.

    Its pending marker can be cleared mid-invocation (the completed tail), after which activation,
    a relaunch and the boot entries would otherwise resolve bare argv as all-profile.
    """
    if _admitted is not None:
        return _admitted
    return _carried_scope(root)


def admit_launch_scope(root: Path, argv: list[str]) -> MaintenanceScope:
    """hermes_bootstrap's preflight: validate this invocation's scope once and keep a default-only
    result for the rest of the invocation (port adaptation A4)."""
    global _admitted
    scope = resolve_launch_scope(root, argv)
    if scope.mode == "default":
        _admitted = scope
    return scope


def relaunch_handoff(root: Path) -> dict | None:
    """The record a managed-interpreter relaunch into *root* carries; None for all-profile."""
    floor = _invocation_floor(root)
    if floor is None:
        return None
    floor.validate()
    return {"schema": RELAUNCH_SCOPE_SCHEMA, "root": str(Path(root).resolve()),
            "maintenance_scope": floor.to_dict()}


def resolve_launch_scope(root: Path, argv: list[str]) -> MaintenanceScope:
    """Validate authority before even early recovery or a launch short-circuit.

    Pending scope applies to flagless starts and repair commands as well. A default-only scope
    this invocation already validated (:func:`_invocation_floor`) still applies after its pending
    marker is cleared, and can never be broadened. This is stdlib-only: it must work before
    activation makes SDKs importable.
    """
    from hermes_cli._parser import command_argv
    command = command_argv(argv)
    requested = None
    scope_command = command[:1] == ["update"] or command[:2] == ["pm", "install"]
    explicit = scope_command and any(
        arg == "--maintenance-scope" or arg.startswith("--maintenance-scope=") for arg in command)
    if explicit:
        import argparse

        class ScopeParser(argparse.ArgumentParser):
            def error(self, message):
                raise ScopeSafetyRefusal(message)

        parser = ScopeParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--maintenance-scope", choices=("all", "default"))
        args, _ = parser.parse_known_args(command[1:])
        requested = args.maintenance_scope
    try:
        floor = _invocation_floor(root)
        if floor is not None and requested is None:
            # A flagless start keeps the scope it was admitted under. An obligation armed by
            # another start since then is not this invocation's to discharge or widen.
            pending = pending_scope(root)
            if pending is not None and pending != floor:
                raise ScopeSafetyRefusal("the pending maintenance obligation changed during this invocation")
            requested = floor
        scope = resolve_scope(root, requested)
    except OSError as exc:
        raise ScopeSafetyRefusal("cannot establish maintenance scope") from exc
    if floor is not None and scope != floor:
        raise ScopeSafetyRefusal("cannot broaden this invocation's default-only maintenance")
    if explicit and command[:1] == ["update"] and scope.mode == "default" and (
            "--no-gateway-restart" not in command or "--gateway" in command):
        raise ScopeSafetyRefusal("default-only maintenance must run from a shell with --no-gateway-restart")
    return scope


def require_scope_target(source: bytes | None) -> None:
    """Refuse an older target that would discard scope at the interpreter seam.

    Read the target's declared protocol as data; never import/execute target code.
    This is a compatibility gate, not a substitute for native source provenance.
    """
    if current_scope().mode == "all":
        return
    import ast
    try:
        tree = ast.parse(source or b"")
        supported = any(isinstance(node, ast.Assign)
                        and any(isinstance(name, ast.Name) and name.id == "MAINTENANCE_SCOPE_SCHEMA"
                                for name in node.targets)
                        and isinstance(node.value, ast.Constant)
                        and type(node.value.value) is int
                        and node.value.value == MAINTENANCE_SCOPE_SCHEMA for node in tree.body)
    except (SyntaxError, ValueError):
        supported = False
    if not supported:
        raise ScopeSafetyRefusal("update target does not support default-only maintenance handoff; checkout unchanged")


def record_scope() -> None:
    from hermes_cli.update_receipt import record_fact, record_skip, persist_running_receipt
    scope = current_scope()
    record_fact("maintenance_scope", scope.to_dict())
    if scope.mode == "default":
        record_skip("other_profile_data", "default-only maintenance: config, skills, env, migrations, caches, snapshots and DB recovery excluded")
    persist_running_receipt()
    if scope.mode == 'default':
        print('→ Maintenance scope: default only; other profile data skipped. '
              'Source, products, runtime and dependency union remain shared.')
