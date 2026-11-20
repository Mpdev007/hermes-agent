"""Harmless attempted effects must be refused before any endpoint/process/private read."""
import socket
import subprocess
from pathlib import Path
import sys
import os

import pytest


@pytest.mark.parametrize("effect", ["inet", "control", "child", "private", "write"])
def test_offline_fence_is_enforcing(effect):
    from tests.offline_policy_fence import expected_denial, _TEMP

    actions = {
        "inet": lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        "control": _contact_control_socket,
        "child": lambda: subprocess.run([sys.executable, "-c", "raise AssertionError('must not execute')"],
                                         timeout=1, check=False),
        "private": lambda: (_TEMP.parent / "outside-this-fixture").read_bytes(),
        "write": lambda: (_TEMP.parent / "outside-this-fixture").write_text("must not write"),
    }
    with expected_denial(), pytest.raises(PermissionError, match="offline-policy-fence"):
        actions[effect]()


def _contact_control_socket():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect("/never-contact-any-control-socket")


@pytest.mark.parametrize("name", ["ctypes", "psutil"])
def test_optional_native_telemetry_is_unavailable_before_import(name):
    with pytest.raises(ModuleNotFoundError, match="unavailable in controlled offline policy fixtures"):
        __import__(name)


def test_file_sync_helpers_keep_unknown_owners_and_require_telemetry_for_a_pid():
    from tools.environments import file_sync

    assert file_sync._temp_entry_owner_alive("hermes-sync-back-unattributed") is True
    with pytest.raises(ModuleNotFoundError, match="psutil unavailable in controlled offline policy fixtures"):
        file_sync._temp_entry_owner_alive(f"hermes-sync-back-{os.getpid()}-fixture")


def test_receipt_directory_cannot_be_replaced_with_a_symlink(tmp_path):
    from tests.offline_policy_fence import expected_denial, _RECEIPT
    with expected_denial(), pytest.raises(PermissionError, match="write-outside-fixture"):
        os.symlink(tmp_path, _RECEIPT.parent)


@pytest.mark.parametrize("uri", [False, True])
def test_sqlite_native_open_cannot_create_outside_fixture(uri):
    import sqlite3
    from tests.offline_policy_fence import expected_denial, _TEMP
    path = _TEMP.parent / ("sqlite-uri-outside" if uri else "sqlite-outside")
    with expected_denial(), pytest.raises(PermissionError, match="native-database-path"):
        sqlite3.connect("file:" + str(path) if uri else str(path), uri=uri)
    assert not path.exists()


def test_sqlite_fixture_database_is_allowed(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / "controlled.sqlite") as connection:
        assert connection.execute("select 1").fetchone() == (1,)


def test_unaudited_dbm_backend_is_rejected_before_import():
    from tests.offline_policy_fence import expected_denial
    with expected_denial(), pytest.raises(PermissionError, match="unsupported-native-db-backend"):
        __import__("dbm")


@pytest.mark.parametrize("operation", ["sendto", "sendmsg"])
@pytest.mark.platforms("linux")
def test_unconnected_unix_datagram_cannot_contact_any_listener(operation):
    from tests.offline_policy_fence import expected_denial, _TEMP
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        # A fresh abstract address avoids the108-byte pathname limit in long
        # review directories and has no real receiving peer.
        address = b"\0offline-policy-" + os.urandom(8).hex().encode()
        with expected_denial(), pytest.raises(PermissionError, match="network-or-control-contact"):
            if operation == "sendto":
                sock.sendto(b"fixture", address)
            else:
                sock.sendmsg([b"fixture"], [], 0, address)


@pytest.mark.platforms("linux")
def test_other_socket_family_is_rejected():
    from tests.offline_policy_fence import expected_denial
    with expected_denial(), pytest.raises(PermissionError, match="network-socket"):
        socket.socket(socket.AF_NETLINK, socket.SOCK_RAW)


@pytest.mark.parametrize("path", ["/proc/self/status", "/run/offline-fixture-not-present",
                                  "/tmp/offline-fixture-not-present", "/var/tmp/offline-fixture-not-present"])
def test_non_home_paths_are_not_implicitly_trusted(path):
    from tests.offline_policy_fence import expected_denial
    with expected_denial(), pytest.raises(PermissionError, match="read-outside-inputs"):
        Path(path).read_bytes()


def test_owned_temporary_descriptor_is_allowed_but_inherited_is_not(tmp_path):
    from tests.offline_policy_fence import expected_denial
    descriptor = os.open(tmp_path / "owned", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("owned fixture")
    assert (tmp_path / "owned").read_text() == "owned fixture"
    with expected_denial(), pytest.raises(PermissionError, match="inherited-file-descriptor"):
        open(1, "w", closefd=False)


def test_other_child_receipt_is_not_writable():
    from tests.offline_policy_fence import expected_denial, _RECEIPT
    with expected_denial(), pytest.raises(PermissionError, match="write-outside-fixture"):
        _RECEIPT.with_name("another-child.json").write_text("must not forge")


@pytest.mark.parametrize("operation", ["fifo", "node", "reverse_dns", "sqlite_extension"])
def test_missing_native_operations_are_denied(operation, tmp_path):
    from tests.offline_policy_fence import expected_denial
    with expected_denial(), pytest.raises(PermissionError, match="offline-policy-fence"):
        if operation == "fifo":
            os.mkfifo(tmp_path / "fifo")
        elif operation == "node":
            os.mknod(tmp_path / "node")
        elif operation == "reverse_dns":
            socket.getnameinfo(("127.0.0.1", 1), 0)
        else:
            import sqlite3
            with sqlite3.connect(":memory:") as database:
                database.enable_load_extension(True)
