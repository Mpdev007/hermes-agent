#!/usr/bin/env python3
"""No-provisioning bootstrap for the explicit controlled-policy test lane.

Invoked only through run_tests.sh with -I -S -B. No site initialization,
.pth evaluation, pytest parent import, inherited plugin or environment fallback.
This is a reviewed-fixture launcher, not an OS sandbox for hostile native code.
"""
from importlib.machinery import PathFinder
import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys


def prepare_paths():
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise RuntimeError("offline launcher requires -I -S -B")
    root = Path(__file__).resolve().parents[1]
    prefix = Path(sys.executable).absolute().parent.parent
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    if not site.is_dir() or any(PathFinder.find_spec(name, [str(site)]) is None
                                for name in ("pytest", "_pytest")):
        raise RuntimeError("existing test interpreter lacks pytest; no provisioning")
    # Path insertion does not execute .pth files or import pytest. -S on older
    # Python does not set the venv prefix, so bind that known existing prefix.
    sys.prefix = sys.exec_prefix = str(prefix)
    sys.path.insert(0, str(root))
    sys.path.append(str(site))
    sys.dont_write_bytecode = True
    return root


def validate_locations():
    scratch = Path(os.environ["TMPDIR"]).resolve(strict=True)
    home = Path(os.environ["HOME"]).resolve(strict=True)
    hermes = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
    if scratch == home or not home.is_relative_to(scratch) or not hermes.is_relative_to(home):
        raise RuntimeError("offline lane requires HOME and HERMES_HOME inside its own scratch root")
    if os.environ.get("__HERMES_TESTS_REEXEC") != "1":
        raise RuntimeError("offline lane requires the exact no-reexec flag")


def serial_files(root, args):
    """Explicit lane only: serial children in the outer driver's owned session.

    No pytest import, host-process enumeration, duration cache, stale-root sweep,
    forkserver, retry or installation. The fence forbids child process creation.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("-j", type=int, default=1)
    parser.add_argument("--file-retries", type=int, default=0)
    parser.add_argument("--file-timeout", type=int, default=60)
    options, pytest_args = parser.parse_known_args(args)
    if options.j != 1 or options.file_retries != 0 or not 1 <= options.file_timeout <= 60:
        raise RuntimeError("controlled lane is serial, zero-retry, timeout at most60s")
    status = 0
    for index, name in enumerate(options.files):
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root / "tests") or path.suffix != ".py":
            raise RuntimeError("only explicit tests under this checkout")
        scratch = Path(os.environ["TMPDIR"]) / f"file-{index}"
        hermes = scratch / "home/.hermes"
        hermes.mkdir(parents=True)
        env = {**os.environ, "TMPDIR": str(scratch), "PYTEST_DEBUG_TEMPROOT": str(scratch),
               "HOME": str(scratch / "home"), "HERMES_HOME": str(hermes),
               "POLICY_FENCE_TEST": path.relative_to(root).as_posix()}
        command = [sys.executable, "-I", "-S", "-B", str(Path(__file__)),
                   "--child", name, *pytest_args]
        # Inherit the supervisor's group. Only the outer driver creates its
        # private session, so a timeout can terminate this entire owned group.
        child = subprocess.Popen(command, cwd=root, env=env, start_new_session=False)
        try:
            result = child.wait(timeout=options.file_timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            result = 124
        status = status or result
        print(f"OFFLINE_FILE file={name} exit={result}", flush=True)
    return status


def main():
    root = prepare_paths()
    validate_locations()
    child = sys.argv[1:2] == ["--child"]
    args = sys.argv[2:] if child else sys.argv[1:]
    required = {"tests.offline_policy_fence", "tests.offline_policy_fixtures",
                "--disable-plugin-autoload", "--noconftest", "no:cacheprovider"}
    if not required.issubset(args):
        raise RuntimeError("fence, isolated fixtures, no conftest/cache and no autoload required")
    # The driver supplies a clean explicit environment; do not accept ambient
    # pytest/plugin/activation settings even from a direct invocation.
    allowed = {key: os.environ[key] for key in ("HOME", "HERMES_HOME", "TMPDIR", "PYTEST_DEBUG_TEMPROOT",
                                              "POLICY_FENCE_ATTEMPT", "POLICY_FENCE_TEST")
               if key in os.environ}
    os.environ.clear()
    os.environ.update(allowed)
    os.environ.update(PATH="/usr/bin:/bin", TZ="UTC", LANG="C.UTF-8", LC_ALL="C.UTF-8",
                      PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      HERMES_TEST_FORKSERVER="0", HERMES_TEST_FILE_RETRIES="0",
                      __HERMES_TESTS_REEXEC="1")
    os.chdir(root)
    if child:
        if Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve() != Path(os.environ["TMPDIR"]).resolve():
            raise RuntimeError("child scratch roots differ")
        # tests/__init__.py is empty and separately pinned. Install the fence
        # before pytest imports any dependency, builtin plugin or conftest.
        importlib.import_module("tests.offline_policy_fence")
        import pytest
        return pytest.main(args)
    return serial_files(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
