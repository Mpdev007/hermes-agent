"""Authoritative admission/commit and retained bindings for Anthropic pool rows.

Use auth.py's existing process/thread locks and ownership rules. The refresh
lock serializes a pool-owned single-use grant, but never authorizes a write:
auth remove and our short admission/commit transactions use the SAME auth.lock.
"""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import logging

from hermes_cli import auth

logger = logging.getLogger(__name__)


def same_source(left, right):
    return (
        left.provider == right.provider
        and left.id == right.id
        and left.source == right.source
        and left.auth_type == right.auth_type
        and str(left.base_url or "").strip().rstrip("/")
        == str(right.base_url or "").strip().rstrip("/")
    )


def owner_path(pool, entry):
    if entry.id in pool._borrowed_root_ids:
        return auth._global_auth_file_path()
    return auth._auth_file_path()


@contextmanager
def store_lock(pool, entry):
    # Native remove holds the profile auth lock; root write-through also takes
    # the root auth lock. Preserve that ordering for borrowed-root rows.
    with auth._auth_store_lock(timeout_seconds=pool._single_use_refresh_lock_timeout()):
        if entry.id in pool._borrowed_root_ids:
            target = owner_path(pool, entry)
            if target is None:
                raise RuntimeError("Borrowed Anthropic credential has no owning store")
            with auth._auth_store_lock(target_path=target):
                yield
        else:
            yield


def current_row(pool, selected):
    """Called under store_lock; None means removed/replaced, NOT stale fallback."""
    from agent.credential_pool import PooledCredential, _profile_owns_pool_provider

    borrowed = selected.id in pool._borrowed_root_ids
    if borrowed == _profile_owns_pool_provider(pool.provider):
        return None
    row = next((row for row in auth.read_credential_pool(pool.provider)
                if row.get("id") == selected.id), None)
    if row is None:
        return None
    current = PooledCredential.from_dict(pool.provider, row)
    return current if same_source(selected, current) else None


def _pair(entry):
    return entry.access_token, entry.refresh_token


def rotation_consumed(pool, entry):
    """Read the native verdict at the credential's actual authority, without writes."""
    from agent.anthropic_credentials import is_rotation_consumed_uncommitted, spent_rotation_source_path
    source_path = spent_rotation_source_path(entry.source)
    if entry.source not in ("hermes_pkce", "claude_code"):
        source_path = owner_path(pool, entry)
    return any(is_rotation_consumed_uncommitted(secret, source_path=source_path)
               for secret in (entry.access_token, entry.refresh_token))


def fail_closed_owned_rotation(pool, current, exc):
    """Quarantine only the still-authoritative row; caller holds its store lock."""
    from agent.credential_pool import persist_pool_entries

    pool._replace_entry(current, current)
    path = owner_path(pool, current)
    pool._fail_closed_unpersisted_rotation(
        current, exc, store=str(path), persist=False, source_path=path,
    )
    updated = next(e for e in pool.entries() if e.id == current.id)
    try:
        # The verdict is already recorded even if the auth store is still down.
        persist_pool_entries(pool.provider, [updated.to_dict()],
                             token_bases={current.id: _pair(current)})
    except Exception:
        logger.warning("Could not persist Anthropic quarantine to %s; native spent-rotation "
                       "verdict retained", path, exc_info=True)


def check_spent_rotation(pool, entry):
    """Adopt the native consumed-rotation verdict before any further POST."""
    from agent.credential_pool import _RefreshDone

    if rotation_consumed(pool, entry):
        exc = RuntimeError("a prior Anthropic OAuth refresh consumed this token but the "
                           "rotation never committed (spent-rotation sidecar verdict)")
        if entry.source not in ("hermes_pkce", "claude_code") and entry.id in pool._persisted_token_pairs:
            raise _RefreshDone(fail_closed_owned_rotation(pool, entry, exc))
        raise _RefreshDone(pool._fail_closed_unpersisted_rotation(
            entry, exc, store="credential store",
        ))


def recover_owned_failure(pool, current, exc):
    """Caller holds store_lock and has revalidated source and token generation."""
    from agent.anthropic_credentials import is_terminal_anthropic_refresh_error
    from agent.credential_pool import persist_pool_entries

    # Reuse native status policy without _adopt/_persist's whole-pool snapshot.
    pool._replace_entry(current, current)
    if is_terminal_anthropic_refresh_error(exc):
        logger.warning("Anthropic OAuth refresh token for %s is terminally invalid (%s); "
                       "re-run 'hermes auth add anthropic' to sign in again.",
                       current.label or current.id[:8], exc)
        pool._mark_dead_refresh_grant(current, exc, persist=False)
        updated = next(e for e in pool.entries() if e.id == current.id)
    else:
        updated = pool._mark_exhausted(current, None, persist=False)
    persist_pool_entries(pool.provider, [updated.to_dict()],
                         token_bases={current.id: _pair(current)})


