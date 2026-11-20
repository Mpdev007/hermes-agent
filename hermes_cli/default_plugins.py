"""Catalog plugins every home gets: entries marked ``default: true`` (official or verified tier).

Each home gets each default plugin installed at its catalog pin and enabled exactly once. The home
records it under ``_default_plugins_installed`` in its config.yaml (the left-core migrator records its
one-time install the same way), so a user who removes or disables a default plugin keeps that choice
through every later update. A default the home already has, enabled or disabled by the user, is
recorded without reinstalling; one left on disk but never enabled (a refused enable leaves the tree)
is enabled on the next attempt.

When it runs: at the end of every source install and ``hermes update`` (``update_cmd_maint``'s
maintenance tail, in the foreground, for every profile home). Installs that never run that tail
(bundled Desktop, Docker, Nix, Termux) and a newly created profile start a detached child instead
(:func:`spawn_install`), so neither a start nor a create waits on git and dependency installs: the
boot bootstrap starts it once per installed revision and on later starts until a child reports every
default settled. Nothing is fetched where ``security.allow_lazy_installs`` is off, and a failed
attempt backs the next one off (an hour, doubling to a week).

Port adaptation A5: under default-only maintenance (explicit, pending or carried) none of this automatic
every-home work starts: a create, boot, child or pass leaves an install-level request
(``cache/default-plugins.owed.d/<token>``, A7: one file per deferral) that reopens the boot step for a later ordinary
start. A complete ordinary pass, including one where every home denies fetching, answers exactly the requests it
captured before it read any home; requests recorded during it, and every failed, partial or deferred pass, stay owed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_INSTALLED_KEY = "_default_plugins_installed"
_BACKOFF = "default-plugins"  # memory_provider_migration failure-stamp name, in the default home


def default_entries() -> list:
    """Catalog entries marked default that this OS and this Hermes can run (live catalog, else in-tree).
    The tier is checked here too: the live/in-tree merge can carry ``default`` onto a community entry."""
    from hermes_cli.plugin_catalog import DEFAULT_TIERS, load_catalog_live
    from hermes_cli.plugins_cmd import PluginOperationError
    from hermes_cli.plugins_cmd_catalog import refuse_incompatible_catalog_entry

    def runs_here(entry) -> bool:
        try:
            refuse_incompatible_catalog_entry(entry)  # the install gate's own platform/version verdict
        except PluginOperationError:
            return False
        return True
    return [entry for entry in load_catalog_live() if entry.default and entry.tier in DEFAULT_TIERS and runs_here(entry)]


def _recorded(home: Path) -> set[str]:
    from hermes_cli.left_core_migration import _read_config
    done = _read_config(home).get(_INSTALLED_KEY)
    return set(map(str, done)) if isinstance(done, list) else set()


def _record(home: Path, names: set[str]) -> None:
    """Set only the marker key: a one-key write re-reads the file as it writes, so another process's
    edit to the rest of *home*'s config (a running gateway, the user) is never overwritten."""
    from hermes_cli.config import atomic_config_write, read_user_config_raw
    from hermes_cli.config_backend import config_exists, write_config_key
    from hermes_cli.left_core_migration import _stamp_created
    path = home / "config.yaml"
    done = _recorded(home)
    marker = sorted(done | names)
    if config_exists(path):
        write_config_key(path, _INSTALLED_KEY, marker)
        return
    config = read_user_config_raw(path)
    config[_INSTALLED_KEY] = marker
    _stamp_created(home, config)
    atomic_config_write(path, config)


def _installed_dir(home: Path, entry) -> Optional[Path]:
    """*home*'s install of *entry*: the dir its install record ties to the catalog name, in either record
    shape (``catalog.name``, or the older top-level ``catalog_name``), else the dir of that name. Read by
    path, not by binding *home* (the installer's lookup reads the bound home only, and a bind at boot would
    freeze the launch environment)."""
    from hermes_cli.plugins_provenance import read_sidecar_rows
    plugins = home / "plugins"
    for name, row in read_sidecar_rows(plugins).items():
        block = row.get("catalog") if isinstance(row, dict) else None
        named = block.get("name") if isinstance(block, dict) else (row.get("catalog_name") if isinstance(row, dict) else None)
        if named == entry.name and (plugins / name).is_dir():
            return plugins / name
    return plugins / entry.name if (plugins / entry.name).is_dir() else None


