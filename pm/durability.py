"""Publication barriers for completed PM generations; no acquisition or selection.

Linux syncfs waits for one filesystem, not every mounted device. Discover all
referenced payload filesystems before flushing; callers keep publication locks
held until their existing atomic selection write completes.
"""
from __future__ import annotations

from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import unquote, urlsplit


def _tool_root(binary: Path) -> Path:
    directory = Path(binary).absolute().parent
    return directory.parent if directory.name in ("bin", "Scripts") else directory


def generation_roots(environment: Path, *, python: Path | None = None) -> tuple[Path, ...]:
    """Actual generation (including editable workspace), uv and interpreter payloads.

    A venv's bin/python may point outside its filesystem. The store may itself
    be overridden/split; do not infer it from HOME or flush an entire tools cache.
    pyvenv.cfg also covers copied interpreters whose base is not a symlink.
    """
    from pm._uv import _toolchain

    environment = Path(environment).absolute()
    tools = _toolchain(realize=False)
    if tools is None:
        raise RuntimeError("cannot flush generation: PM toolchain is unavailable")
    # uv is a self-contained executable and can live in a shared bin directory.
    # Do not recursively treat its unrelated siblings as generation payloads.
    roots = [environment.parent, Path(tools[0]).absolute(), _tool_root(tools[1])]
    if python is not None:
        roots.append(_tool_root(python))
    for line in (environment / "pyvenv.cfg").read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            home = Path(value.strip())
            if not home.is_absolute():
                home = environment / home
            roots.append(home.parent if home.name in ("bin", "Scripts") else home)
    return tuple(dict.fromkeys(roots))


def _editable_paths(path: Path):
    """Read uv's editable references as data; never execute a .pth import line.

    Snapshotted app members usually stay inside the generation. Project side
    environments and external editable sources need not. PEP 610 metadata
    covers PEP 660 import-hook editables without interpreting their Python code.
    """
    if path.suffix == ".pth" and path.parent.name in ("site-packages", "dist-packages"):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.rstrip()
            if not line or line.startswith(("#", "import ", "import\t")):
                continue
            target = path.parent / line
            # site.addpackage ignores nonexistent paths too.
            if target.exists():
                yield target
    elif (path.name == "direct_url.json" and path.parent.name.endswith(".dist-info")
          and path.parent.parent.name in ("site-packages", "dist-packages")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("dir_info", {}).get("editable"):
            url = urlsplit(data["url"])
            if url.scheme != "file" or url.netloc not in ("", "localhost"):
                raise ValueError(f"unsupported editable source: {path}")
            yield Path(unquote(url.path))


def _payload_paths(roots):
    """Walk only published trees and their references, including nested mounts.

    No per-file flush: the metadata walk finds filesystems (and wheel-cache
    symlink targets). Hardlinks/reflinks already share the destination device.
    Directory inode identities bound cycles; dangling links are metadata only.
    Permission/read errors refuse publication instead of dropping a filesystem.
    """
    pending = list(reversed(roots))
    visited = set()
    while pending:
        path = Path(pending.pop())
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            yield path.parent  # the link itself, before following its target
            try:
                target = path.resolve(strict=True)
            except FileNotFoundError:
                continue
            info = target.stat()
            yield target
            # Keep the installed spelling while walking/parsing metadata: a
            # symlinked .pth resolves its relative entries at site-packages,
            # not at the cached wheel's source directory.
        # Relative .pth lines are interpreted at each installation location,
        # even when uv hardlinks identical metadata at two different paths.
        identity = (info.st_dev, info.st_ino)
        if stat.S_ISREG(info.st_mode):
            identity += (os.path.abspath(path),)
        if identity in visited:
            continue
        visited.add(identity)
        if stat.S_ISDIR(info.st_mode):
            yield path
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            yield path
            pending.extend(_editable_paths(path))
        else:
            raise ValueError(f"not a regular publication payload: {path}")


def _bind_syncfs():
    """Checked libc ABI; Python does not currently expose os.syncfs.

    Never fall back to global sync on Linux, including a missing libc symbol
    or an unsupported syscall. Those failures must leave selection unchanged.
    """
    import ctypes

    try:
        syncfs = ctypes.CDLL(None, use_errno=True).syncfs
    except (AttributeError, OSError) as exc:
        raise RuntimeError("Linux publication requires libc syncfs") from exc
    syncfs.argtypes = [ctypes.c_int]
    syncfs.restype = ctypes.c_int

    def flush(fd: int) -> None:
        while True:
            ctypes.set_errno(0)
            if syncfs(fd) == 0:
                return
            error = ctypes.get_errno() or errno.EIO
            if error != errno.EINTR:
                raise OSError(error, os.strerror(error))

    return flush


def flush_filesystems(roots) -> None:
    roots = tuple(roots)
    if not roots:
        raise ValueError("publication requires explicit payload roots")
    if not (sys.platform.startswith("linux") or sys.platform == "android"):
        # Deliberately retain the legacy non-Linux contract. macOS uses sync;
        # Windows has no os.sync and still promises atomic records, not durable
        # payload contents. This change does not invent a Windows guarantee.
        sync = getattr(os, "sync", None)
        if sync is not None:
            sync()
        return
    flush = _bind_syncfs()
    with ExitStack() as cleanup:
        filesystems = {}
        for path in _payload_paths(roots):
            device = os.stat(path).st_dev
            if device in filesystems:
                continue
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            cleanup.callback(os.close, fd)
            if os.fstat(fd).st_dev != device:
                raise RuntimeError(f"publication filesystem changed while opening {path}")
            filesystems[device] = fd
        # All roots must exist and all discovery must succeed first. Keep fds
        # open through the complete barrier; any failure prevents selection.
        for fd in filesystems.values():
            flush(fd)
