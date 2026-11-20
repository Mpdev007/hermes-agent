"""Real offline builds from read-only inputs without permission changes outside copies."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from pm import workspace
from pm.environment import PythonEnvironment
from tests.pm.test_workspace_build_inputs import _buildable_source


def _fingerprint(root):
    rows = {}
    for path in [root, *sorted(root.rglob('*'))]:
        st = path.lstat()
        rows[str(path.relative_to(root))] = (
            stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, st.st_uid, st.st_gid,
            os.readlink(path) if path.is_symlink() else
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
    return rows


def _readonly(root):
    for path in [*sorted(root.rglob('*'), reverse=True), root]:
        if not path.is_symlink():
            path.chmod(0o555 if path.is_dir() else 0o444)


@pytest.mark.platforms('posix')
def test_owner_write_normalization_never_follows_symlinks(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'value').write_text('untouched')
    _readonly(outside)
    before = _fingerprint(outside)
    copied = tmp_path / 'fresh-copy'
    copied.mkdir()
    (copied / 'file').write_text('copy')
    (copied / 'directory-link').symlink_to(outside, target_is_directory=True)
    (copied / 'file-link').symlink_to(outside / 'value')
    _readonly(copied)
    workspace._make_build_snapshot_writable(copied)
    assert _fingerprint(outside) == before
    assert stat.S_IMODE((copied / 'file').stat().st_mode) == 0o644
    (copied / 'file').write_text('writable private copy')
    with pytest.raises(workspace.InstallError, match='real directory'):
        workspace._make_build_snapshot_writable(copied / 'directory-link')
    assert _fingerprint(outside) == before


@pytest.mark.platforms('posix')
def test_real_build_and_replay_leave_readonly_sources_unchanged(tmp_path):
    uv = shutil.which('uv')
    assert uv, 'real native uv required, not a mocked build'
    core, plugin = tmp_path / 'core', tmp_path / 'plugin'
    core.mkdir()
    (core / 'pyproject.toml').write_text(
        '[project]\nname="readonly-core"\nversion="1"\nrequires-python=">=3.11"\n'
        '[tool.uv]\npackage=false\nno-index=true\n')
    _buildable_source(plugin)
    backend = plugin / 'build/backend.py'
    backend.write_text(backend.read_text().replace(
        '    assert Path("README.md")',
        '    Path("generated.marker").write_text("private backend write")\n'
        '    assert Path("README.md")', 1))
    _readonly(core)
    _readonly(plugin)
    original = (_fingerprint(core), _fingerprint(plugin))

    def engine(name):
        return PythonEnvironment(uv=Path(uv), python=Path(sys.executable),
            destination=tmp_path / (name + '-env'), cache=tmp_path / (name + '-cache'),
            env=dict(os.environ), offline=True)

    saved = tmp_path / 'saved'
    first = engine('initial')
    workspace.lock_and_sync([plugin], [], root=saved, source=core,
                            seed_lock=None, environment=first)
    assert list((saved / 'plugin-sources').glob('*/generated.marker'))
    assert (_fingerprint(core), _fingerprint(plugin)) == original
    _readonly(saved)
    saved_before = _fingerprint(saved)
    repaired = tmp_path / 'repaired'
    second = engine('repair')
    workspace.lock_and_sync([plugin], [], root=repaired, source=core,
                            seed_lock=None, replay=saved, environment=second)
    assert _fingerprint(saved) == saved_before
    assert (_fingerprint(core), _fingerprint(plugin)) == original
    for env in (first, second):
        probe = subprocess.run([str(env.executable), '-I', '-c',
            'import replay_plugin; print(replay_plugin.VALUE)'], cwd=tmp_path,
            text=True, capture_output=True, timeout=30, check=True)
        assert probe.stdout.strip() == 'recorded plugin bytes'