def _settled(home: Path, entry) -> bool:
    """*home* already has *entry*: a tree the user put there (enabled or not, it is theirs), or one an
    earlier automatic attempt left that someone has since enabled or disabled. A tree a refused enable
    left behind (stamped by that attempt, in neither list) is retried."""
    from hermes_cli.left_core_migration import _plugins_listed
    from hermes_cli.memory_provider_migration import _failure_stamp
    target = _installed_dir(home, entry)
    if target is None:
        return _placed(home, entry).exists()  # the tree a failed attempt left is gone: the user removed it
    if not _failure_stamp(home, _attempt(entry)).exists():
        return True
    names = {entry.name, target.name}
    try:
        from hermes_yaml import safe_load
        names.add(str((safe_load((target / "plugin.yaml").read_text(encoding="utf-8-sig")) or {}).get("name") or ""))
    except (OSError, ValueError, AttributeError):  # no or malformed manifest: the dir names count
        pass
    enabled, disabled = _plugins_listed(home, tuple(names - {""}))
    return enabled or disabled


def _attempt(entry) -> str:
    """Failure-stamp name of this home's automatic install of *entry*."""
    return f"default-{entry.name}"


def _placed(home: Path, entry) -> Path:
    """Written when a failed automatic attempt left *entry*'s tree on disk (a refused enable)."""
    return home / "cache" / f"default-plugins.placed-{entry.name}"


def fetch_policy(home: Path) -> str:
    """``"allowed"``, ``"denied"`` (``security.allow_lazy_installs`` off, or the env opt-out) or
    ``"unreadable"`` for *home* (managed overlay applied). Nothing is fetched unless allowed, but an
    unreadable config keeps the home owed, so a repaired file is picked up. Read without binding the
    profile: a whole-profile bind at boot would freeze the launch environment before boot sets it up."""
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").strip().lower() in ("1", "true", "yes"):
        return "denied"
    from gateway.config_loader import read_yaml_layers
    try:
        security = read_yaml_layers(home).get("security")
    except Exception:  # health: allow BLE001 -- a malformed or unreadable config never grants a fetch
        logger.debug("default plugins: unreadable config for %s", home, exc_info=True)
        return "unreadable"
    allowed = (security if isinstance(security, dict) else {}).get("allow_lazy_installs", True) is True
    return "allowed" if allowed else "denied"


def fetch_allowed(home: Path) -> bool:
    return fetch_policy(home) == "allowed"


def install_defaults(home: Path, *, say: Callable[[str], None] = logger.info,
                     entries: Optional[list] = None) -> dict:
    """Install and enable every default plugin *home* never had. ``installed`` lists what this call
    installed, ``owed`` what is still to fetch (a failed install). One plugin's failure never raises."""
    from hermes_cli.left_core_migration import _first_cause, _install_into
    from hermes_cli.memory_provider_migration import _home_label, _note_failure, _note_success
    result: dict = {"installed": [], "owed": []}
    policy = fetch_policy(home)
    if policy == "unreadable":
        return {**result, "owed": [str(home)]}
    if policy == "denied":
        return {**result, "skipped": "lazy-installs-disabled"}
    done = _recorded(home)
    pending = [entry for entry in (default_entries() if entries is None else entries) if entry.name not in done]
    have = {entry.name for entry in pending if _settled(home, entry)}
    install = _install_into(home, consent=True)  # a reviewed default: its dependencies come with it
    for entry in pending:
        if entry.name in have:
            continue
        try:
            # A tree still on disk here is one an earlier automatic attempt left and nobody chose since
            # (_settled): discarded first, so a re-pin cannot wedge the retry at "already exists". Its
            # placed marker goes with it, since a tree missing because WE removed it is not the user's
            # removal. The install runs at this home's catalog pin, never an explicit ref (a ref off that
            # pin is recorded as a user's ``--ref`` hold that unattended updates never move), and
            # unforced, so it never overwrites another plugin already at the destination.
            leftover = _installed_dir(home, entry)
            if leftover is not None:
                _placed(home, entry).unlink(missing_ok=True)
            outcome = install(entry.name, discard=leftover)
        except Exception as exc:  # health: allow BLE001 -- network, uv or kill list: reported per plugin, never raised
            outcome = {"ok": False, "error": str(exc)}
        if outcome.get("ok"):
            _note_success(home, _attempt(entry))
            _placed(home, entry).unlink(missing_ok=True)
            have.add(entry.name)
            result["installed"].append(entry.name)
            say(f"  ✓ Installed default plugin '{entry.name}' for {_home_label(home)}")
            continue
        result["owed"].append(entry.name)
        _note_failure(home, _attempt(entry))  # marks a tree it left as ours to retry, not the user's
        if _installed_dir(home, entry) is not None:
            _placed(home, entry).parent.mkdir(parents=True, exist_ok=True)
            _placed(home, entry).touch()
        say(f"  ⚠ Default plugin '{entry.name}' could not be installed for {_home_label(home)}: "
            f"{_first_cause(str(outcome.get('error') or ''))}. Hermes retries at a later start; "
            f"`hermes plugins install {entry.name}` installs it now.")
    if have:
        _record(home, have)
    return result


