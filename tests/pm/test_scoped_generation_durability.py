"""Mocked durability boundaries only: never issue sync/syncfs or build a venv."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def no_real_flush(monkeypatch):
    monkeypatch.setattr(os, "sync", lambda: pytest.fail("global sync must not run"), raising=False)
    # Fail even if a new test forgets to mock the Linux binding.
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: pytest.fail("real libc must not load"))


def _tree(tmp_path, monkeypatch):
    from pm import _uv
    generation = tmp_path / "generation"
    venv = generation / "venv"
    (venv / "bin").mkdir(parents=True)
    python = tmp_path / "store" / "python" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    uv = tmp_path / "other-store" / "uv" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()
    (venv / "bin/python").symlink_to(python)
    (venv / "pyvenv.cfg").write_text(f"home = {python.parent}\n")
    monkeypatch.setattr(_uv, "_toolchain", lambda **kw: (uv, python))
    return generation, venv, uv, python


def test_roots_include_snapshot_and_actual_tool_payloads(tmp_path, monkeypatch):
    from pm.durability import generation_roots
    generation, venv, uv, python = _tree(tmp_path, monkeypatch)
    assert set(generation_roots(venv)) == {generation, uv, python.parent.parent}


def test_uv_shared_directory_siblings_are_not_publication_payload(tmp_path, monkeypatch):
    from pm import _uv
    from pm.durability import generation_roots, _payload_paths
    _, venv, _, python = _tree(tmp_path, monkeypatch)
    shared = tmp_path / "shared-bin"
    shared.mkdir()
    uv = shared / "uv"
    uv.touch()
    unrelated = shared / "unrelated-loop"
    unrelated.symlink_to(unrelated.name)
    monkeypatch.setattr(_uv, "_toolchain", lambda **kw: (uv, python))
    roots = generation_roots(venv)
    assert uv in roots and shared not in roots
    visited = set(_payload_paths(roots))
    assert uv in visited and unrelated not in visited


def test_missing_toolchain_refuses_publication(tmp_path, monkeypatch):
    from pm import _uv
    from pm.durability import generation_roots
    _, venv, _, _ = _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(_uv, "_toolchain", lambda **kw: None)
    with pytest.raises(RuntimeError, match="toolchain"):
        generation_roots(venv)


def test_payload_discovery_covers_links_editables_and_snapshot(tmp_path, monkeypatch):
    from pm.durability import _payload_paths
    generation, venv, _, _ = _tree(tmp_path, monkeypatch)
    workspace = generation / "workspace"
    workspace.mkdir()
    (workspace / "uv.lock").touch()
    external = tmp_path / "editable"
    external.mkdir()
    (external / "module.py").touch()
    site = venv / "lib/python3.14/site-packages"
    site.mkdir(parents=True)
    (site / "external.pth").write_text(str(external) + "\nimport never_execute_this\n")
    other = tmp_path / "pep660"
    other.mkdir()
    dist = site / "other.dist-info"
    dist.mkdir()
    (dist / "direct_url.json").write_text(json.dumps({"url": other.as_uri(), "dir_info": {"editable": True}}))
    (workspace / "loop").symlink_to(generation, target_is_directory=True)
    # A dangling link is payload metadata, not a newly installed target.
    (workspace / "dangling").symlink_to(tmp_path / "absent")
    visited = set(_payload_paths((generation,)))
    assert {generation, workspace / "uv.lock", external, external / "module.py", other} <= visited
    assert tmp_path / "unreferenced-usb" not in visited


def _fake_devices(monkeypatch, roots):
    """Model separate mounted filesystems without creating mounts or touching USB."""
    from pm import durability
    real_stat, real_fstat, real_open, real_close = os.stat, os.fstat, os.open, os.close
    opened, closed = {}, []
    devices = {str(p): dev for p, dev in roots}
    def device_for(path):
        matches = [root for root in devices if str(path) == root or str(path).startswith(root + os.sep)]
        return devices[max(matches, key=len)] if matches else None
    def stat(path, *args, **kwargs):
        actual = real_stat(path, *args, **kwargs)
        device = device_for(path)
        if device is None:
            return actual
        fields = list(actual)
        fields[2] = device
        return os.stat_result(fields)
    def open_(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if device_for(path) is not None:
            opened[fd] = str(path)
        return fd
    def fstat(fd):
        actual = real_fstat(fd)
        if fd not in opened:
            return actual
        fields = list(actual)
        fields[2] = device_for(opened[fd])
        return os.stat_result(fields)
    def close(fd):
        if fd in opened and fd not in closed:
            closed.append(fd)
        real_close(fd)
    monkeypatch.setattr(durability, "_payload_paths", lambda _: (p for p, _ in roots))
    monkeypatch.setattr(durability.os, "stat", stat)
    monkeypatch.setattr(durability.os, "fstat", fstat)
    monkeypatch.setattr(durability.os, "open", open_)
    monkeypatch.setattr(durability.os, "close", close)
    return opened, closed


def test_flushes_each_referenced_filesystem_once_and_closes_fds(tmp_path, monkeypatch):
    from pm import durability, environments
    roots = [(tmp_path / n, dev) for n, dev in (("generation", 11), ("snapshot", 11), ("python", 22), ("uv", 33))]
    for path, _ in roots:
        path.mkdir()
    opened, closed = _fake_devices(monkeypatch, roots)
    flushed = []
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: flushed.append(opened[fd]))
    monkeypatch.setattr(durability.sys, "platform", "linux")
    environments.flush_before_selecting(*(p for p, _ in roots))
    assert flushed == [str(roots[i][0]) for i in (0, 2, 3)]
    assert sorted(closed) == sorted(opened)


@pytest.mark.parametrize("failure", [errno.EIO, errno.ENOSYS, errno.ENOSPC])
def test_flush_error_propagates_and_closes_all_fds(tmp_path, monkeypatch, failure):
    from pm import durability
    roots = [(tmp_path / "generation", 11), (tmp_path / "python", 22)]
    for path, _ in roots:
        path.mkdir()
    opened, closed = _fake_devices(monkeypatch, roots)
    calls = []
    def syncfs(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError(failure, "mock writeback failure")
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: syncfs)
    with pytest.raises(OSError) as caught:
        durability.flush_filesystems([p for p, _ in roots])
    assert caught.value.errno == failure
    assert sorted(closed) == sorted(opened)


def test_missing_root_closes_fds_without_flushing(tmp_path, monkeypatch):
    from pm import durability
    existing = tmp_path / "generation"
    existing.mkdir()
    closed = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[-1])
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: pytest.fail("flush before complete discovery"))
    with pytest.raises(FileNotFoundError):
        durability.flush_filesystems([existing, tmp_path / "missing"])
    assert len(closed) == 1


def test_binding_declares_abi_retries_eintr_and_checks_errno(monkeypatch):
    from pm import durability
    results = iter([(-1, errno.EINTR), (0, 0), (-1, errno.EIO), (-1, 0)])
    calls = []
    class Native:
        def __call__(self, fd):
            calls.append(fd)
            result, error = next(results)
            ctypes.set_errno(error)
            return result
    native = Native()
    loads = []
    monkeypatch.setattr(ctypes, "CDLL", lambda name, **kw: (loads.append((name, kw)), SimpleNamespace(syncfs=native))[1])
    flush = durability._bind_syncfs()
    flush(42)
    assert native.argtypes == [ctypes.c_int]
    assert native.restype is ctypes.c_int
    assert loads == [(None, {"use_errno": True})]
    assert calls == [42, 42]
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            flush(42)
        assert caught.value.errno == errno.EIO


@pytest.mark.parametrize("missing", ["symbol", "library"])
def test_binding_unavailable_is_fatal(monkeypatch, missing):
    from pm import durability
    def load(*args, **kwargs):
        if missing == "library":
            raise OSError("not available")
        return object()
    monkeypatch.setattr(ctypes, "CDLL", load)
    with pytest.raises(RuntimeError, match="syncfs"):
        durability._bind_syncfs()


def test_empty_roots_refused(monkeypatch):
    from pm import environments
    with pytest.raises(ValueError):
        environments.flush_before_selecting()


def test_non_linux_keeps_explicit_legacy_contract(tmp_path, monkeypatch):
    from pm import durability
    called = []
    monkeypatch.setattr(durability.sys, "platform", "darwin")
    monkeypatch.setattr(os, "sync", lambda: called.append("sync"))
    durability.flush_filesystems([tmp_path])
    assert called == ["sync"]
    monkeypatch.setattr(durability.sys, "platform", "win32")
    monkeypatch.delattr(os, "sync")
    durability.flush_filesystems([tmp_path])  # no newly invented Windows guarantee


def _mock_generation(generation, base):
    from pm import operations
    venv = generation / "venv"
    python = operations._python(venv)
    python.parent.mkdir(parents=True)
    python.touch()
    (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\n")
    (generation / "uv.lock").write_text("fixture lock\n")
    return python


@pytest.mark.parametrize("failure", ["writeback", "binding", "discovery"])
def test_side_previous_selection_survives_flush_failure(tmp_path, monkeypatch, failure):
    from pm import durability, operations
    _, _, _, base = _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: None)
    root = tmp_path / "side"
    old_python = operations._ensure_generation("proof", root, {"version": 1}, _mock_generation,
                                               record={}, explicit=True)
    before = (root / "active.json").read_bytes()
    calls = []
    def refuse(*args):
        calls.append(args)
        assert (root / "active.json").read_bytes() == before
        if failure == "binding":
            raise RuntimeError("mock missing syncfs binding")
        raise OSError(errno.EIO, failure)
    if failure == "writeback":
        monkeypatch.setattr(durability, "_bind_syncfs", lambda: refuse)
    elif failure == "binding":
        monkeypatch.setattr(durability, "_bind_syncfs", refuse)
    else:
        monkeypatch.setattr(durability, "_payload_paths", refuse)
    with pytest.raises(RuntimeError if failure == "binding" else OSError):
        operations._ensure_generation("proof", root, {"version": 2}, _mock_generation,
                                      record={}, explicit=True)
    assert calls
    assert (root / "active.json").read_bytes() == before
    assert operations.environment_python("proof", root=root) == old_python
    assert sorted(root.glob("gen-*")) == [old_python.parent.parent.parent]


def test_side_current_generation_never_flushes_or_builds(tmp_path, monkeypatch):
    from pm import durability, operations
    _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: None)
    root = tmp_path / "side"
    old = operations._ensure_generation("proof", root, {}, _mock_generation, record={}, explicit=True)
    def unexpected(*args, **kwargs):
        pytest.fail("current selection must not build, discover or flush")
    monkeypatch.setattr(durability, "generation_roots", unexpected)
    monkeypatch.setattr(durability, "flush_filesystems", unexpected)
    assert operations._ensure_generation("proof", root, {}, unexpected, record={}, explicit=True) == old


def _application(tmp_path, monkeypatch):
    from pm import install
    from pm.lock import Facts
    import hermes_cli.runtime_state as runtime
    generation, venv, uv, base = _tree(tmp_path, monkeypatch)
    facts = Facts(tmp_path / "facts.json")
    facts.record_state("venv", "old", [], environment=tmp_path / "old/venv")
    monkeypatch.setattr(install.paths, "repo_root", lambda: tmp_path)
    recovery = []
    monkeypatch.setattr(runtime, "recover_publication", lambda root: recovery.append(root))
    monkeypatch.setattr(runtime, "finish_publication", lambda root: None)
    package = SimpleNamespace(apply=lambda *a, **k: {"environment": venv},
                              expected_stamp=lambda *a, **k: "new")
    def commit(current=False, change=None):
        install._commit_selection(package, facts, change, enabled=[], stamp="new", inputs={},
                                  current=current, repair=False, explicit=True)
    return commit, tmp_path / "facts.json", (generation, uv, base.parent.parent), recovery


@pytest.mark.parametrize("failure", ["writeback", "binding", "discovery"])
def test_application_previous_facts_survive_flush_failure(tmp_path, monkeypatch, failure):
    from pm import durability
    commit, facts, roots, recovery = _application(tmp_path, monkeypatch)
    before = facts.read_bytes()
    calls = []
    def refuse(*args):
        calls.append(args)
        assert facts.read_bytes() == before
        if failure == "binding":
            raise RuntimeError("mock missing syncfs binding")
        raise OSError(errno.EIO, failure)
    if failure == "writeback":
        monkeypatch.setattr(durability, "_bind_syncfs", lambda: refuse)
    elif failure == "binding":
        monkeypatch.setattr(durability, "_bind_syncfs", refuse)
    else:
        monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: None)
        monkeypatch.setattr(durability, "_payload_paths", refuse)
    with pytest.raises(RuntimeError if failure == "binding" else OSError):
        commit()
    assert calls and recovery == [tmp_path]
    assert facts.read_bytes() == before


def test_application_selection_waits_for_all_filesystems(tmp_path, monkeypatch):
    from pm import durability
    commit, facts, roots, _ = _application(tmp_path, monkeypatch)
    before = facts.read_bytes()
    roots_with_devices = list(zip(roots, [11, 22, 33]))
    opened, closed = _fake_devices(monkeypatch, roots_with_devices)
    calls = []
    def syncfs(fd):
        assert facts.read_bytes() == before
        calls.append(opened[fd])
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: syncfs)
    commit()
    assert calls == list(map(str, roots))
    assert sorted(closed) == sorted(opened)
    assert facts.read_bytes() != before


def test_application_current_selection_never_flushes(tmp_path, monkeypatch):
    from pm import durability
    commit, facts, _, _ = _application(tmp_path, monkeypatch)
    before = facts.read_bytes()
    monkeypatch.setattr(durability, "generation_roots", lambda *a, **k: pytest.fail("discovery on cache hit"))
    monkeypatch.setattr(durability, "flush_filesystems", lambda *a, **k: pytest.fail("flush on cache hit"))
    commit(current=True)
    assert facts.read_bytes() == before


def test_side_selection_waits_for_complete_explicit_barrier(tmp_path, monkeypatch):
    from pm import durability, operations
    _, _, uv, base = _tree(tmp_path, monkeypatch)
    root = tmp_path / "side"
    calls = []
    def barrier(roots):
        assert not (root / "active.json").exists()
        assert len(roots) == 3
        generation, *tools = roots
        assert (generation / "uv.lock").is_file()
        assert set(tools) == {uv, base.parent.parent}
        calls.extend(roots)
    monkeypatch.setattr(durability, "flush_filesystems", barrier)
    operations._ensure_generation("proof", root, {}, _mock_generation, record={}, explicit=True)
    assert len(calls) == 3 and (root / "active.json").is_file()


def test_open_or_device_race_failure_closes_held_descriptors(tmp_path, monkeypatch):
    from pm import durability
    roots = [(tmp_path / "generation", 11), (tmp_path / "python", 22)]
    for path, _ in roots:
        path.mkdir()
    opened, closed = _fake_devices(monkeypatch, roots)
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: pytest.fail("partial discovery"))
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_dev=999))
    with pytest.raises(RuntimeError, match="filesystem changed"):
        durability.flush_filesystems([p for p, _ in roots])
    assert len(closed) == 1 and sorted(closed) == sorted(opened)


def test_staged_plugin_payload_is_explicitly_included(tmp_path, monkeypatch):
    from pm import durability
    from pm.publication import StagedPlugin
    commit, _, roots, _ = _application(tmp_path, monkeypatch)
    # Only exercise _commit_selection's publication-root boundary, not an install.
    change = object.__new__(StagedPlugin)
    change.target = tmp_path / "plugins/demo"
    change.metadata = tmp_path / "plugins/.install-metadata.json"
    monkeypatch.setattr(StagedPlugin, "publish", lambda self, root: None)
    observed = []
    monkeypatch.setattr(durability, "flush_filesystems", lambda requested: observed.extend(requested))
    commit(change=change)
    assert observed == [*roots, change.target, change.metadata]


def test_actual_discovery_covers_nested_mount_and_external_editable_filesystems(tmp_path, monkeypatch):
    from pm import durability
    generation, venv, uv, base = _tree(tmp_path, monkeypatch)
    mount = generation / "workspace"
    mount.mkdir()
    (mount / "source.py").touch()
    external = tmp_path / "external"
    external.mkdir()
    (external / "module.py").touch()
    site = venv / "lib/python3.14/site-packages"
    site.mkdir(parents=True)
    (site / "external.pth").write_text(str(external) + "\n")
    unrelated = tmp_path / "unreferenced-usb"
    unrelated.mkdir()
    roots = [(generation, 11), (mount, 22), (uv, 33), (base.parent.parent, 44),
             (external, 55), (unrelated, 66)]
    real_discovery = durability._payload_paths
    opened, closed = _fake_devices(monkeypatch, roots)
    monkeypatch.setattr(durability, "_payload_paths", real_discovery)
    devices = []
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: devices.append(os.fstat(fd).st_dev))
    durability.flush_filesystems(durability.generation_roots(venv))
    assert set(devices) == {11, 22, 33, 44, 55}
    assert len(devices) == 5  # including the base interpreter also reached by a venv symlink
    assert sorted(closed) == sorted(opened)


def test_open_failure_closes_earlier_descriptors(tmp_path, monkeypatch):
    from pm import durability
    roots = [(tmp_path / "generation", 11), (tmp_path / "python", 22)]
    for path, _ in roots:
        path.mkdir()
    opened, closed = _fake_devices(monkeypatch, roots)
    original_open = os.open
    def fail_second(path, flags):
        if path == roots[1][0]:
            raise PermissionError(errno.EACCES, "fixture access failure")
        return original_open(path, flags)
    monkeypatch.setattr(os, "open", fail_second)
    monkeypatch.setattr(durability, "_bind_syncfs", lambda: lambda fd: pytest.fail("partial discovery"))
    with pytest.raises(PermissionError):
        durability.flush_filesystems([p for p, _ in roots])
    assert len(closed) == 1 and sorted(closed) == sorted(opened)


def test_eviction_config_records_do_not_require_a_single_path_attribute(tmp_path, monkeypatch):
    from pm import durability
    from pm.plugin_eviction import PluginEviction
    commit, _, roots, _ = _application(tmp_path, monkeypatch)
    # Config records are individually durable in PluginEviction.publish; the
    # generation barrier must still work for this multi-config publication type.
    change = object.__new__(PluginEviction)
    monkeypatch.setattr(PluginEviction, "publish", lambda self, root: None)
    observed = []
    monkeypatch.setattr(durability, "flush_filesystems", lambda requested: observed.extend(requested))
    commit(change=change)
    assert observed == list(roots)


@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_linked_pth_is_resolved_from_each_installed_location(tmp_path, link_kind):
    from pm.durability import _payload_paths
    sites = [tmp_path / name / "site-packages" for name in ("a", "b")]
    for site in sites:
        site.mkdir(parents=True)
        (site.parent / "source").mkdir()
    first = sites[0] / "project.pth"
    first.write_text("../source\n")
    second = sites[1] / "project.pth"
    if link_kind == "hardlink":
        second.hardlink_to(first)
    else:
        second.symlink_to(first)
    visited = {path.resolve() for path in _payload_paths(sites)}
    assert {site.parent / "source" for site in sites} <= visited
