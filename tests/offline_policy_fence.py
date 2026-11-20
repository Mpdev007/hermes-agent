"""Explicit offline-test fence, loaded with pytest -p before conftest/product imports.

Controlled Python fixtures only, not an OS sandbox for hostile native extensions.
No network endpoints (including local gateway), child processes or private-home reads.
Unexpected denied attempts fail the run even when product code catches the exception.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
import json
import hashlib
import importlib.abc
import os
from pathlib import Path
import sys
import re
import traceback

_ROOT = Path(__file__).resolve().parents[1]
_TEMP = Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve()
_PREFIXES = tuple(Path(p).resolve() for p in (sys.prefix, sys.base_prefix))
_GUARDS = (_ROOT.parent / "guard-policy-v5",)
_SYSTEM_FILES = frozenset(Path(p).resolve() for p in ("/etc/os-release", "/etc/localtime", "/dev/null"))
_SYSTEM_DIRS = (Path("/usr/share/zoneinfo"),)
_NATIVE_DB_MODULES = frozenset({"dbm", "dbm.gnu", "dbm.ndbm", "_dbm", "_gdbm"})
_EXPECTED = ContextVar("expected_offline_denial", default=False)
_DENIALS = Counter()
_UNEXPECTED = Counter()
_DETAILS = []
_OUTCOMES = Counter(passed=0, failed=0, skipped=0, errors=0)
_UNAVAILABLE_IMPORTS = Counter()
sys.dont_write_bytecode = True
_ATTEMPT = os.environ.get("POLICY_FENCE_ATTEMPT", "")
_TEST = os.environ.get("POLICY_FENCE_TEST", "")
if (not re.fullmatch(r"[a-f0-9]{32}", _ATTEMPT) or not _TEST.startswith("tests/")
        or not (_ROOT / _TEST).resolve().is_relative_to(_ROOT / "tests")):
    raise RuntimeError("exact attempt and test identity required")
_RECEIPT = _ROOT / ".pytest_cache/policy-fence" / (_ATTEMPT + "-" + hashlib.sha256(_TEST.encode()).hexdigest() + ".json")
_PENDING = _RECEIPT.with_suffix(".pending")
_OWNED_FDS = {}
_OS_OPEN, _OS_CLOSE = os.open, os.close


def _inside(path, root):
    return path == root or root in path.parents


class _UnavailableNativeTelemetry(importlib.abc.MetaPathFinder):
    """Declare unrelated optional native telemetry unavailable before importing it.

    No fake module or API is supplied. Code requiring either dependency must
    fail; only its normal ImportError fallback may proceed. Effects stay denied.
    """
    def find_spec(self, fullname, path=None, target=None):
        name = fullname.split(".", 1)[0]
        if name in {"ctypes", "psutil"}:
            _UNAVAILABLE_IMPORTS[name] += 1
            raise ModuleNotFoundError(f"{name} unavailable in controlled offline policy fixtures", name=name)


def _path(raw):
    if isinstance(raw, int):
        owner = _OWNED_FDS.get(raw)
        if owner is None:
            _deny("inherited-file-descriptor")
        current = os.fstat(raw)
        if (current.st_dev, current.st_ino) != owner[:2]:
            _deny("reused-file-descriptor")
        return owner[2]
    if raw is None:
        return Path.cwd()
    return Path(os.fsdecode(raw)).absolute()


def _deny(kind, path=None, known_probe=False):
    _DENIALS[kind] += 1
    if not _EXPECTED.get() and not known_probe:
        _UNEXPECTED[kind] += 1
        _DETAILS.append({"kind": kind, "path": str(path) if path else "",
                         "stack": [(f.filename, f.lineno, f.name) for f in traceback.extract_stack(limit=10)]})
    raise PermissionError("offline-policy-fence: " + kind)


def _check_path(raw, write=False):
    path = _path(raw)
    if path == Path("/dev/null"):
        return
    path = path.resolve()
    if write:
        if _inside(path, _TEMP) or path in {_RECEIPT, _PENDING}:
            return
        _deny("write-outside-fixture")
    if _inside(path, _TEMP):
        return
    if (path.name in {".netrc", ".pypirc", "auth.json", "credentials.json", "secrets.json", "secret.json"}
            or path.name == ".env" or path.name.startswith(".env.") or ".ssh" in path.parts):
        _deny("credential-file-read", path)
    # Default-deny outside the named inputs, not just outside $HOME. This also
    # excludes /proc, /run, unrelated temp roots and other mounted user data.
    allowed = ((_inside(path, _ROOT) and ".git" not in path.relative_to(_ROOT).parts)
               or path == _ROOT.parent / "manifest.json"
               or any(_inside(path, root) for root in (*_GUARDS, *_PREFIXES, *_SYSTEM_DIRS))
               or path in _SYSTEM_FILES)
    if not allowed:
        _deny("read-outside-inputs", path)


def _owned_open(path, flags, mode=0o777, *, dir_fd=None):
    if dir_fd is not None:
        _deny("relative-descriptor-open")
    resolved = _path(path).resolve()
    _check_path(resolved, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
    fd = _OS_OPEN(path, flags, mode)
    if _inside(resolved, _TEMP) or resolved in {_RECEIPT, _PENDING}:
        info = os.fstat(fd)
        _OWNED_FDS[fd] = (info.st_dev, info.st_ino, resolved)
    return fd


def _owned_close(fd):
    _OWNED_FDS.pop(fd, None)
    return _OS_CLOSE(fd)


def _unsupported_operation(*_args, **_kwargs):
    _deny("unsupported-native-operation")


def _check_sqlite(raw):
    if raw == ":memory:":
        return
    if not raw or os.fsdecode(raw).startswith("file:"):
        _deny("native-database-path")  # no URI, temp-name or alternate VFS interpretation
    path = _path(raw).resolve()
    if not _inside(path, _TEMP):
        _deny("native-database-path", path)


def _audit(event, args):
    if event == "socket.__new__":
        if args[1] != 1:  # only AF_UNIX fixtures; refuse INET and every other family
            caller = sys._getframe(1)
            parent = caller.f_back
            probe = (args[1] == 10 and parent is not None and parent.f_code.co_name == "_has_ipv6"
                     and parent.f_code.co_filename.endswith("/urllib3/util/connection.py"))
            # urllib3 handles denial as "IPv6 unavailable". Still refuse socket creation;
            # record this reviewed import-time probe separately, never permit a bind/contact.
            _deny("urllib3-ipv6-probe-denied" if probe else "network-socket", known_probe=probe)
    elif event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.sendto", "socket.sendmsg",
                   "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
        _deny("network-or-control-contact")
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec", "os.kill", "os.killpg"}:
        _deny("process-control")
    elif event == "sqlite3.connect":
        _check_sqlite(args[0])
    elif event == "import" and args[0] in _NATIVE_DB_MODULES:
        # CPython dbm backends do not consistently emit file-open audit events.
        # The controlled suite does not need them: reject before native import.
        _deny("unsupported-native-db-backend")
    elif event in {"os.setxattr", "os.removexattr", "os.getxattr", "os.listxattr", "ctypes.dlopen", "ctypes.dlsym",
                   "os.chown", "os.mkfifo", "os.mknod", "sqlite3.enable_load_extension", "sqlite3.load_extension"}:
        _deny("unsupported-native-operation")
    elif event == "open":
        mode, flags = args[1], args[2]
        write = bool((isinstance(mode, str) and any(c in mode for c in "wax+"))
                     or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        _check_path(args[0], write)
    elif event in {"os.listdir", "os.scandir"}:
        _check_path(args[0])
    elif event == "os.mkdir":
        path = _path(args[0])
        if path in _RECEIPT.parents[:2] and path.is_dir() and not path.is_symlink():
            return  # mkdir(exist_ok=True) on the driver's pre-created real directory
        _check_path(args[0], True)
    elif event in {"os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime"}:
        _check_path(args[0], True)
    elif event in {"os.rename", "os.link", "os.symlink"}:
        _check_path(args[0], True)
        _check_path(args[1], True)


@contextmanager
def expected_denial():
    token = _EXPECTED.set(True)
    try:
        yield
    finally:
        _EXPECTED.reset(token)


if _NATIVE_DB_MODULES.intersection(sys.modules):
    raise RuntimeError("offline fence cannot qualify a preloaded native dbm backend")
sys.addaudithook(_audit)
sys.meta_path.insert(0, _UnavailableNativeTelemetry())
os.open, os.close = _owned_open, _owned_close
os.mkfifo = os.mknod = _unsupported_operation


def pytest_runtest_logreport(report):
    if report.when == "call":
        _OUTCOMES[report.outcome] += 1
    elif report.failed:
        _OUTCOMES["errors"] += 1
    elif report.skipped:
        _OUTCOMES["skipped"] += 1


def pytest_collectreport(report):
    if report.failed:
        _OUTCOMES["errors"] += 1


def pytest_sessionfinish(session, exitstatus):
    if _UNEXPECTED:
        session.exitstatus = 1
    if Path(session.config.args[0]).as_posix() != _TEST:
        raise RuntimeError("collected test identity differs from owned receipt")
    receipt = _RECEIPT
    receipt.parent.mkdir(parents=True, exist_ok=True)
    data = {"attempt": _ATTEMPT, "test_file": _TEST, "pid": os.getpid(), "tests": [item.nodeid for item in session.items],
            "loaded_source": {name: str(Path(sys.modules[name].__file__).resolve())
                              for name in ("model_tools", "gateway.session_policy", "hermes_cli.plugins", "agent.tool_executor")
                              if name in sys.modules},
            "denials": dict(_DENIALS), "unexpected": dict(_UNEXPECTED), "outcomes": dict(_OUTCOMES),
            "optional_native_imports_unavailable": dict(_UNAVAILABLE_IMPORTS),
            "exitstatus": int(session.exitstatus), "complete": True,
            "isolation": "controlled-python-fixtures-not-os-sandbox"}
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.write_line(f"OFFLINE_FENCE denials={dict(_DENIALS)} unexpected={dict(_UNEXPECTED)}")
        for detail in _DETAILS:
            reporter.write_line("OFFLINE_FENCE unexpected_detail=" + str(detail))
    sys.stdout.flush()
    pending = receipt.with_suffix(".pending")
    with pending.open("w") as output:
        output.write(json.dumps(data, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    # Publish last: reporting or evidence failures leave no completed receipt.
    os.replace(pending, receipt)