def install_defaults_all_homes(*, say: Callable[[str], None] = print, wait: float = 900.0) -> dict:
    """Every profile home sharing this install (the default home plus each live profile). The catalog is
    read once; one home's failure never stops the next. Waits up to *wait* seconds for another installer
    to finish (``TimeoutError`` past that)."""
    from hermes_cli.plugin_catalog import fetch_live_catalog
    from pm.plugins_state import dependency_homes
    if installs_deferred():
        # Port adaptation A5: under default-only maintenance no home is read, nothing is fetched or written.
        return {"installed": [], "owed": [], "deferred": True}
    summary: dict = {"installed": [], "owed": []}
    policies = {home: fetch_policy(home) for home in dependency_homes()}
    summary["owed"] += [str(home) for home, policy in policies.items() if policy == "unreadable"]
    homes = [home for home, policy in policies.items() if policy == "allowed"]
    entries = default_entries() if homes else []  # no catalog read (or fetch) where nothing may install
    if homes and not (fetch_live_catalog() or {}).get("entries"):  # an expired offline cache has none
        # Offline: the in-tree defaults still install, but one the live catalog added since this release
        # is unknown, so the pass is owed until the live catalog is read.
        summary["owed"].append("live catalog")
    if not entries:
        return summary
    with _machine_lock(wait):  # never two installers in one home (an update tail beside a detached child)
        for home in homes:
            if installs_deferred():  # A5: an obligation that began during the pass stops it before this home's writes
                summary["deferred"] = True
                break
            try:
                outcome = install_defaults(home, say=say, entries=entries)
            except Exception:  # health: allow BLE001 -- one home (unreadable config.yaml) never stops the rest; retried next run
                logger.warning("default plugins skipped for %s", home, exc_info=True)
                outcome = {"installed": [], "owed": [str(home)]}
            summary["installed"] += outcome["installed"]
            summary["owed"] += outcome["owed"]
    return summary


def _machine_lock(wait: float):
    """The OS-held lock every installer of the default set holds (the kernel frees it when its holder dies).
    Raises ``TimeoutError`` when another holder keeps it past *wait* seconds."""
    from hermes_cli.auth import _file_lock
    from pm.environments import dependency_home_root
    return _file_lock(_state(Path(dependency_home_root()), "lock"), threading.local(), wait,
                      "another default plugin install is running")


# ── detached child: first start, profile create ──────────────────────────────

def _state(root: Path, name: str) -> Path:
    return root / "cache" / f"default-plugins.{name}"


def _identity() -> str:
    from hermes_cli.boot_bootstrap import current_install_identity
    from pm.paths import install_root
    return current_install_identity(install_root()) or ""


# ── port adaptation A5: automatic every-home work waits under default-only maintenance ─────────────

