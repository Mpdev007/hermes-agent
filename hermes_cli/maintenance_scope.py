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

# Target-tree capability: understood by source updates before moving any code.
MAINTENANCE_SCOPE_SCHEMA = 1


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


def resolve_launch_scope(root: Path, argv: list[str]) -> MaintenanceScope:
    """Validate authority before even early recovery or a launch short-circuit.

    Pending scope applies to flagless starts and repair commands as well. This
    is stdlib-only: it must work before activation makes SDKs importable.
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
        scope = resolve_scope(root, requested)
    except OSError as exc:
        raise ScopeSafetyRefusal("cannot establish maintenance scope") from exc
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
