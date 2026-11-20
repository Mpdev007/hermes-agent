"""Native prepared-process leases outlive an uncontained bootstrap (A7.6).

One disposable install, the existing install opt-in, no new runner permissions. The topology
bootstrap uses the native preparation join and direct child creation; it does not repeat PM sync.
The prepared interpreter calls the installed main with --prepared. Nothing replaces custody,
activation, receipt persistence or finalization. See _prepared.py for the two trace gates.

The existing test_update_lock_tree.py remains the positive contained-job control. This file
deliberately proves the opposite topology with IsProcessInJob, rather than forcing a job refusal
or claiming a bootstrap job record attests its prepared child.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import psutil
import pytest

from tests.e2e.core.windows._helpers import wait_until
from tests.e2e.core.windows_update._machine import (
    REPO_ROOT,
    REQUIRES_OPT_IN,
    UPDATE_TIMEOUT,
    fail_with,
    new_machine,
)
from tests.e2e.core.windows_update._prepared import DRIVER
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [pytest.mark.platforms("windows"), pytest.mark.integration,
              pytest.mark.live_system_guard_bypass, REQUIRES_OPT_IN]


def _events(private: Path, mode: str) -> list[dict]:
    path = private / (mode + ".jsonl")
    if not path.exists():
        return []
    # A line without its terminating newline is an in-flight append, never an event yet.
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines(keepends=True) if line.endswith("\n")]


def _event(machine, private: Path, mode: str, name: str, timeout: float = 120) -> dict:
    def found():
        return next((row for row in reversed(_events(private, mode)) if row["event"] == name), None)
    try:
        return wait_until(found, timeout, f"{mode} {name}", interval=0.1)
    except AssertionError as exc:
        logs = "\n".join(p.name + ":\n" + p.read_text(encoding="utf-8", errors="replace")
                         for p in private.glob("*.log"))
        raise AssertionError(fail_with(machine, f"{exc}\n{logs}")) from None


def _signal(private: Path, name: str) -> None:
    (private / name).touch()


def _spawn(machine, python: Path, private: Path, mode: str, cell: str):
    with (private / (mode + ".log")).open("wb") as log:
        proc = subprocess.Popen(
            [str(python), "-I", "-S", "-B", "-u", "-X", "utf8", str(private / "driver.py"),
             str(machine.install_dir.resolve()), str(private), mode, cell],
            cwd=machine.install_dir, env=machine.env({"HERMES_HOME": str(machine.hermes_home)}),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
    machine._spawned.append(proc)
    return proc


def _identity(pid: int) -> psutil.Process:
    # Retain the process incarnation now, before killing any ancestor; never target a reused pid.
    proc = psutil.Process(pid)
    proc.create_time()
    return proc


def _under(proc: psutil.Process, pid: int) -> bool:
    # Windows venv python.exe may be a launcher/worker pair; use the observed worker's ancestry.
    return proc.pid == pid or any(parent.pid == pid for parent in proc.parents())


def _kill_one(proc: psutil.Process) -> None:
    proc.kill()  # this exact process only; its child must survive without its job
    proc.wait(timeout=30)


def _own_lease(row: dict, lock_path: str) -> list:
    held = row["held"]
    assert held["path"] == lock_path and held["join"] == "own" and held["fd"] is None, row
    assert held["lease"] and len(held["lease"]) == 2 and held["depth"] >= 1, row
    return held["lease"]


def _contend(machine, python, private, cell, *, acquired):
    before = len(_events(private, "contender"))
    proc = _spawn(machine, python, private, "contender", cell)
    proc.wait(timeout=30)
    rows = _events(private, "contender")
    assert len(rows) == before + 1 and rows[-1]["event"] == "contend", rows
    row = rows[-1]
    # Popen may retain a venv launcher; the emitted pid is the native worker that tried the lock.
    assert row["acquired"] is acquired, row
    assert proc.returncode == (0 if acquired else 2), row
    return row


def _snapshot(home: Path) -> dict:
    # Refusal oracle supplements the write audit: names, modes, sizes and mtimes of all shared
    # fixture files. This is metadata, not a claim to detect an unaudited native write-and-restore.
    records = {}
    for path in home.rglob("*"):
        info = path.lstat()
        records[str(path.relative_to(home))] = (info.st_mode, info.st_size, info.st_mtime_ns)
    return records


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    with FakeLLMServer() as srv:
        machine = new_machine(tmp_path_factory.mktemp("pc"), srv.base_url, label="pc")
        try:
            run = machine.install()
            assert run.returncode == 0, fail_with(machine, "install.ps1 failed", run)
            pythons = sorted(machine.hermes_home.glob("installs/*/environments/*/venv/Scripts/python.exe"))
            assert pythons, fail_with(machine, "no installed venv Python")
            assert machine.installed_head() == machine.head
            yield machine, pythons[0]
        finally:
            machine.teardown()


def _start(installed, cell):
    machine, python = installed
    private = machine.logs / cell
    private.mkdir()  # unique per case; retained by the lane's existing artifact copy
    (private / "driver.py").write_text(DRIVER, encoding="utf-8")
    owner = _spawn(machine, python, private, "owner", cell)
    try:
        owner_row = _event(machine, private, "owner", "spawn")
        assert owner_row["child_in_owner_job"] is False, owner_row
        ready = _event(machine, private, "bootstrap", "ready")
        bootstrap = _identity(ready["pid"])
        assert _under(bootstrap, owner_row["child"]) and _under(bootstrap, owner_row["pid"]), ready
        _own_lease(ready, ready["held"]["path"])
    except BaseException:
        machine.kill_owned()
        raise
    return machine, python, private, owner, bootstrap, ready


def _target(machine, private):
    target = _event(machine, private, "prepared", "target")
    assert Path(target["root"]) == machine.install_dir.resolve(), target
    assert Path(target["executable"]).is_relative_to(machine.hermes_home), target
    assert {key: Path(value).resolve() for key, value in target["modules"].items()} == {
        "completion": (machine.install_dir / "hermes_cli/update_completion.py").resolve(),
        "lock": (machine.install_dir / "hermes_cli/update_lock.py").resolve(),
    }, target
    expected = {p: hashlib.sha256((REPO_ROOT / p).read_bytes()).hexdigest() for p in target["blobs"]}
    assert len(expected) == 4 and target["blobs"] == expected, target
    return target


@pytest.mark.parametrize("cell", ["survivor", "tail-error"])
def test_prepared_survivor_keeps_its_lease_through_the_result_and_releases_at_exit(installed, cell):
    machine, python, private, owner, bootstrap, ready = _start(installed, cell)
    prepared = None
    try:
        _signal(private, "spawn-prepared")
        spawn = _event(machine, private, "bootstrap", "spawn")
        assert spawn["child_in_bootstrap_job"] is False, spawn
        target = _target(machine, private)
        prepared = _identity(target["pid"])
        assert _under(prepared, spawn["child"]), target
        activation = _event(machine, private, "prepared", "activation")
        assert activation["pid"] == prepared.pid == target["pid"]
        assert _under(prepared, bootstrap.pid)
        lease = _own_lease(activation, ready["held"]["path"])
        assert lease[1] != ready["held"]["lease"][1], (activation, ready)
        _kill_one(bootstrap)
        owner_identity = _identity(_event(machine, private, "owner", "spawn")["pid"])
        _kill_one(owner_identity)
        owner.wait(timeout=30)
        assert prepared.is_running(), "prepared process died with an uncontained bootstrap or owner"
        # Separate sibling contender, no update ancestor; only the prepared child's personal lease remains.
        _contend(machine, python, private, cell, acquired=False)
        _signal(private, "activate")
        # Normal cell runs the real completion tail; error cell exercises its exception/finally.
        result_row = _event(machine, private, "prepared", "result", UPDATE_TIMEOUT)
        assert _own_lease(result_row, ready["held"]["path"]) == lease, result_row
        answer = json.loads((private / "result.json").read_text(encoding="utf-8"))
        request = json.loads((private / "request.json").read_text(encoding="utf-8"))
        assert answer["schema"] == 1 and answer["update_id"] == request["receipt"]["update_id"]
        assert answer["receipt"]["update_id"] == answer["update_id"]
        assert answer["receipt"]["finished_at"] and answer["windows_resume"] is None
        assert answer["exit_code"] == 0 and answer["receipt"]["outcome"] == "success", answer
        assert any(s["name"] == "deps" and s["outcome"] == "success" for s in answer["receipt"]["stages"])
        if cell == "tail-error":
            assert any("fixture_prepared_tail_error" in f["reason"]
                       for f in answer["receipt"]["followups"]), answer
        _contend(machine, python, private, cell, acquired=False)
        _signal(private, "prepared-exit")
        assert prepared.wait(timeout=30) == answer["exit_code"]
        _contend(machine, python, private, cell, acquired=True)
    finally:
        machine.kill_owned()


def test_exhausted_native_leases_refuse_prepared_before_activation_or_shared_writes(installed):
    machine, python, private, owner, bootstrap, ready = _start(installed, "exhausted")
    try:
        exhaustion = _event(machine, private, "bootstrap", "exhausted")
        assert exhaustion["count"] == exhaustion["slots"] == 16, exhaustion
        before = _snapshot(machine.hermes_home)
        _signal(private, "spawn-prepared")
        spawn = _event(machine, private, "bootstrap", "spawn")
        assert spawn["child_in_bootstrap_job"] is False, spawn
        target = _target(machine, private)
        prepared = _identity(target["pid"])
        assert _under(prepared, spawn["child"]) and _under(prepared, bootstrap.pid), target
        row = _event(machine, private, "prepared", "result")
        assert row["pid"] == prepared.pid == target["pid"]
        assert row["held"]["join"] == "none" and row["held"]["lease"] is None, row
        assert bootstrap.is_running() and owner.poll() is None, "the owning ancestor must remain alive"
        answer = json.loads((private / "result.json").read_text(encoding="utf-8"))
        request = json.loads((private / "request.json").read_text(encoding="utf-8"))
        assert answer["schema"] == 1 and answer["update_id"] == request["receipt"]["update_id"]
        assert answer["exit_code"] == 1 and answer["receipt"] is None and answer["windows_resume"] is None
        assert "no lock descriptor or lease of its own" in answer["error"], answer
        _signal(private, "prepared-exit")
        assert prepared.wait(timeout=30) == 1
        events = _events(private, "prepared")
        assert any(e["event"] == "lock-open" for e in events), events
        assert not [e for e in events if e["event"] in ("activation", "forbidden-write", "forbidden-process")], events
        assert _snapshot(machine.hermes_home) == before, "shared fixture metadata changed on refusal"
        _contend(machine, python, private, "exhausted", acquired=False)
        _signal(private, "bootstrap-exit")
        bootstrap.wait(timeout=30)
        _signal(private, "owner-exit")
        owner.wait(timeout=30)
        _contend(machine, python, private, "exhausted", acquired=True)
    finally:
        machine.kill_owned()