def installs_deferred() -> bool:
    """Whether automatic every-home work waits in this context (explicit, pending or carried default scope:
    ``maintenance_scope.every_home_installs_deferred``). Checked at the start (:func:`spawn_install`), the child
    (:func:`_run_child`) and the installer (:func:`install_defaults_all_homes`) before any fetch, spawn or
    per-profile write."""
    from hermes_cli.maintenance_scope import every_home_installs_deferred
    from pm.paths import install_root
    return every_home_installs_deferred(install_root())


def _owe_deferred(root: Path) -> None:
    """Leave one install-level request for this deferral: a file of its own in ``default-plugins.owed.d`` (beside
    ``settled``), created whole (O_EXCL) and never rewritten. Port adaptation A7: a single shared marker lost a
    request written between a consumer's check and its unlink; a request of its own cannot be."""
    owed = _state(root, "owed.d")
    owed.mkdir(parents=True, exist_ok=True)
    token = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(8)}"
    os.close(os.open(owed / token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def _owed_batch(root: Path) -> frozenset:
    """The deferred requests a pass may answer: the regular files standing now in ``default-plugins.owed.d``, each
    created whole under a name of its own and never rewritten (:func:`_owe_deferred`, or an A5/A6 build's marker
    claimed by :func:`_claimed_batch`). A pass captures them before it enumerates any home and answers only these
    (:func:`_consume_owed`).

    Port adaptation A7.3 (Hermes fa0015f0): the A5/A6 shared name ``default-plugins.owed`` is never part of a batch.
    An A5/A6 writer may recreate it after a claim (and re-stamp it during a scan); that is a later request, and
    unlinking the shared name would drop it. It still counts as owed (:func:`_owed_present`)."""
    batch = set()
    try:
        entries = list(_state(root, "owed.d").iterdir())
    except FileNotFoundError:
        return frozenset()
    for path in entries:
        try:
            if stat.S_ISREG(path.lstat().st_mode):  # never a link: only the request files themselves
                batch.add(path)
        except FileNotFoundError:  # answered by another pass meanwhile
            continue
    return frozenset(batch)


def _owed_present(root: Path) -> bool:
    """Whether any deferred request is owed: a per-request file, or an A5/A6 build's shared marker (detection only;
    it is claimed into ``owed.d`` before a pass may answer it)."""
    return _state(root, "owed").is_file() or bool(_owed_batch(root))


def _claimed_batch(root: Path) -> frozenset:
    """:func:`_owed_batch` for a pass that may settle what it captures. Port adaptation A7.2: an A5/A6 build's single
    ``default-plugins.owed`` is first claimed by one atomic rename into ``owed.d``, so this pass owns exactly the
    request that stood before it began. An A5/A6 writer that comes later finds no file (``Path.touch`` re-stamps an
    existing file, else creates one) and leaves a new marker, which stays owed for a later pass; deleting the shared
    name itself could drop that writer's request. A claimed request that is not settled stays owed in ``owed.d``."""
    legacy = _state(root, "owed")
    if legacy.is_file():
        owed = _state(root, "owed.d")
        owed.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(legacy, owed / f"legacy-{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(8)}")
        except FileNotFoundError:  # another pass claimed it first; that pass answers for it
            pass
    return _owed_batch(root)


def deferred_request_owed() -> bool:
    """Whether a deferred request is owed: every profile's boot then owes the default-plugin step, even on a
    revision whose record had settled (``boot_bootstrap._owed_steps``)."""
    from pm.environments import dependency_home_root
    return _owed_present(Path(dependency_home_root()))


@contextlib.contextmanager
def _settlement_order():
    """Port adaptation A7.3 (Hermes fa0015f0): one cross-process order between arming default-only maintenance and a
    pass's decision to settle what it captured, held only around that decision and its publication.

    Every default-only obligation is armed by an update that holds the checkout lock (``update_lock.UpdateLock``,
    taken checkout-first by ``source_completion`` and ``hermes update``). The settling pass takes the same lock across
    its final scope read, the settled record and the consumption of its batch: an obligation armed first is seen
    there and the batch is kept; a pass that decides first has published before that update can arm. A pass inside
    the update's own process (its maintenance tail) or one it started with the lock inherited joins the hold. Yields
    False when another holder keeps the lock past the native contention wait, or the lock cannot be taken: the
    caller then settles nothing and keeps its batch owed for a later ordinary pass. While held, this short claim reads
    as an update in progress (``update_lock.update_in_progress``) and names this process in the lock record."""
    from hermes_cli import update_lock
    from hermes_cli.update_lock import _acquire_checkout, _release_checkout
    from pm.paths import install_root
    root = Path(install_root())
    held = getattr(update_lock, "_HELD", None)
    if held is not None and held["path"] != str(update_lock.checkout_lock_path(root)):
        # This process holds another checkout's lock; ``_acquire_checkout`` keeps one record per process, so taking
        # this one would replace it. Settle nothing here.
        yield False
        return
    try:
        refused = _acquire_checkout(root)
    except OSError:
        refused = True
    if refused is not None:
        yield False
        return
    try:
        yield True
    finally:
        _release_checkout()


def _consume_owed(batch: frozenset) -> None:
    """A complete ordinary pass answers exactly the requests it captured before it began (:func:`_owed_batch`);
    any recorded since (a deferral in another process during the pass) stays owed for a later independent pass."""
    for request in batch:
        request.unlink(missing_ok=True)


def spawn_install(home: Optional[Path] = None) -> bool:
    """Start the detached child that installs the defaults for every home (*home*, when given, must allow
    fetching), unless the last attempt failed recently. A running child sees the request when it finishes
    and scans again. Never waits."""
    from hermes_cli._launchers import runtime_command
    from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway, windows_detach_popen_kwargs
    from hermes_cli.memory_provider_migration import _failed_recently
    from hermes_cli._subprocess_compat import IS_WINDOWS
    from pm.environments import dependency_home_root
    from pm.paths import install_root
    root = Path(dependency_home_root())
    if installs_deferred():
        # Port adaptation A5: a create (or a boot) under default-only maintenance starts nothing. *home* is not
        # read, ``settled`` and ``rerun`` are untouched, and one install-level request is owed for a later
        # ordinary start (:func:`deferred_request_owed`).
        _owe_deferred(root)
        return False
    if home is not None and fetch_policy(home) == "denied":
        return False
    _state(root, "settled").unlink(missing_ok=True)  # unsettled until a pass installs it, so every boot re-checks
    if _failed_recently(root, _BACKOFF):
        return False
    rerun = _state(root, "rerun")
    rerun.parent.mkdir(parents=True, exist_ok=True)
    rerun.touch()
    argv = runtime_command(install_root(), module="hermes_cli.default_plugins")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        # detached-spawn: ok — git and dependency installs must neither delay a boot or a create nor die with it; one installer at a time (OS lock)
        subprocess.Popen(argv, **kwargs, **windows_detach_popen_kwargs())
    except OSError:
        if not IS_WINDOWS:
            raise
        # A job object without BREAKAWAY_OK refuses CREATE_BREAKAWAY_FROM_JOB; retry without it.
        subprocess.Popen(argv, **kwargs, creationflags=windows_detach_flags_without_breakaway())
    return True


def _run_child() -> int:
    """The detached child; one that finds another installer running leaves its request to that one."""
    from pm.environments import dependency_home_root
    root = Path(dependency_home_root())
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s default-plugins %(message)s",
                        filename=str(root / "logs" / "default-plugins.log"))
    try:
        if installs_deferred():
            # Port adaptation A5: the child re-reads pending or carried scope itself and never scans under it.
            _owe_deferred(root)
            return 0
        return 1 if install_and_record(say=logger.info, wait=1.0)["owed"] else 0
    except TimeoutError:
        return 0
    except Exception:  # health: allow BLE001 -- a detached child has no stderr: log the cause and back off
        from hermes_cli.memory_provider_migration import _note_failure
        logger.exception("default plugin install failed")
        _note_failure(root, _BACKOFF)
        return 1


def install_and_record(*, say: Callable[[str], None] = print, wait: float = 900.0) -> dict:
    """The one coordinator (update tail and detached child): :func:`install_defaults_all_homes` again until
    no rescan was requested meanwhile (a profile created during the pass), then the outcome the boot step
    reads: a failure begins the backoff, a clean pass marks this revision settled."""
    from hermes_cli.memory_provider_migration import _note_failure, _note_success
    from pm.environments import dependency_home_root
    root = Path(dependency_home_root())
    rerun = _state(root, "rerun")
    while True:
        started = time.time()
        batch = _claimed_batch(root)  # A7: the deferred requests this pass answers, captured before any home is read
        outcome = install_defaults_all_homes(say=say, wait=wait)
        if outcome.get("deferred") or not (rerun.exists() and rerun.stat().st_mtime >= started):
            break
    if not outcome.get("deferred") and not outcome["owed"]:
        # Port adaptation A7.2: only a complete pass that is STILL ordinary settles its batch. A7.3: that last scope
        # read, the settled record and the consumption are one step against arming (:func:`_settlement_order`).
        with _settlement_order() as ordered:
            if ordered and not installs_deferred():
                _note_success(root, _BACKOFF)
                _state(root, "settled").parent.mkdir(parents=True, exist_ok=True)
                _state(root, "settled").write_text(_identity(), encoding="utf-8")
                _consume_owed(batch)
                return outcome
        # Default-only (or unprovable) scope came first, or the order could not be taken: nothing is settled and
        # every captured request stays owed for a later ordinary pass.
        outcome = {**outcome, "deferred": True, **({} if ordered else {"settlement": "order_unavailable"})}
    elif not outcome.get("deferred") and installs_deferred():
        outcome = {**outcome, "deferred": True}  # A7.2: a failed pass that ended restricted is deferred, not failed
    if outcome.get("deferred"):
        # Port adaptation A5: deferred, not failed: no backoff and nothing settled; the request stays owed.
        _owe_deferred(root)
        return outcome
    _note_failure(root, _BACKOFF)
    # A later pass (a profile created after this revision settled) can fail too: owed again.
    _state(root, "settled").unlink(missing_ok=True)
    return outcome


def step_install_default_plugins() -> dict:
    """Boot-bootstrap step, never blocking the start: owed (later starts re-check, without network) until a
    child reports every default settled for this revision; an owed start outside the backoff starts one."""
    from pm.environments import dependency_home_root
    from pm.plugins_state import dependency_homes
    try:
        if installs_deferred():
            # Port adaptation A3 (A5: the same shared check): default-only maintenance (explicit, pending or
            # carried) never reads named homes nor spawns the every-home installer. The step stays owed and a
            # deferred request is left as it is, so the next ordinary start runs the step unchanged.
            return {"ok": True, "skipped": "default-only maintenance", "owed": True}
        root = Path(dependency_home_root())
        batch = _claimed_batch(root)  # A7: captured before the homes are enumerated
        if all(fetch_policy(home) == "denied" for home in dependency_homes()):
            # A7.2: the scan only answers for a scope that was still ordinary when it finished listing the homes.
            # A7.3: that read and the consumption are one step against arming (:func:`_settlement_order`).
            with _settlement_order() as ordered:
                if ordered and not installs_deferred():
                    # A5: nothing may install in any home, so a deferred request is answered as an ordinary
                    # create's. A7: only the requests standing before this check; one recorded during it stays owed.
                    _consume_owed(batch)
                    return {"ok": True, "skipped": "lazy-installs-disabled"}
            if not ordered:
                return {"ok": True, "skipped": "settlement order unavailable", "owed": True}
            return {"ok": True, "skipped": "default-only maintenance", "owed": True}
        settled = _state(root, "settled")
        if (settled.is_file() and settled.read_text(encoding="utf-8-sig").strip() == _identity()
                and not _owed_present(root)):  # A5: a deferred request (A7.3: or a legacy marker) reopens it
            return {"ok": True}
        return {"ok": True, "spawned": spawn_install(), "owed": True}
    except (OSError, ValueError) as exc:  # unreadable profiles dir, failed spawn: still owed, retried next boot
        return {"ok": False, "error": str(exc), "owed": True}


if __name__ == "__main__":
    sys.exit(_run_child())
