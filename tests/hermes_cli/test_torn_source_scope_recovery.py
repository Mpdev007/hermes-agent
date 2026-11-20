"""Source-only recovery must not discharge or widen profile-maintenance obligations.

Real bootstrap/reexec, Git restoration and custody; the selected-runtime fixture
uses an inert CLI and prebuilt dependency selection (no build or deployment claim).
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli import _launchers
from hermes_cli.maintenance_scope import MaintenanceScope
from hermes_cli.venv_sync import arm_completion
from pm.environments import install_state_dir
from tests.hermes_cli.test_source_launcher_publication import _torn_by_a_killed_merge


def fingerprint(root):
    result = {}
    for path in (root, *sorted(root.rglob('*'))):
        stat = path.lstat()
        result[str(path.relative_to(root))] = (
            stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
            stat.st_uid, stat.st_gid, stat.st_ino,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
    return result


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('damage', ['missing', 'truncated'])
@pytest.mark.parametrize('case', ['publication', 'corrupt', 'permitted'])
def test_source_restore_rechecks_pending_scope_before_runtime(tmp_path, monkeypatch, damage, case):
    repo, home, interpreter, pre, original, marker = _torn_by_a_killed_merge(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    constants = repo / 'hermes_constants.py'
    if damage == 'truncated':
        # A prefix of the actual tracked source, attributable to Git's torn write.
        constants.write_bytes(original[:80])
    pending = arm_completion(repo, maintenance_scope=MaintenanceScope.select('default'))
    profiles = home / 'profiles'
    rows = []
    for name in ('alpha', 'beta'):
        config = profiles / name / 'config.yaml'
        config.parent.mkdir(parents=True)
        config.write_bytes(b'plugins:\n  disabled: [foreign]\n')
        rows.append({'config': str(config),
                     'previous': base64.b64encode(b'plugins:\n  enabled: [foreign]\n').decode(),
                     'config_after': hashlib.sha256(config.read_bytes()).hexdigest()})
    journal = install_state_dir(repo) / 'publication.json'
    if case == 'publication':
        journal.write_text(json.dumps({'configs': rows, 'facts_before': None}), encoding='utf-8')
    elif case == 'corrupt':
        pending.write_text('{broken', encoding='utf-8')
    before_profiles = fingerprint(profiles)
    before_pending = fingerprint(pending)
    before_journal = fingerprint(journal) if journal.exists() else None
    env = dict(os.environ)
    env.pop('PYTEST_CURRENT_TEST', None)
    env.pop('HERMES_HOME', None)  # exercise delayed default-home pin too
    command = _launchers.runtime_command(repo, python=interpreter)
    result = subprocess.run([*command, '--help'], cwd=tmp_path, env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=60, check=False)
    assert constants.is_file(), result.stderr
    assert constants.read_bytes() == original, result.stderr
    assert not marker.exists() and not (repo / '.git/index.lock').exists()
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert head == pre
    if case == 'permitted':
        assert result.returncode == 7, result.stdout + result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1])['value'] == 'repaired'
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert 'maintenance scope refused:' in result.stderr
        expected = {'publication': 'pending shared dependency publication',
                    'corrupt': 'invalid pending maintenance scope'}[case]
        assert expected in result.stderr
        assert 'source-update completion failed' not in result.stderr
    assert fingerprint(profiles) == before_profiles
    assert fingerprint(pending) == before_pending
    assert (fingerprint(journal) if journal.exists() else None) == before_journal


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('blocked', ['unmarked', 'local-edit'])
def test_unprovable_scope_never_falls_through_when_source_cannot_restore(tmp_path, monkeypatch, blocked):
    repo, home, interpreter, _, _, marker = _torn_by_a_killed_merge(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    pending = arm_completion(repo, maintenance_scope=MaintenanceScope.select('default'))
    constants = repo / 'hermes_constants.py'
    if blocked == 'unmarked':
        marker.unlink()  # no native recovery provenance exists
    else:
        constants.write_bytes(b'raise ImportError("local edit must not be overwritten")\n')
    before_pending = fingerprint(pending)
    before_source = constants.read_bytes() if constants.exists() else None
    env = dict(os.environ)
    env.pop('PYTEST_CURRENT_TEST', None)
    env.pop('HERMES_HOME', None)
    result = subprocess.run([*_launchers.runtime_command(repo, python=interpreter), '--help'],
                            cwd=tmp_path, env=env, capture_output=True, text=True,
                            encoding='utf-8', timeout=60, check=False)
    assert result.returncode != 0
    assert '"value": "repaired"' not in result.stdout
    assert (constants.read_bytes() if constants.exists() else None) == before_source
    assert fingerprint(pending) == before_pending
    if blocked == 'local-edit':
        # Native recovery retires a completed source journal when only an
        # unrelated local edit remains; the scoped completion debt is separate.
        assert not marker.exists()
        assert 'local edit must not be overwritten' in result.stderr
