"""Native scope escape regressions. No network, real auth or live state.

PM's resolver/build leaf is inert; policy, discovery, eviction, publication,
locks, facts and receipts are real. Transport is in-process for this fixture.
"""
import importlib
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def conflict(tmp_path, monkeypatch):
    import pm
    import pm.paths
    from pm.registry import get_package
    from pm.environment import ResolutionConflict
    from hermes_cli import main_install_repair as repair
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from pm import plugin_eviction
    import os
    writes = []
    def audit(event, args):
        if event == 'open':
            _, mode, flags = args
            write = isinstance(mode, str) and any(c in mode for c in 'wax+') or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        else:
            write = event in {'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.utime'}
        if write and any(isinstance(a, (str, bytes, os.PathLike)) and str(tmp_path / '.hermes' / 'profiles') in os.fsdecode(a) for a in args):
            writes.append({'event': event, 'args': [str(a) for a in args]})
    install = importlib.import_module('pm.install')
    home = tmp_path / '.hermes'
    home.mkdir()
    core = tmp_path / 'core'
    core.mkdir()
    (core / 'pyproject.toml').write_text('[project]\nname="fixture-core"\nversion="0"\nrequires-python=">=3.11"\ndependencies=[]\n[project.optional-dependencies]\nmcp=[]\n')
    (core / 'uv.lock').write_text('version = 1\n')
    (home / 'config.yaml').write_text('mcp_servers:\n  fixture:\n    command: never-execute\n')
    foreign = []
    for name in ['alpha', 'beta']:
        directory = home / 'profiles' / name
        plugin = directory / 'plugins' / ('conflict-' + name)
        plugin.mkdir(parents=True)
        (plugin / 'plugin.yaml').write_text(f'name: conflict-{name}\n')
        (plugin / 'pyproject.toml').write_text(f'[project]\nname="conflict-{name}"\nversion="0"\nrequires-python=">=3.11"\ndependencies=["scope-fixture-lib==1"]\n')
        config = directory / 'config.yaml'
        config.write_text(f'plugins:\n  enabled: [conflict-{name}]\nmemory:\n  provider: conflict-{name}\n')
        foreign.append(config)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(pm.paths, 'repo_root', lambda: core)
    # Only dependency availability is injected; use the real configured SDK probe.
    original_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *a, **kw: None if name == 'mcp' else original_find(name, *a, **kw))
    assert any(row[2] == 'mcp' for row in repair._configured_features_missing_deps())
    trace = []
    def transport(*args, project_root=None, **kwargs):
        assert project_root == core
        trace.append({'sync': kwargs})
        return install.sync_venv(*args, **kwargs)
    monkeypatch.setattr(pm, 'sync_venv', transport)
    package = get_package('venv')
    def resolver(extras, **kwargs):
        from pm.workspace import enabled_member_dirs
        members = kwargs.get('plugin_dirs')
        if members is None:
            members = enabled_member_dirs()
        trace.append({'resolve': [str(p) for p in members], 'extras': extras})
        if 'mcp' in extras and members:
            raise ResolutionConflict('venv', 'fixture resolver: mcp requires scope-fixture-lib==2; foreign plugins require ==1')
        return {}  # inert environment build; native commit/publication stays real
    monkeypatch.setattr(package, 'apply', resolver)
    monkeypatch.setattr(plugin_eviction, '_interpreter_version', lambda: '.'.join(map(str, sys.version_info[:3])))
    sys.addaudithook(audit)
    before = {str(p): p.read_bytes().hex() for p in foreign}
    yield core, foreign, trace, repair, MaintenanceScope, maintenance_scope
    from pm.receipt import latest
    evidence = {'before': before, 'after': {str(p): p.read_bytes().hex() for p in foreign},
                'foreign_writes': writes, 'trace': trace, 'native_pm_receipt': latest()}
    (tmp_path / 'helper-evidence.json').write_text(json.dumps(evidence, indent=2))
    if 'test_all_' not in tmp_path.name:
        assert writes == [], evidence
        assert evidence['before'] == evidence['after']


