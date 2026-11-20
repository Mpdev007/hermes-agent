"""Public PM install, fresh runtime and real lock-held recovery (no acquisition).

Only tool acquisition/availability, PM interpreter selection and dependency
build leaves are doubles. Public -m pm.cli parsing, runtime.run_cli transport,
pm.launch dispatch, scope, discovery, sync, locks, journal recovery and receipts
are native. Private foreign fixtures are read-only in restricted cases.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.hermes_cli.test_locked_publication_scope import fingerprint

SOURCE = Path(__file__).resolve().parents[2]

SETUP = r'''
import atexit, contextlib, importlib, json, os, sys
from pathlib import Path
ROOT = Path(os.environ['FIXTURE_ROOT'])
SOURCE = Path(os.environ['FIXTURE_SOURCE'])
CORE = ROOT / 'core'
HOME = ROOT / '.hermes'
JOURNAL = Path(os.environ['FIXTURE_JOURNAL'])
trace = []
import pm.paths
pm.paths.repo_root = lambda: CORE
import pm.runtime as runtime
inner = os.environ.get('FIXTURE_STAGE') == 'runtime'
runtime.is_runtime = lambda: inner
runtime.lease_current_runtime = lambda: None
if not inner:
    def command(script, args=(), **kwargs):
        trace.append({'reexec': list(args)})
        return [sys.executable, '-I', '-B', str(ROOT / 'runtime-fixture.py'), *args]
    runtime.runtime_command = command
install = importlib.import_module('pm.install')
install.ensure = lambda *a, **kw: None
install.activate = lambda *a, **kw: []
from pm.registry import get_package
from pm.workspace import enabled_member_dirs
def build(extras, **kwargs):
    members = kwargs.get('plugin_dirs')
    if members is None:
        members = enabled_member_dirs()
    from hermes_cli.maintenance_scope import current_scope
    trace.append({'build': [str(p) for p in members], 'scope': current_scope().to_dict()})
    return {}
get_package('venv').apply = build
real_lock = install._venv_install_lock
@contextlib.contextmanager
def lock(*a, **kw):
    with real_lock(*a, **kw):
        trace.append({'lock': True})
        if os.environ['FIXTURE_TIMING'] == 'late' and not JOURNAL.exists():
            JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            JOURNAL.write_bytes((ROOT / 'journal-input').read_bytes())
            trace.append({'late_journal': True})
        yield
install._venv_install_lock = lock
writes = []
def audit(event, args):
    if event == 'open':
        _, mode, flags = args
        write = isinstance(mode, str) and any(c in mode for c in 'wax+') or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    else:
        write = event in {'os.remove','os.rename','os.mkdir','os.rmdir','os.chmod','os.utime'}
    if write and any(isinstance(a, (str, bytes, os.PathLike)) and str(HOME / 'profiles') in os.fsdecode(a) for a in args):
        writes.append({'event': event, 'args': [str(a) for a in args]})
sys.addaudithook(audit)
def save():
    (ROOT / ('runtime.json' if inner else 'outer.json')).write_text(json.dumps({'trace': trace, 'foreign_writes': writes}))
atexit.register(save)
'''


@pytest.mark.parametrize('mode', ['default', 'pending', 'all', 'legacy'])
@pytest.mark.parametrize('target', ['foreign', 'own'])
@pytest.mark.parametrize('timing', ['existing', 'late'])
def test_public_install_scope_survives_reexec_and_locked_recovery(tmp_path, monkeypatch, mode, target, timing):
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion
    from pm.environments import install_state_dir

    home, core = tmp_path / '.hermes', tmp_path / 'core'
    core.mkdir()
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (core / 'pyproject.toml').write_text('[project]\nname="scope-fixture"\nversion="0"\nrequires-python=">=3.11"\ndependencies=[]\n[project.optional-dependencies]\nmcp=[]\n')
    (core / 'uv.lock').write_text('version = 1\n')
    (home / 'config.yaml').write_text('plugins:\n  enabled: []\n')
    for name in ['alpha', 'beta']:
        plugin = home / 'profiles' / name / 'plugins' / ('member-' + name)
        plugin.mkdir(parents=True)
        (plugin / 'plugin.yaml').write_text('name: member-' + name + '\n')
        (plugin / 'pyproject.toml').write_text('[project]\nname="member-' + name + '"\nversion="0"\ndependencies=["scope-fixture-lib==1"]\n')
        (plugin.parent.parent / 'config.yaml').write_text('plugins:\n  enabled: [member-' + name + ']\n')
    config = home / ('profiles/alpha/config.yaml' if target == 'foreign' else 'config.yaml')
    previous = config.read_bytes() + b'# restored by native recovery\n'
    row = {'facts_before': None, 'configs': [{'config': str(config),
           'previous': base64.b64encode(previous).decode(),
           'config_after': hashlib.sha256(config.read_bytes()).hexdigest()}]}
    raw = json.dumps(row).encode()
    journal = install_state_dir(core) / 'publication.json'
    journal.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'journal-input').write_bytes(raw)
    if mode == 'pending':
        arm_completion(core, maintenance_scope=MaintenanceScope.select('default'))
    if timing == 'existing':
        journal.write_bytes(raw)
    if mode in {'default', 'pending'}:
        for path in sorted((home / 'profiles').rglob('*'), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        (home / 'profiles').chmod(0o555)
    before = fingerprint(home / 'profiles')
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    (fixture / 'sitecustomize.py').write_text(SETUP)
    (tmp_path / 'runtime-fixture.py').write_text(
        'import os, runpy, sys\n'
        'os.environ["FIXTURE_STAGE"] = "runtime"\n'
        'sys.path.insert(0, os.environ["FIXTURE_SOURCE"])\n'
        'runpy.run_path(' + repr(str(fixture / 'sitecustomize.py')) + ')\n'
        'runpy.run_path(' + repr(str(SOURCE / 'pm/launch.py')) + ', run_name="__main__")\n')
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(home),
               PYTHONPATH=os.pathsep.join([str(fixture), str(SOURCE)]),
               FIXTURE_ROOT=str(tmp_path), FIXTURE_SOURCE=str(SOURCE),
               FIXTURE_JOURNAL=str(journal), FIXTURE_TIMING=timing)
    args = ['install', '--extra', 'mcp']
    if mode in {'default', 'all'}:
        args += ['--maintenance-scope', mode]
    command = [sys.executable, '-B', '-m', 'pm.cli', *args]
    result = subprocess.run(command, cwd=core, env=env, capture_output=True, text=True, timeout=30)
    (tmp_path / 'public-cli.stdout').write_text(result.stdout)
    (tmp_path / 'public-cli.stderr').write_text(result.stderr)
    outer = json.loads((tmp_path / 'outer.json').read_text())
    inner = json.loads((tmp_path / 'runtime.json').read_text())
    after = fingerprint(home / 'profiles')
    receipt = json.dumps({'command': command,
        'returncode': result.returncode, 'before': before, 'after': after,
        'outer': outer, 'inner': inner}, indent=2)
    (tmp_path / 'evidence.json').write_text(receipt)
    if destination := os.environ.get('HERMES_SCOPE_EVIDENCE'):
        output = Path(destination) / f'{mode}-{target}-{timing}'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'evidence.json').write_text(receipt)
        (output / 'stdout.txt').write_text(result.stdout)
        (output / 'stderr.txt').write_text(result.stderr)
    expected = 'all' if mode in {'all', 'legacy'} else 'default'
    assert outer['trace'][0]['reexec'][-2:] == ['--maintenance-scope', expected]
    assert {'lock': True} in inner['trace']
    if timing == 'late':
        assert {'late_journal': True} in inner['trace']
    if mode in {'default', 'pending'} and target == 'foreign':
        assert result.returncode == 1, result.stderr
        assert 'maintenance scope refused' in result.stderr
        assert journal.read_bytes() == raw
        assert not any('build' in row for row in inner['trace'])
    else:
        assert result.returncode == 0, result.stderr
        assert config.read_bytes() == previous
        assert not journal.exists()
        build = next(row for row in inner['trace'] if 'build' in row)
        assert len(build['build']) == 2  # foreign enabled requirements are never dropped
        assert build['scope']['mode'] == expected
    if mode in {'default', 'pending'} or target == 'own':
        assert before == after
        assert not inner['foreign_writes'] and not outer['foreign_writes']
    else:
        assert before != after and inner['foreign_writes']


def test_pm_install_bootstrap_scope_without_update_restart_flag(tmp_path, monkeypatch):
    from hermes_cli.maintenance_scope import resolve_launch_scope
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert resolve_launch_scope(tmp_path / 'core', ['pm', 'install', '--maintenance-scope', 'default']).mode == 'default'


def test_public_module_install_help():
    result = subprocess.run([sys.executable, '-B', '-m', 'pm.cli', 'install', '--help'],
                            cwd=SOURCE, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert '--maintenance-scope {all,default}' in result.stdout