def refresh_owned_entry(pool, selected, *, force=False):
    """Refresh a persisted, pool-owned row without holding auth.lock over HTTP.

    All native refresh callers for these rows enter this path. The per-row
    lock reuses auth.py's reentrant file-lock machinery. Remove does not take it:
    re-read under the real owner store lock at BOTH admission and commit.
    """
    from agent.anthropic_credentials import refresh_anthropic_oauth_pure
    from agent.credential_pool import _RefreshDone, _MARK_OK, persist_pool_entries

    owner = owner_path(pool, selected)
    if owner is None:
        return None
    suffix = hashlib.sha256(selected.id.encode()).hexdigest()
    refresh_target = owner.with_name(f"{owner.stem}-anthropic-refresh-{suffix}.json")
    with auth._auth_store_lock(target_path=refresh_target,
                              timeout_seconds=pool._single_use_refresh_lock_timeout()):
        with store_lock(pool, selected):
            admitted = current_row(pool, selected)
            if admitted is None:
                return None
            if _pair(admitted) != _pair(selected):
                pool._replace_entry(selected, admitted)
                return admitted
            if not force and not pool._entry_needs_refresh(admitted):
                return admitted
            refresh_token = admitted.refresh_token
            if admitted.auth_type != "oauth" or not refresh_token:
                return None
            try:
                check_spent_rotation(pool, admitted)
            except _RefreshDone as done:
                return done.result
        # Pool-owned grants have no singleton write. Exchange only; all status
        # and token writes remain conditional on continued source ownership.
        try:
            refreshed = refresh_anthropic_oauth_pure(
                refresh_token, use_json=admitted.source.endswith("hermes_pkce"))
            updated = replace(admitted, access_token=refreshed["access_token"],
                              refresh_token=refreshed["refresh_token"],
                              expires_at_ms=refreshed["expires_at_ms"])
        except Exception as exc:
            logger.debug("Anthropic pool refresh failed; checking native recovery", exc_info=True)
            with store_lock(pool, admitted):
                current = current_row(pool, admitted)
                if current is None:
                    return None
                if _pair(current) != _pair(admitted):
                    pool._replace_entry(admitted, current)
                    return current
                return recover_owned_failure(pool, current, exc)
        with store_lock(pool, admitted):
            current = current_row(pool, admitted)
            if current is None:
                return None  # exchange may have happened; never resurrect/adopt its result
            if _pair(current) != _pair(admitted):
                pool._replace_entry(admitted, current)
                return current  # valid same-source peer rotation, never overwrite it
            updated = replace(updated, **_MARK_OK)
            # Persist only this row: stale sibling snapshots must not resurrect
            # another row removed while HTTP was in flight.
            try:
                persisted = persist_pool_entries(pool.provider, [updated.to_dict()],
                                                 token_bases={admitted.id: _pair(admitted)},
                                                 status_cleared_ids=[updated.id])
            except Exception as exc:
                logger.warning("Anthropic pool-owned rotation commit failed", exc_info=True)
                return fail_closed_owned_rotation(pool, current, exc)
            if persisted is None:
                return fail_closed_owned_rotation(
                    pool, current, RuntimeError("owning auth-store write did not commit"))
            updated = current_row(pool, admitted)
            if updated is None:
                return None
            pool._replace_entry(admitted, updated)
            pool._persisted_token_pairs[updated.id] = _pair(updated)
            return updated


def sync_selected_entry(retained, selected, fresh, current, *, model=None):
    """Validate at adoption and update only the retained row, not its cursor/lease."""
    from agent.credential_persistence import fingerprint_secret_value

    with retained._lock, store_lock(fresh, current):
        bound = next((e for e in retained._entries if e.id == selected.id), None)
        if bound is None or not same_source(selected, bound) or not same_source(selected, current):
            return None
        if (selected.id in retained._borrowed_root_ids) != (current.id in fresh._borrowed_root_ids):
            return None
        live = current_row(fresh, current)
        if live is None:
            return None
        if live.source == "claude_code":
            # auth.json deliberately stores no borrowed Claude secrets. Its row
            # proves membership/status; the shared credentials file owns tokens.
            live = replace(live, access_token=current.access_token,
                           refresh_token=current.refresh_token, expires_at_ms=current.expires_at_ms)
            with fresh._claude_code_credentials_lock():
                live = fresh._sync_anthropic_entry_from_credentials_file(live)
        elif live.source.startswith("env:"):
            # Reference-only rows have no raw secret on disk. Hydrate only the
            # fresh native env value matching the still-authoritative fingerprint.
            fingerprint = fingerprint_secret_value(current.runtime_api_key)
            if not fingerprint or live.extra.get("secret_fingerprint") != fingerprint:
                return None
            live = replace(live, access_token=current.access_token)
        fresh._replace_entry(current, live)
        available, _ = fresh._available_entries(clear_expired=False, refresh=False, model=model)
        if not any(e.id == live.id for e in available):
            return None
        retained._replace_entry(bound, live)
        retained._persisted_token_pairs[live.id] = _pair(live)
        return live