def test_default_extra_conflict_fails_without_foreign_eviction(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    before = {str(p): p.read_bytes() for p in foreign}
    with scoped(Scope.select('default')):
        with pytest.raises(Exception, match='fixture resolver'):
            repair._install_configured_features_missing_deps(core)
    assert {str(p): p.read_bytes() for p in foreign} == before
    assert trace[0]['sync']['evict_incompatible_plugins'] is False
    assert len(trace[1]['resolve']) == 2
    from pm.receipt import latest
    receipt = latest()
    assert receipt['outcome'] == 'failed'


def test_all_extra_conflict_retains_native_eviction(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    with scoped(Scope.select('all')):
        repair._install_configured_features_missing_deps(core)
    import hermes_yaml as yaml
    for config in foreign:
        changed = yaml.safe_load(config.read_text())
        assert changed['plugins']['disabled'] == ['conflict-' + config.parent.name]
        # Current PM keeps the provider selection for a later eviction retry;
        # only the plugin's enabled/disabled state is changed.
        assert changed['memory']['provider'] == 'conflict-' + config.parent.name
    assert trace[0]['sync']['evict_incompatible_plugins'] is True
    from pm.receipt import latest
    assert latest()['outcome'] == 'ok'


def test_default_unavailable_configured_extra_is_not_healthy(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    manifest = core / 'pyproject.toml'
    manifest.write_text(manifest.read_text().replace('mcp=[]', 'unrelated=[]'))
    with scoped(Scope.select('default')):
        with pytest.raises(RuntimeError, match='Configured features.*missing'):
            repair._install_configured_features_missing_deps(core)
    assert trace == []


def test_helper_resolves_persisted_scope_without_context(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    from hermes_cli.venv_sync import arm_completion
    arm_completion(core, maintenance_scope=Scope.select('default'))
    assert Scope.select('all').mode == 'all'
    with pytest.raises(Exception, match='fixture resolver'):
        repair._install_configured_features_missing_deps(core)
    assert trace[0]['sync']['evict_incompatible_plugins'] is False


def test_helper_refuses_invalid_pending_before_pm(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    from hermes_cli.venv_sync import completion_pending_path
    pending = completion_pending_path(core)
    pending.parent.mkdir(parents=True)
    pending.write_text('{bad')
    with pytest.raises(ValueError):
        repair._install_configured_features_missing_deps(core)
    assert trace == []


def test_source_build_propagates_real_helper_failure(conflict):
    core, foreign, trace, repair, Scope, scoped = conflict
    from hermes_cli.source_build import build_update_products, ProductBuildError
    with pytest.raises(ProductBuildError, match='feature dependencies.*fixture resolver'):
        build_update_products(core, desktop=False, maintenance_scope=Scope.select('default'))
    from pm.receipt import latest
    assert latest()['outcome'] == 'failed'
    assert len(trace[1]['resolve']) == 2


def test_scope_type_is_bootstrap_safe_without_site(tmp_path):
    import subprocess
    import os
    code = '''
from pathlib import Path
from hermes_cli.maintenance_scope import ScopeSafetyRefusal, MaintenanceScope, maintenance_scope, require_scope_target, resolve_launch_scope
import sys
assert issubclass(ScopeSafetyRefusal, ValueError)
assert resolve_launch_scope(Path.cwd(), []).mode == 'all'
with maintenance_scope(MaintenanceScope.select('default')):
    try:
        require_scope_target(b'MAINTENANCE_SCOPE_SCHEMA = 0')
    except ScopeSafetyRefusal:
        pass
    else:
        raise AssertionError('older target accepted')
assert not {'yaml', 'pydantic', 'openai', 'litellm', 'gateway', 'pm.install'} & set(sys.modules)
'''
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home / '.hermes'))
    result = subprocess.run([sys.executable, '-S', '-c', code], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(('case', 'readonly'), [
    (case, readonly)
    for case in ['default', 'flagless', 'corrupt', 'stale', 'broaden', 'unreadable', 'mismatch', 'flagless-pm', 'explicit-pm', 'invalid', 'no-permission', 'all', 'all-fault', 'late-publication', 'fallback-publication', 'legacy-dispatch']
    for readonly in ([False] if case.startswith('all') else [False, True])
])
def test_public_bootstrap_scope_boundary(tmp_path, case, readonly):
    import subprocess
    probe = Path(__file__).with_name('scope_bootstrap_probe.py')
    work = tmp_path / case
    result = subprocess.run([sys.executable, str(probe), case, str(work), *(['--readonly'] if readonly else [])],
                            capture_output=True, text=True, timeout=30)
    data = json.loads((work / 'probe.json').read_text())
    assert data['code'] == result.returncode
    if case.startswith('all'):
        assert result.returncode == 0, result.stderr
        assert data['before'] != data['after']
        assert data['journal_after'] is None
        assert data['foreign_writes']
        assert 'activate_dependencies' in data['trace']
        assert 'recover_publication' in data['trace']
        if case == 'all-fault':
            assert 'fixture external PM query unavailable' in result.stderr
            assert 'running with the previous dependencies' in result.stderr
    else:
        assert result.returncode != 0, data
        assert data['before'] == data['after'], data
        assert data['journal_before'] == data['journal_after'], data
        assert data['foreign_writes'] == [], data
        forbidden = {'activate_dependencies', 'recover_publication', 'recover_if_needed', '_continue_legacy_post_swap'}
        if case not in {'late-publication', 'fallback-publication'}:
            forbidden.add('_settle_interrupted_update')
        assert not (forbidden & set(data['trace'])), data
        if case in {'late-publication', 'fallback-publication'}:
            assert 'prepare_launch' in data['trace']
        assert 'maintenance scope refused' in result.stderr
