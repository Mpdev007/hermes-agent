"""Authenticated, profile-bound discovery without legacy execution dispatch."""
import asyncio
from pathlib import Path

from persistence.core.errors import AuthorityRefusal


async def discover_commands(authority, actor, params, *, completion=False):
    if 'session:read' not in actor.capabilities or actor.profile_id != authority.profile_id:
        raise AuthorityRefusal('permission_denied')
    allowed = {'profile', 'text', 'session_id'} if completion else {'profile', 'session_id'}
    if (not isinstance(params, dict) or set(params) - allowed
            or any(not isinstance(value, str) for value in params.values())):
        raise AuthorityRefusal('invalid_params')
    scope = None
    if params.get('session_id'):
        # Project skills resolve from the session's workspace, not the gateway process cwd.
        from gateway.session_commands import _session_workspace
        from gateway.session_contract import SessionRef
        ref = SessionRef(authority.profile_id, params['session_id'])
        authority.authorize(actor, ref, 'session:read')
        scope = _session_workspace(authority.runner, authority.sessions[ref.session_id].source)

    def discover():
        from hermes_cli.profiles import profile_matches_home
        home = Path(authority.profile_id)
        if params.get('profile') and not profile_matches_home(params['profile'], home):
            raise AuthorityRefusal('profile_mismatch')
        from gateway.run import _profile_runtime_scope
        from tui_gateway.command_discovery import command_catalog, slash_completions
        with _profile_runtime_scope(home):
            return (slash_completions(params.get('text', ''), scope=scope) if completion
                    else command_catalog(scope=scope))

    return await asyncio.to_thread(discover)


async def list_plugins(authority, actor, params):
    """``plugins.list`` for this profile's own plugin manager: what the gateway loaded, with each
    plugin's tool/hook/command counts and load error (the classic CLI's ``/plugins`` reads it, so the
    CLI process imports no plugin code). Plain data only."""
    if 'session:read' not in actor.capabilities or actor.profile_id != authority.profile_id:
        raise AuthorityRefusal('permission_denied')
    if (not isinstance(params, dict) or set(params) - {'observe_only'}
            or ('observe_only' in params and not isinstance(params['observe_only'], bool))):
        raise AuthorityRefusal('invalid_params')

    def rows():
        import os
        from gateway.run import _profile_runtime_scope
        from gateway.status import get_process_start_time
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        from hermes_cli.plugins import get_plugin_toolsets
        with _profile_runtime_scope(Path(authority.profile_id)):
            if not params.get('observe_only', False):
                discover_plugins()  # existing CLI behavior; observation never initiates discovery
            manager = get_plugin_manager()
            observing = params.get('observe_only', False)
            if observing and not manager._discovery_lock.acquire(timeout=1):
                raise AuthorityRefusal('policy_observation_unavailable')
            try:
                loaded = manager.list_plugins()
                discovery_complete = manager._discovery_complete
                toolsets = [] if observing else get_plugin_toolsets()
                portable = sorted(manager.get_portable_mcp_servers())
            finally:
                if observing:
                    manager._discovery_lock.release()
        return {'plugins': [{'name': str(p['name']), 'key': str(p['key']), 'version': str(p['version'] or ''),
                             'source': str(p['source']),
                             'enabled': bool(p['enabled']), 'tools': int(p['tools']), 'hooks': int(p['hooks']),
                             'commands': int(p['commands']), 'error': str(p['error'] or ''),
                             'policy_evidence': p.get('policy_evidence', [])} for p in loaded],
                'policy_observation': {'schema': 'hermes.plugin-policy-observation/v1',
                                       'pid': os.getpid(), 'start_time': get_process_start_time(os.getpid()),
                                       'discovery_complete': bool(discovery_complete),
                                       'profile_id': authority.profile_id, 'workers_attested': False},
                # The CLI's /tools and its completion validate plugin toolsets against these.
                'toolsets': [{'key': str(key), 'label': str(label), 'description': str(desc or '')}
                             for key, label, desc in toolsets],
                'portable_mcp_servers': [str(name) for name in portable]}

    return await asyncio.to_thread(rows)
