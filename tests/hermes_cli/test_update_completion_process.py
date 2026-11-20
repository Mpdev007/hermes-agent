"""A checkout transition must not finish in the old interpreter's module graph."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from hermes_cli import update_completion


@pytest.fixture
def transition(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    package = root / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    pm_package = root / "pm"
    pm_package.mkdir()
    (pm_package / "__init__.py").write_text("OLD_API = True\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Completion test")
    git("config", "user.email", "completion@example.invalid")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "old incompatible runtime")
    old = git("rev-parse", "HEAD")
    # Deliberately incompatible: a cached OLD_API-only PM cannot prepare this tree.
    (root / "pm/__init__.py").write_text(
        "from hermes_cli.probe import event\n"
        "def sync_venv(extras, *, explicit, project_root, evict_incompatible_plugins):\n"
        "    assert explicit and evict_incompatible_plugins\n"
        "    event('prepare')\n"
    )
    (root / "pm/receipt.py").write_text(
        "from contextlib import nullcontext\n"
        "worker_context = lambda update_id: nullcontext()\n"
        "last_for_update = lambda update_id: {'update_id': update_id, 'outcome': 'success'}\n"
        "def accept_worker_receipt(data, update_id):\n"
        "    assert data['update_id'] == update_id\n"
    )
    (root / "pm/client.py").write_text(
        "from hermes_cli.probe import event\n"
        "ensure_tools_for_sync = lambda: event('tools')\n"
    )
    # A tree that already has PM facts: nothing to carry from a pre-PM venv.
    (root / "pm/extras.py").write_text("first_generation_selection = lambda root: None\n")
    (package / "probe.py").write_text(
        "import json, os, pathlib, sys\n"
        "def event(name, **values):\n"
        "    with pathlib.Path('events.jsonl').open('a') as f:\n"
        "        f.write(json.dumps(dict(name=name, pid=os.getpid(), python=sys.executable, **values)) + '\\n')\n"
    )
    selected = tmp_path / "selected-python"
    venv.EnvBuilder(with_pip=False).create(selected)
    selected_python = selected / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    (pm_package / "environments.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "selected_venv = lambda root: Path(sys.executable).parent.parent\n"
        f"project_python = lambda root: Path({str(selected_python)!r})\n"
        "activation_environment = lambda root: {**os.environ, 'PYTHONPATH': str(root)}\n"
        "def activate_dependencies(root):\n"
        "    from hermes_cli.probe import event\n"
        "    event('activate')\n"
    )
    (root / "hermes_constants.py").write_text(
        # source_completion's update lock resolves the process home through
        # hermes_constants (never a profile override), like the Rust updater does.
        "import os\n"
        "from pathlib import Path\n"
        "def get_process_hermes_home():\n"
        "    return Path(os.environ['HERMES_HOME'])\n"
    )
    (package / "venv_sync.py").write_text(
        "from hermes_cli.probe import event\n"
        "publish_launchers = lambda root: event('launchers')\n"
        "collect_superseded_generations = lambda root: event('collect')\n"
        "refuse_foreign_owned_venv = lambda root: None\n"
        "from pathlib import Path\n"
        "import os\n"
        "def completion_pending_path(root):\n"
        "    return Path(os.environ['HERMES_HOME']) / 'completion-pending'\n"
        "def arm_completion(root, **kwargs):\n"
        "    path = Path(os.environ['HERMES_HOME']) / 'completion-pending'\n"
        "    path.write_text('source update tail not finished\\n')\n"
        "    return path\n"
        "def clear_completion(root):\n"
        "    (Path(os.environ['HERMES_HOME']) / 'completion-pending').unlink()\n"
    )
    (package / "source_build.py").write_text(
        "from hermes_cli.probe import event\n"
        "def build_update_products(root, *, desktop, after_dependencies=None):\n"
        "    after_dependencies()\n"
        "    event('build', desktop=desktop)\n"
    )
    (package / "update_launch_files.py").write_text(
        "from hermes_cli.probe import event\n"
        "def refresh_launch_files(): event('launch_files')\n"
    )
    (package / "source_stamp.py").write_text(
        "from hermes_cli.probe import event\n"
        "write_source_stamp = lambda root: event('stamp')\n"
    )
    # The shared completion tail is part of the NEW tree the child runs from, and so are the
    # modules its imports reach: update_lock (the tail claims the shared update lock) and
    # _subprocess_compat (it exposes PM's git for the builds) — neither exists in the OLD
    # tree, and a bare copy of source_completion.py would die on ModuleNotFoundError.
    shutil.copy2(Path(update_completion.__file__).with_name("maintenance_scope.py"),
                 package / "maintenance_scope.py")
    shutil.copy2(Path(update_completion.__file__).with_name("source_completion.py"),
                 package / "source_completion.py")
    shutil.copy2(Path(update_completion.__file__).with_name("update_lock.py"),
                 package / "update_lock.py")
    shutil.copy2(Path(update_completion.__file__).with_name("_startup_fast.py"), package / "_startup_fast.py")
    shutil.copy2(Path(update_completion.__file__).with_name("_subprocess_compat.py"),
                 package / "_subprocess_compat.py")
    shutil.copytree(Path(update_completion.__file__).parents[1] / "hermes_agent", root / "hermes_agent",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (package / "gitlock.py").write_text(
        "from hermes_cli.probe import event\n"
        "convert_treeless_checkout_first = lambda root: event('convert')\n"
    )
    (package / "main.py").write_text("")
    (package / "update_cmd_config.py").write_text("_LAST_SIBLING_SNAPSHOTS = {}\n")
    (package / "update_cmd_posix_pause.py").write_text("adopt_left_pending = lambda token: None\n")
    (package / "update_inventory.py").write_text(
        "from types import SimpleNamespace\nRuntimeRecord = UpdatePlan = SimpleNamespace\n"
    )
    (package / "update_cmd_maint.py").write_text(
        "from hermes_cli.probe import event\n"
        "def _run_post_update_maintenance(**kwargs):\n"
        "    from hermes_cli.update_cmd_config import _LAST_SIBLING_SNAPSHOTS\n"
        "    event('maintenance', snapshots=_LAST_SIBLING_SNAPSHOTS, **kwargs)\n"
        "    return True\n"
    )
    (package / "update_cmd.py").write_text(
        "from hermes_cli.probe import event\n"
        "_invalidate_update_cache = lambda: event('cache')\n"
        "_sweep_bytecode_after_update = lambda branch: event('bytecode')\n"
        "_write_fleet_restart_pending_marker = lambda **kw: event('pending')\n"
        "_write_gateway_update_exit_code = lambda ok: event('exit_marker', ok=ok)\n"
        "_fleet_restart_skip_reason = lambda plan: None\n"
        "def _restart_gateway_fleet_after_update(plan, gateway_mode):\n"
        "    event('restart', profiles=[r.profile for r in plan.runtimes])\n"
        "    return object()\n"
        "def _resume_windows_gateways_and_merge_outcome(out, token, gateway_mode):\n"
        "    token['resume_needed'] = False\n"
        "    event('resume')\n"
        "def _resume_windows_gateways_after_update(token):\n"
        "    if token and token.get('resume_needed'):\n"
        "        token['resume_needed'] = False\n"
        "        event('emergency_resume')\n"
        "def _verify_fleet_after_update(out, **kw):\n"
        "    from hermes_cli.update_receipt import finalize_pending_update_receipt\n"
        "    event('verify')\n"
        "    finalize_pending_update_receipt(0, 'verified')\n"
    )
    (package / "update_receipt.py").write_text(
        "import contextvars, json, os, pathlib\n"
        "_current = contextvars.ContextVar('receipt', default=None)\n"
        "class UpdateReceipt: pass\n"
        "def record_stage(*args, **kwargs): pass\n"
        "def record_build_stage(*args, **kwargs): pass\n"
        "def record_skip(*args, **kwargs): pass\n"
        "def record_fact(*args, **kwargs): pass\n"
        "def persist_running_receipt(): pass\n"
        "TAIL_FOLLOWUPS = frozenset({'dependencies', 'launchers', 'build', 'maintenance', 'profile_sync',\n"
        "                            'config_migration', 'completion'})\n"
        "finalized_receipt = lambda update_id: None\n"
        "def record_followup(step, reason, **kwargs):\n"
        "    print(f'  ⚠ Update follow-up {step!r} did not finish: {reason}')\n"
        "    r = _current.get()\n"
        "    if r is not None: r.data.setdefault('followups', []).append({'step': step, 'reason': reason})\n"
        "def amend_terminal_followup(*args): pass\n"
        "def record_user_action(*args): pass\n"
        "def _receipt_dir():\n"
        "    return pathlib.Path(os.environ['HERMES_HOME']) / 'logs/update_receipts'\n"
        "def read_run_record(update_id):\n"
        "    for path in _receipt_dir().glob(f'update_*_{update_id}.json'):\n"
        "        return path, json.loads(path.read_text(encoding='utf-8-sig'))\n"
        "def finalize_pending_update_receipt(code, reason):\n"
        "    r = _current.get()\n"
        "    if r is None: return\n"
        "    r.data.update(exit_code=code, outcome='success' if code == 0 else 'failed', finished_at='now')\n"
        "    path = pathlib.Path(os.environ['HERMES_HOME']) / 'logs/update_receipts'\n"
        "    path.mkdir(parents=True, exist_ok=True)\n"
        "    path = path / ('update_test_' + r.correlation_id + '.json')\n"
        "    path.write_text(json.dumps(r.data, ensure_ascii=False), encoding=r.data.get('encoding', 'utf-8'))\n"
        "    _current.set(None)\n"
        "    return path\n"
    )
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "new incompatible runtime")
    new = git("rev-parse", "HEAD")
    request = {
        "schema": 1, "source": str(root), "home": str(home), "branch": "main",
        "desktop": True, "assume_yes": True, "gateway_mode": True,
        "pre_update_version": "old", "snapshot_id": "active-before",
        "sibling_snapshots": {"work": "work-before"},
        "plan": {"runtimes": [{"kind": "gateway", "profile": "work"}]},
        "receipt": {"update_id": "b" * 32, "outcome": "running", "steps": []},
        "windows_resume": {"resume_needed": True, "profiles": {"work": [123]}},
    }
    return root, git, old, new, request


@pytest.mark.parametrize("bom_boundary", [None, "request", "receipt"])
def test_old_process_new_git_tree_completes_in_fresh_python(transition, tmp_path, bom_boundary):
    from hermes_cli import update_completion

    root, git, old, new, request = transition
    request["pre_update_version"] = "日本 café"
    if bom_boundary == "receipt":
        request["receipt"]["encoding"] = "utf-8-sig"
    # Copy executable code, not its text shape: the process exercises the real transport.
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "completion entrypoint")
    new = git("rev-parse", "HEAD")
    git("checkout", old)
    driver = (
        "import importlib.util, json, os, subprocess, sys\n"
        "spec = importlib.util.spec_from_file_location('transport', sys.argv[1])\n"
        "transport = importlib.util.module_from_spec(spec); spec.loader.exec_module(transport)\n"
        f"if {bom_boundary == 'request'!r}:\n"
        "    transport._write_json = lambda path, data: path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8-sig')\n"
        "import pm\nassert pm.OLD_API\n"
        "subprocess.run(['git', 'checkout', sys.argv[2]], check=True)\n"
        "result = transport.run_completion(json.loads(sys.argv[3]))\n"
        "assert pm.OLD_API, 'transport mutated old module graph'\n"
        "print('RESULT=' + json.dumps(result))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver, update_completion.__file__, new, json.dumps(request)],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root), "HERMES_HOME": request["home"]},
        capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout.split("RESULT=")[1])
    assert response["exit_code"] == 0
    assert response["receipt"]["update_id"] == request["receipt"]["update_id"]
    assert response["windows_resume"]["resume_needed"] is False
    assert not (Path(request["home"]) / "completion-pending").exists()
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    by_name = {event["name"]: event for event in events}
    assert by_name["activate"]["pid"] == by_name["build"]["pid"]
    assert by_name["prepare"]["pid"] != by_name["build"]["pid"]
    # A treeless checkout converts before the dependency work, where a pre-fix Desktop's history
    # walks filled disks (#129514).
    names = [e["name"] for e in events]
    assert by_name["convert"]["pid"] == by_name["prepare"]["pid"] and names.index("convert") < names.index("tools")
    assert Path(by_name["build"]["python"]).is_relative_to(root.parent / "selected-python")
    assert by_name["build"]["pid"] == by_name["maintenance"]["pid"] == by_name["restart"]["pid"]
    assert by_name["maintenance"]["snapshots"] == {"work": "work-before"}
    assert by_name["maintenance"]["pre_update_snapshot_id"] == "active-before"
    assert by_name["maintenance"]["pre_update_version"] == "日本 café"
    assert by_name["restart"]["profiles"] == ["work"]
    assert [e["name"] for e in events].index("exit_marker") < [e["name"] for e in events].index("restart")


def _run_cmd_update(monkeypatch, request, capsys):
    """``hermes update --gateway`` whose impl hands this request to the real post-commit completion."""
    from types import SimpleNamespace
    from hermes_cli import main, update_cmd, update_receipt

    monkeypatch.setenv("HERMES_HOME", request["home"])
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)

    def complete(args, gateway_mode):
        update_receipt.begin_update_receipt()
        request["receipt"] = update_receipt._current.get().data
        update_cmd._complete_source_update(request)

    monkeypatch.setattr(update_cmd, "_cmd_update_impl", complete)
    try:
        main.cmd_update(SimpleNamespace(gateway=True))
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, update_receipt.read_latest_receipt(), capsys.readouterr().out


@pytest.mark.parametrize("code", [0, 23, -9])
def test_missing_child_result_is_an_owed_completion_and_releases_lock(transition, monkeypatch, capsys, code):
    """Review P2 (invariant 3): the tree already moved, so a completion that died without a result
    (exit 0/23 with no answer, SIGKILL/OOM) is an owed ``completion`` follow-up, never "Update failed".

    Was exit ``code or 1``, receipt ``failed``, ``.update_exit_code`` 1 and the paused gateways left
    paused: that pinned the failure invariant 3 forbids.
    """
    from hermes_cli import update_lock

    root, _git, _old, _new, request = transition
    die = f"os._exit({code})" if code >= 0 else "os.kill(os.getpid(), 9)"
    (root / "hermes_cli/update_completion.py").write_text(f"import os\n{die}\n", encoding="utf-8")
    exit_code, receipt, out = _run_cmd_update(monkeypatch, request, capsys)
    assert exit_code == 0, out
    assert receipt["update_id"] == request["receipt"]["update_id"]
    assert receipt["outcome"] == "success"
    assert receipt["exit_code"] == 0
    assert [row["step"] for row in receipt["followups"]] == ["completion"]
    assert "Update failed" not in out
    # The Desktop hand-off scripts must not read this as a plain "Update complete." (review P4).
    assert "Desktop app build owed: the update completion did not finish" in out
    # This process resumes what the lost child never did, and the gateway watcher hears success.
    assert request["windows_resume"]["resume_needed"] is False
    assert (Path(request["home"]) / ".update_exit_code").read_text(encoding="utf-8-sig").strip() == "0"
    lock = update_lock.UpdateLock()
    assert lock.acquire()
    lock.release()


def test_completion_spawn_failure_after_commit_is_owed(transition, monkeypatch, capsys):
    """Review P2: an OSError while starting the completion (temporary directory, request file,
    Popen) after the commit point is the same owed ``completion`` follow-up and exit 0."""
    import tempfile

    _root, _git, _old, _new, request = transition

    def no_temporary_directory(*args, **kwargs):
        raise FileNotFoundError(2, "No usable temporary directory found")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", no_temporary_directory)
    exit_code, receipt, out = _run_cmd_update(monkeypatch, request, capsys)
    assert exit_code == 0, out
    assert receipt["outcome"] == "success"
    assert [row["step"] for row in receipt["followups"]] == ["completion"]
    assert "No usable temporary directory" in receipt["followups"][0]["reason"]
    assert (Path(request["home"]) / ".update_exit_code").read_text(encoding="utf-8-sig").strip() == "0"


def test_lost_completion_with_parked_local_changes_stays_partial(transition, monkeypatch, capsys):
    """The one documented exception survives the owed path: parked user changes still exit 1."""
    from hermes_cli import update_cmd

    root, _git, _old, _new, request = transition
    (root / "hermes_cli/update_completion.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
    notice = "⚠ Your local changes are parked in stash@{0}; re-apply them by hand."
    monkeypatch.setattr(update_cmd, "_unrestored_autostash_notice", lambda: notice)
    exit_code, receipt, out = _run_cmd_update(monkeypatch, request, capsys)
    assert exit_code == 1, out
    assert receipt["outcome"] == "partial"
    assert [row["step"] for row in receipt["followups"]] == ["completion"]
    assert notice in out


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("never_built", ["dependencies", "refused", "died"])
@pytest.mark.parametrize("desktop", [True, False])
def test_desktop_build_that_never_ran_is_named_for_the_handoff(transition, capsys, never_built, desktop):
    """Review P4 (F6): the hand-off scripts read ``Desktop app build owed:`` to tell the user the app is
    stale. The builder prints it only when the build fails; a run whose build never ran (dependency
    sync failed, the tail refused by the update lock, the prepared child died) must print it too."""
    scripts = Path(update_completion.__file__).parents[1] / "scripts/desktop-update"
    for script in ("posix.sh", "windows.ps1"):  # the exact prefix the hand-off scripts match
        assert "Desktop app build owed: " in (scripts / script).read_text(encoding="utf-8-sig")
    assert update_completion.DESKTOP_BUILD_OWED == "Desktop app build owed:"
    root, _git, _old, _new, request = transition
    request["desktop"] = desktop
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    if never_built == "dependencies":
        (root / "pm/__init__.py").write_text("def sync_venv(*a, **kw): raise RuntimeError('dependency refused')\n",
                                            encoding="utf-8")
    elif never_built == "refused":
        (root / "hermes_cli/source_completion.py").write_text(
            "def complete_source_checkout(*args, **kwargs):\n"
            "    raise RuntimeError('an update is still running (pid 1); wait for it to exit')\n", encoding="utf-8")
    else:
        (root / "hermes_cli/source_completion.py").write_text(
            "import os\n"
            "def complete_source_checkout(*args, **kwargs): os.kill(os.getpid(), 9)  # posix-only test\n",
            encoding="utf-8")
    result = update_completion.run_completion(request)
    out = capsys.readouterr().out
    assert result["exit_code"] == 0, out
    owed = [line.strip() for line in out.splitlines() if line.strip().startswith("Desktop app build owed: ")]
    assert len(owed) == (1 if desktop else 0), out


def test_unwritable_gateway_status_never_fails_a_settled_commit(tmp_path, monkeypatch):
    """Review P1: the bootstrap's gateway status write is best effort, like
    ``_write_gateway_update_exit_code``; the committed run still answers its result with exit 0."""
    from hermes_cli import venv_sync

    monkeypatch.setattr(venv_sync, "arm_completion", lambda root, **kwargs: None)
    # A7.4: this settle holds the update's checkout custody (not under test here; see the custody selectors below).
    monkeypatch.setattr(update_completion, "_checkout_custody", lambda request: None)
    receipt = {"update_id": "u1", "outcome": "success", "finished_at": "now"}
    monkeypatch.setattr(update_completion, "_read_terminal_receipt", lambda request: receipt)
    home = tmp_path / "home-is-a-file"
    home.write_text("x", encoding="utf-8")
    result = tmp_path / "result.json"
    request = {"source": str(tmp_path), "receipt": {"update_id": "u1"}, "gateway_mode": True,
               "home": str(home), "pm_receipt": None}
    assert update_completion._settle_after_commit(request, result, "dependencies", "pip failed") == 0
    answer = json.loads(result.read_text(encoding="utf-8-sig"))
    assert answer["exit_code"] == 0 and answer["receipt"] == receipt


# ── A7.4 (Hermes 8c2c45fd finding 4): no shared write from a bootstrap without the update's checkout custody ──

def _custody_case(tmp_path, monkeypatch):
    """A committed tree whose bootstrap is about to settle, with every shared write it could make tripwired.
    ``update_lock._HELD`` starts empty (a fresh bootstrap holds nothing until it joins) and the contention
    wait is shortened; both are restored afterwards."""
    from hermes_cli import update_lock, update_receipt, venv_sync

    source, home = tmp_path / "checkout", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    monkeypatch.setattr(update_lock, "_HELD", None)
    monkeypatch.setattr(update_lock, "CHECKOUT_CONTENTION_WAIT_SECONDS", 0.2)
    writes: list[str] = []

    def tripwire(name):
        def fire(*args, **kwargs):
            writes.append(name)
        return fire
    monkeypatch.setattr(venv_sync, "arm_completion", tripwire("tail marker"))
    for name in ("resume_run_record", "record_stage", "record_followup", "finalize_pending_update_receipt"):
        monkeypatch.setattr(update_receipt, name, tripwire(f"receipt {name}"))
    monkeypatch.setattr(update_completion, "_resume_receipt", tripwire("receipt resume"))
    request = {"schema": 1, "source": str(source), "home": str(home), "gateway_mode": True, "desktop": False,
               "receipt": {"update_id": "c" * 32, "outcome": "running", "steps": []}, "pm_receipt": None}
    return source, home, request, writes


def _close_held_lock():
    from hermes_cli import update_lock
    held = update_lock._HELD
    if held is not None and held.get("fd") is not None:
        os.close(held["fd"])


@pytest.mark.platforms("posix")
def test_a_bootstrap_denied_the_checkout_writes_nothing_shared_and_answers_its_parent(tmp_path, monkeypatch, capsys):
    """The Windows bootstrap route through its outer handler: ``_prepare`` raises the native lease-join refusal
    (``_prepare_scoped``), and another owner really holds the checkout lock, so the settle's own native join
    (``_checkout_custody``) is refused too. Nothing shared is written: no tail marker, no receipt write, no gateway
    status, and the competing owner's lock bytes and hold are untouched (red on A7.3: ``_settle_after_commit``
    armed the marker at once). The parent gets a correlated non-zero answer that names the owed step."""
    import fcntl

    from hermes_cli import update_lock

    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    lock_path = update_lock.checkout_lock_path(source)
    lock_path.write_bytes(b'{"pid": 4242, "competing": "owner"}\n')
    competitor = os.open(lock_path, os.O_RDWR)  # not inheritable (PEP 446): never mistaken for a passed fd
    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = lock_path.read_bytes()

    def lease_refused(*args):
        raise RuntimeError("could not join the update's checkout lock (held by process 4242)")
    monkeypatch.setattr(update_completion, "_prepare", lease_refused)
    result_path = tmp_path / "result.json"
    try:
        code = update_completion._bootstrap(request, tmp_path / "request.json", result_path)
        assert code == 1
        assert writes == [] and not (home / ".update_exit_code").exists()
        assert lock_path.read_bytes() == before and update_lock._HELD is None
        probe = os.open(lock_path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):  # the competitor still holds it
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        os.close(competitor)
    answer = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert answer["update_id"] == request["receipt"]["update_id"]
    assert answer["exit_code"] == 1 and answer["receipt"] is None
    assert "could not hold the update's checkout" in answer["error"] and "dependencies owed" in answer["error"]
    assert "could not hold the update's checkout" in capsys.readouterr().out


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("custody", ["held", "reacquired"])
def test_a_bootstrap_with_the_checkout_still_settles_natively(tmp_path, monkeypatch, custody):
    """Positive control: a bootstrap that holds the checkout (a joined lease or the passed descriptor) or
    re-acquires it once it is free still makes the native post-commit settle: it arms the tail and answers
    with its terminal receipt."""
    from hermes_cli import update_lock

    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    if custody == "held":  # A7.5: a real descriptor this bootstrap locked, recorded as the native join records it
        import fcntl

        lock_path = update_lock.checkout_lock_path(source)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(update_lock, "_HELD", {"path": str(lock_path), "fd": fd, "owned": True, "depth": 1})
    receipt = {"update_id": request["receipt"]["update_id"], "outcome": "success", "finished_at": "now"}
    monkeypatch.setattr(update_completion, "_read_terminal_receipt", lambda request: receipt)
    result_path = tmp_path / "result.json"
    try:
        assert update_completion._settle_after_commit(request, result_path, "dependencies", "pip failed") == 0
        assert update_completion._checkout_join(source) == "own"
    finally:
        _close_held_lock()
    assert writes == ["tail marker"]
    assert json.loads(result_path.read_text(encoding="utf-8-sig"))["receipt"] == receipt
    assert (home / ".update_exit_code").read_text(encoding="utf-8").strip() == "0"


def test_the_update_that_holds_the_checkout_settles_a_bootstrap_without_it(transition, monkeypatch, capsys):
    """The parent's half: the real ``_report_without_custody`` answer (loaded by path in the child, exactly the
    bytes a refused bootstrap writes) reaches the update process, which holds the checkout for the whole command.
    It records the owed tail as a lost completion -- its own arm, one ``completion`` follow-up naming the
    custody reason, exit 0 -- and releases the lock afterwards."""
    from hermes_cli import update_lock, venv_sync

    root, _git, _old, _new, request = transition
    armed: list[Path] = []
    monkeypatch.setattr(venv_sync, "arm_completion", lambda project_root, **kwargs: armed.append(Path(project_root)))
    real = Path(update_completion.__file__).resolve()
    (root / "hermes_cli/update_completion.py").write_text(
        "import importlib.util, json, pathlib, sys\n"
        f"spec = importlib.util.spec_from_file_location('refused_bootstrap', {str(real)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig'))\n"
        "raise SystemExit(module._report_without_custody(request, pathlib.Path(sys.argv[2]), 'dependencies',\n"
        "                                                'lease refused', 'held by process 4242'))\n",
        encoding="utf-8")
    exit_code, receipt, out = _run_cmd_update(monkeypatch, request, capsys)
    assert exit_code == 0, out
    assert receipt["outcome"] == "success"
    assert [row["step"] for row in receipt["followups"]] == ["completion"]
    assert "could not hold the update's checkout" in receipt["followups"][0]["reason"]
    assert armed == [root]
    lock = update_lock.UpdateLock()
    assert lock.acquire()
    lock.release()


# ── A7.5 (Hermes cfadb94a, Windows boundary): a join through the ancestor alone is custody only inside the job ──
# The native join record (``update_lock._HELD``) is data: these selectors set the record a Windows join leaves
# (``_acquire_checkout``: a lease byte of its own, or ``fd=None, lease=None`` when every byte was taken) and never
# make the interpreter believe it runs on Windows. The live lease sequence itself is native-Windows only.

def _join_state(monkeypatch, source: Path, kind: str) -> int | None:
    """Record a native join of ``source``'s checkout lock of the given kind (restored afterwards): ``owned`` /
    ``inherited`` (a descriptor), ``lease`` (a Windows lease byte of its own), ``ancestor`` (the ancestor alone) or
    ``none``. Returns the stand-in descriptor it opened, which the caller closes."""
    from hermes_cli import update_lock

    if kind == "none":
        monkeypatch.setattr(update_lock, "_HELD", None)
        return None
    fd = None if kind == "ancestor" else os.open(os.devnull, os.O_RDONLY)
    held = {"path": str(update_lock.checkout_lock_path(source)), "fd": None, "owned": False, "depth": 1}
    if kind in ("owned", "inherited"):
        held.update(fd=fd, owned=kind == "owned")
    else:
        held["lease"] = None if fd is None else (fd, update_lock._WINDOWS_LOCK_OFFSET + 1)
    monkeypatch.setattr(update_lock, "_HELD", held)
    return fd


def _close(*fds) -> None:
    for fd in fds:
        if fd is not None:
            os.close(fd)


@pytest.mark.parametrize("kind,update_job,custody", [
    ("owned", None, True), ("inherited", None, True), ("lease", None, True), ("ancestor", "bound", True),
    ("ancestor", None, False), ("ancestor", "refused", False), ("ancestor", True, False), ("none", "bound", False),
])
def test_a_checkout_join_is_custody_only_with_its_own_descriptor_or_lease_or_inside_the_job(
        tmp_path, monkeypatch, kind, update_job, custody):
    """Native ownership decides: a descriptor or lease of its own always fences this process's writes; a join
    through the ancestor alone only with the parent's exact positive record that its job took this child."""
    source, _home, request, _writes = _custody_case(tmp_path, monkeypatch)
    if update_job is not None:
        request["update_job"] = update_job
    fd = _join_state(monkeypatch, source, kind)
    try:
        assert (update_completion._join_without_custody(request) is None) == custody
    finally:
        _close(fd)


def test_an_uncontained_ancestor_only_join_settles_nothing_shared_and_answers_its_parent(tmp_path, monkeypatch,
                                                                                        capsys):
    """Post-commit settlement: a bootstrap whose join rests on its live ancestor alone, outside the job, writes no
    tail marker, receipt or gateway status (red on A7.4: ``holds_checkout_lock`` accepted the join and the settle
    armed the tail). Its correlated private answer names the reason; the parent settles it as before."""
    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    _join_state(monkeypatch, source, "ancestor")
    result_path = tmp_path / "result.json"
    code = update_completion._settle_after_commit(request, result_path, "completion",
                                                  "the completion process exited 1 without a result")
    assert code == 1 and writes == [] and not (home / ".update_exit_code").exists()
    answer = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert answer["update_id"] == request["receipt"]["update_id"]
    assert answer["exit_code"] == 1 and answer["receipt"] is None
    assert "only through its live update ancestor" in answer["error"] and "completion owed" in answer["error"]
    assert "could not hold the update's checkout" in capsys.readouterr().out


@pytest.mark.parametrize("kind,update_job", [("lease", None), ("ancestor", "bound")])
def test_a_bootstrap_with_its_own_lease_or_inside_the_job_still_settles_natively(tmp_path, monkeypatch, kind,
                                                                                update_job):
    """Positive controls: a valid lease of its own, or a join through the ancestor inside the job, still makes the
    native post-commit settle: it arms the tail and answers with its terminal receipt."""
    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    if update_job is not None:
        request["update_job"] = update_job
    fd = _join_state(monkeypatch, source, kind)
    receipt = {"update_id": request["receipt"]["update_id"], "outcome": "success", "finished_at": "now"}
    monkeypatch.setattr(update_completion, "_read_terminal_receipt", lambda request: receipt)
    result_path = tmp_path / "result.json"
    try:
        assert update_completion._settle_after_commit(request, result_path, "dependencies", "pip failed") == 0
    finally:
        _close(fd)
    assert writes == ["tail marker"]
    assert json.loads(result_path.read_text(encoding="utf-8-sig"))["receipt"] == receipt
    assert (home / ".update_exit_code").read_text(encoding="utf-8").strip() == "0"


@pytest.mark.parametrize("outcome", ["refused", "ancestor", "ancestor-in-job", "lease"])
def test_the_preparation_join_needs_custody_of_its_own(tmp_path, monkeypatch, outcome):
    """Initial preparation (the Windows join in ``_prepare_scoped``, driven through its helper): an explicit refusal
    and a join through the ancestor alone outside the job raise before anything writes the checkout, and the latter
    join is dropped so the settle joins afresh; a lease of its own, or the ancestor inside the job, proceeds."""
    from hermes_cli import update_lock

    source, _home, request, _writes = _custody_case(tmp_path, monkeypatch)
    if outcome == "ancestor-in-job":
        request["update_job"] = "bound"
    fds: list[int | None] = []

    def native_join(root):
        if outcome == "refused":
            return update_lock.UpdateHolder(pid=4242, age_seconds=0.0)
        fds.append(_join_state(monkeypatch, source, "lease" if outcome == "lease" else "ancestor"))
        return None
    monkeypatch.setattr(update_lock, "_acquire_checkout", native_join)
    try:
        if outcome in ("refused", "ancestor"):
            with pytest.raises(RuntimeError, match="held by process 4242" if outcome == "refused"
                               else "custody of its own"):
                update_completion._join_for_preparation(request)
            assert update_lock._HELD is None
        else:
            update_completion._join_for_preparation(request)
            assert update_completion._checkout_join(source) == ("own" if outcome == "lease" else "ancestor")
    finally:
        _close(*fds)


def test_an_uncontained_ancestor_only_bootstrap_prepares_nothing_and_answers_its_parent(tmp_path, monkeypatch):
    """The whole bootstrap route: preparation refuses the join through the ancestor alone before any write, the
    outer handler's settle joins afresh, meets the same join and only reports to the parent."""
    from hermes_cli import update_lock

    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    joins: list[str] = []

    def native_join(root):
        joins.append("ancestor")
        _join_state(monkeypatch, source, "ancestor")
        return None
    monkeypatch.setattr(update_lock, "_acquire_checkout", native_join)

    def prepare(request, request_path, result_path):
        update_completion._join_for_preparation(request)  # the Windows branch of _prepare_scoped
        writes.append("dependency preparation")
        return 0
    monkeypatch.setattr(update_completion, "_prepare", prepare)
    result_path = tmp_path / "result.json"
    assert update_completion._bootstrap(request, tmp_path / "request.json", result_path) == 1
    assert joins == ["ancestor", "ancestor"] and writes == [] and not (home / ".update_exit_code").exists()
    answer = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert answer["exit_code"] == 1 and answer["receipt"] is None
    assert "dependencies owed" in answer["error"] and "only through its live update ancestor" in answer["error"]


@pytest.mark.parametrize("binder,bound,recorded", [
    ("owned", True, True), ("lease", True, True), ("ancestor", True, False), ("owned", False, False),
])
def test_the_parent_records_the_job_binding_only_after_binding_with_custody_of_its_own(
        tmp_path, monkeypatch, binder, bound, recorded):
    """The parent's half (``_bind_and_resume``): the positive record reaches the child's request before the child is
    resumed, only when the job took the child and the parent's own join fences anything; a refused bind or a
    binder joined through its own ancestor records none. The parent's copy of the request is not changed."""
    from hermes_cli import update_lock

    source, _home, request, _writes = _custody_case(tmp_path, monkeypatch)
    request_path = tmp_path / "request.json"
    update_completion._write_json(request_path, request)
    fd = _join_state(monkeypatch, source, binder)
    resumed: list[dict] = []
    monkeypatch.setattr(update_lock, "bind_child_to_update_tree",
                        lambda proc: None if bound else OSError(5, "Access is denied"))
    monkeypatch.setattr(update_lock, "resume_suspended_child",
                        lambda proc: resumed.append(json.loads(request_path.read_text(encoding="utf-8-sig"))))
    try:
        update_completion._bind_and_resume(object(), request, request_path)
    finally:
        _close(fd)
    assert len(resumed) == 1 and (resumed[0].get("update_job") == "bound") == recorded
    assert "update_job" not in request


# ── A7.6 (Hermes af6219a7, prepared-child limit): the --prepared process needs custody of its own ──

def _prepared_case(tmp_path, monkeypatch):
    """``_custody_case`` plus tripwires on what the --prepared process does after its custody check: dependency
    activation, the running-receipt persistence and the tail (``_finish``)."""
    import pm.environments
    from hermes_cli import update_receipt

    source, home, request, writes = _custody_case(tmp_path, monkeypatch)
    monkeypatch.setattr(pm.environments, "activate_dependencies", lambda root: writes.append("activation"))
    monkeypatch.setattr(update_receipt, "persist_running_receipt", lambda: writes.append("receipt persist"))
    monkeypatch.setattr(update_completion, "_finish", lambda request, result_path: writes.append("tail") or 0)
    return source, home, request, writes


@pytest.mark.parametrize("outcome", ["ancestor", "ancestor-with-bootstrap-record", "refused", "unopenable"])
def test_a_prepared_child_without_custody_of_its_own_activates_nothing_and_answers_its_parent(
        tmp_path, monkeypatch, outcome):
    """No dependency activation, no stage or receipt write, no tail: only the correlated private answer, which the
    update process settles as a lost completion. The bootstrap's ``update_job`` record in the shared request never
    covers this process (red on A7.5: the prepared entry activated and ran ``_finish`` with no join at all)."""
    from hermes_cli import update_lock

    source, home, request, writes = _prepared_case(tmp_path, monkeypatch)
    if outcome == "ancestor-with-bootstrap-record":
        request["update_job"] = "bound"

    def native_join(root):
        if outcome == "refused":
            return update_lock.UpdateHolder(pid=4242, age_seconds=0.0)
        if outcome == "unopenable":
            raise PermissionError(13, "Permission denied")
        _join_state(monkeypatch, source, "ancestor")
        return None
    monkeypatch.setattr(update_lock, "_acquire_checkout", native_join)
    result_path = tmp_path / "result.json"
    assert update_completion._prepared(request, result_path, source) == 1
    assert writes == [] and not (home / ".update_exit_code").exists()
    assert update_lock._HELD is None  # a join through the ancestor alone is released, not kept
    answer = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert answer["update_id"] == request["receipt"]["update_id"]
    assert answer["exit_code"] == 1 and answer["receipt"] is None and answer["windows_resume"] is None
    assert "completion owed" in answer["error"] and "could not hold the update's checkout" in answer["error"]


@pytest.mark.platforms("posix")
def test_a_prepared_child_refused_by_a_competing_owner_activates_nothing(tmp_path, monkeypatch):
    """Explicit refusal through the real native join: no descriptor was passed and another open file description
    holds the checkout lock, so the prepared process reports privately and the competitor's hold is untouched."""
    import fcntl

    from hermes_cli import update_lock

    source, home, request, writes = _prepared_case(tmp_path, monkeypatch)
    lock_path = update_lock.checkout_lock_path(source)
    lock_path.write_bytes(b'{"pid": 4242, "competing": "owner"}\n')
    competitor = os.open(lock_path, os.O_RDWR)  # not inheritable (PEP 446): never mistaken for a passed fd
    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result_path = tmp_path / "result.json"
    try:
        assert update_completion._prepared(request, result_path, source) == 1
        assert writes == [] and update_lock._HELD is None
    finally:
        os.close(competitor)
    answer = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert answer["exit_code"] == 1 and answer["receipt"] is None


@pytest.mark.parametrize("kind", ["lease", "owned"])
@pytest.mark.parametrize("tail", ["completes", "raises"])
def test_a_prepared_child_with_custody_of_its_own_holds_it_through_finalization(tmp_path, monkeypatch, kind, tail):
    """Positive controls: a lease byte or descriptor of its own is joined before dependency activation and is still
    held when the tail finalizes, and after it, also when the tail raises (the kernel drops it at exit)."""
    import pm.environments
    from hermes_cli import update_lock

    source, _home, request, _writes = _prepared_case(tmp_path, monkeypatch)
    events: list = []
    descriptors: list[int | None] = []

    def native_join(root):
        events.append("join")
        descriptors.append(_join_state(monkeypatch, source, kind))
        return None

    def finish(request, result_path):
        events.append(("tail", update_completion._checkout_join(source)))
        if tail == "raises":
            raise RuntimeError("the tail failed")
        return 0
    monkeypatch.setattr(update_lock, "_acquire_checkout", native_join)
    monkeypatch.setattr(pm.environments, "activate_dependencies",
                        lambda root: events.append(("activation", update_completion._checkout_join(source))))
    monkeypatch.setattr(update_completion, "_finish", finish)
    try:
        if tail == "raises":
            with pytest.raises(RuntimeError, match="the tail failed"):
                update_completion._prepared(request, tmp_path / "result.json", source)
        else:
            assert update_completion._prepared(request, tmp_path / "result.json", source) == 0
        assert events == ["join", ("activation", "own"), ("tail", "own")]
        assert update_completion._checkout_join(source) == "own"
    finally:
        _close(*descriptors)


@pytest.mark.platforms("posix")
def test_a_prepared_child_joins_the_descriptor_its_bootstrap_passed(tmp_path, monkeypatch):
    """Live POSIX control through the real native join: the locked descriptor the bootstrap passes
    (``checkout_lock_fds``, inheritable after ``pass_fds``) is this process's own custody, and the tail's own
    native acquisition (``complete_source_checkout``: ``UpdateLock(install_root=source)``, the request's spelling)
    nests in it and gives it back, so the join is still held, at depth 1, for finalization."""
    import fcntl

    from hermes_cli import update_lock

    source, _home, request, _writes = _prepared_case(tmp_path, monkeypatch)
    lock_path = update_lock.checkout_lock_path(source)
    passed = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(passed, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(passed, True)
        assert update_completion._prepared_custody(request) is None
        assert update_lock._HELD["fd"] == passed and update_lock._HELD["owned"] is False
        assert update_completion._checkout_join(source) == "own"
        tail = update_lock.UpdateLock(install_root=Path(request["source"]))
        assert tail.acquire_checkout(tail.install_root) and update_lock._HELD["depth"] == 2
        tail.release()
        assert update_lock._HELD["depth"] == 1 and update_lock._HELD["fd"] == passed
        assert update_completion._checkout_join(source) == "own"
    finally:
        os.close(passed)


@pytest.mark.parametrize("cleanup_failure", [None, "kill", "wait"])
def test_interrupt_after_child_success_demotes_gateway_marker_at_boundary(transition, monkeypatch, cleanup_failure):
    import io
    from types import SimpleNamespace
    from hermes_cli import main, update_cmd, update_lock, update_receipt

    root, _git, _old, _new, request = transition
    marker = Path(request["home"]) / ".update_exit_code"
    (root / "hermes_cli/update_completion.py").write_text(
        "import os, pathlib, time\n"
        "(pathlib.Path(os.environ['HERMES_HOME']) / '.update_exit_code').write_text('0\\n')\n"
        "print('SUCCESS_PUBLISHED', flush=True)\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setenv("HERMES_HOME", request["home"])
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
    interrupted = False
    cleanup_error = (OSError("retained-handle kill failed") if cleanup_failure == "kill"
                     else subprocess.TimeoutExpired("completion", 5))
    waits = []
    children = []
    popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        proc = popen(*args, **kwargs)
        # Hook the completion child itself: arming the host obligation may probe the checkout
        # identity through git first (#125952), and that probe is not the process under test.
        if not children and str(root / "hermes_cli/update_completion.py") in args[0]:
            children.append(proc)
            wait = proc.wait
            kill = proc.kill

            def cleanup_kill():
                kill()
                if cleanup_failure == "kill":
                    raise cleanup_error

            def cleanup_wait(timeout=None):
                waits.append(timeout)
                # Inject faults after real cleanup so a regression cannot leak the child.
                result = wait(timeout=5)
                if cleanup_failure == "wait":
                    raise cleanup_error
                return result

            monkeypatch.setattr(proc, "kill", cleanup_kill)
            monkeypatch.setattr(proc, "wait", cleanup_wait)
        return proc

    class Interrupt(io.StringIO):
        def write(self, value):
            nonlocal interrupted
            result = super().write(value)
            if not interrupted and "SUCCESS_PUBLISHED" in self.getvalue():
                assert marker.read_text().strip() == "0"
                interrupted = True
                raise KeyboardInterrupt()
            return result

    def complete(args, gateway_mode):
        update_receipt.begin_update_receipt()
        request["receipt"] = update_receipt._current.get().data
        monkeypatch.setattr(subprocess, "Popen", capture_child)
        update_cmd._complete_source_update(request)

    stdout = Interrupt()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", complete)
    # Was KeyboardInterrupt + a ``failed`` receipt: Ctrl-C after the commit point is an interrupt
    # (exit 130), and the receipt keeps the truth the child already wrote (C3, item 5).
    with pytest.raises(SystemExit) as error:
        main.cmd_update(SimpleNamespace(gateway=True))
    assert error.value.code == 130
    assert interrupted, "child never published success before cancellation"
    assert children[0].poll() is not None
    assert children[0].stdout.closed
    assert waits and all(timeout is not None and timeout > 0 for timeout in waits)
    assert isinstance(error.value.__cause__, KeyboardInterrupt)
    if cleanup_failure:
        assert error.value.__cause__.__cause__ is cleanup_error
    assert "Interrupted after the code was updated" in stdout.getvalue()
    receipt = update_receipt.read_latest_receipt()
    assert receipt["update_id"] == request["receipt"]["update_id"]
    assert receipt["outcome"] == "interrupted"  # the child was stopped before it closed the run
    assert receipt["exit_code"] == 130
    assert receipt["stop_reason"].startswith("KeyboardInterrupt:")
    # The gateway watcher still hears the interrupt (a non-zero exit is fine for Ctrl-C).
    assert marker.read_text().strip() == "1"
    lock = update_lock.UpdateLock()
    assert lock.acquire()
    lock.release()


@pytest.mark.platforms("posix")
def test_killed_selected_python_returns_signal_exit_status(transition):
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    (root / "hermes_cli/source_build.py").write_text(
        "import os, signal\n"
        "def build_update_products(*a, **kw): os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    result = update_completion.run_completion(request)
    # Was 137: the tree already moved, so a prepared child that dies without a result is an owed
    # ``completion`` follow-up (C3/A6), the tail obligation armed for the next launch.
    assert result["exit_code"] == 0
    assert result["receipt"]["outcome"] == "success"
    assert [f["step"] for f in result["receipt"]["followups"]] == ["completion"]
    assert "exited 137" in result["receipt"]["followups"][0]["reason"]
    assert result["pm_receipt"]["update_id"] == request["receipt"]["update_id"]


def test_failed_build_after_commit_is_a_followup_and_later_steps_still_run(transition):
    """Contract C3: the code is committed, so a failed product build cannot fail the update."""
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    (root / "hermes_cli/source_build.py").write_text(
        "import subprocess\n"
        "def build_update_products(*a, **kw): raise subprocess.CalledProcessError(23, ['builder'])\n"
    )
    result = update_completion.run_completion(request)
    # Was 23 / "failed": a post-commit build failure exits 0 with a success receipt.
    assert result["exit_code"] == 0
    assert result["receipt"]["outcome"] == "success"
    # The failure is not silent: it is a named receipt follow-up.
    assert [f["step"] for f in result["receipt"]["followups"]] == ["build"]
    # The tail obligation stays armed so the next launch / `hermes update` rebuilds.
    assert (Path(request["home"]) / "completion-pending").read_text() == "source update tail not finished\n"
    events = [json.loads(line)["name"] for line in (root / "events.jsonl").read_text().splitlines()]
    # Was "not in": the failed build no longer skips maintenance (config migration) ...
    assert "maintenance" in events
    # ... nor the gateway restart and its verification.
    assert "restart" in events and "verify" in events
    # The tail was not complete, so the install stamp is not written.
    assert "stamp" not in events
    # The gateway watcher hears success: the code it runs is the committed code.
    assert {"name": "exit_marker", "ok": True}.items() <= next(
        json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()
        if json.loads(line)["name"] == "exit_marker").items()


def test_prepare_failure_preserves_correlated_pm_receipt(transition):
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    (root / "pm/__init__.py").write_text(
        "def sync_venv(*a, **kw): raise RuntimeError('dependency refused')\n"
    )
    with (root / "pm/receipt.py").open("a") as stream:
        stream.write("last_for_update = lambda update_id: {'update_id': update_id, 'outcome': 'refused', 'refusal': {'reason': 'dependency refused'}}\n")
    result = update_completion.run_completion(request)
    # Was exit 1 / no receipt: the dependency sync runs after the tree moved, so its failure is a
    # ``dependencies`` follow-up (A6) with the tail obligation armed — and the correlated pm
    # refusal still travels back for the receipt.
    assert result["exit_code"] == 0
    assert result["receipt"]["outcome"] == "success"
    assert [f["step"] for f in result["receipt"]["followups"]] == ["dependencies"]
    assert "dependency refused" in result["receipt"]["followups"][0]["reason"]
    assert (Path(request["home"]) / "completion-pending").read_text() == "source update tail not finished\n"
    assert result["pm_receipt"] == {"update_id": request["receipt"]["update_id"], "outcome": "refused",
                                    "refusal": {"reason": "dependency refused"}}


def test_bootstrap_does_not_initialize_old_site_packages(transition, tmp_path, monkeypatch):
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    obsolete = tmp_path / "obsolete-python"
    venv.EnvBuilder(with_pip=False).create(obsolete)
    site = obsolete / ("Lib/site-packages" if os.name == "nt" else
                       f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    trap = tmp_path / "old-site-loaded"
    (site / "application.pth").write_text(f"import pathlib; pathlib.Path({str(trap)!r}).touch()\n")
    monkeypatch.setattr(sys, "executable", str(obsolete / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    result = update_completion.run_completion(request)
    assert result["exit_code"] == 0
    assert not trap.exists(), "preparation initialized the old application's .pth graph"


@pytest.mark.parametrize("stage", ["prepare", "selected"])
def test_progress_is_forwarded_before_held_stage_is_released(transition, monkeypatch, stage):
    import io
    import threading
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    release = root / "release"
    released = root / "released"
    held_body = (
        "    print('STAGE_READY')\n"  # Deliberately no flush: -I ignores PYTHONUNBUFFERED.
        "    deadline = time.monotonic() + 20\n"
        "    while not Path('release').exists():\n"
        "        if time.monotonic() >= deadline: raise TimeoutError('stage was not released')\n"
        "        time.sleep(0.02)\n"
        "    Path('released').touch()\n"
    )
    module, definition = (
        ("pm/__init__.py", "def sync_venv(*a, **kw):\n") if stage == "prepare" else
        ("hermes_cli/source_build.py", "def build_update_products(*a, **kw):\n")
    )
    (root / module).write_text("import time\nfrom pathlib import Path\n" + definition + held_body)
    observed = threading.Event()

    class Output(io.StringIO):
        def write(self, value):
            result = super().write(value)
            if "STAGE_READY" in self.getvalue():
                observed.set()
            return result

    output = Output()
    monkeypatch.setattr(sys, "stdout", output)
    responses, errors = [], []

    def run():
        try:
            responses.append(update_completion.run_completion(request))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert observed.wait(10), f"{stage} progress remained buffered: {output.getvalue()}"
        assert worker.is_alive(), "completion finished instead of waiting for release"
        assert not released.exists(), "stage passed its gate before progress was observed"
    finally:
        release.touch()
        worker.join(timeout=30)
    assert not worker.is_alive(), "completion did not stop after release"
    assert not errors
    assert released.exists()
    assert responses[0]["exit_code"] == 0


@pytest.mark.platforms("posix")
def test_interactive_configuration_keeps_terminal_input(transition):
    import pty
    import select
    import signal
    import time
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    shutil.copy2(update_completion.__file__, root / "hermes_cli/update_completion.py")
    (root / "hermes_cli/update_cmd_maint.py").write_text(
        "import sys\nfrom hermes_cli.probe import event\n"
        "def _run_post_update_maintenance(**kw):\n"
        "    assert sys.stdin.isatty() and sys.stdout.isatty()\n"
        "    event('answer', value=input('CONFIG? '))\n"
        "    return True\n"
    )
    master, slave = pty.openpty()
    driver = "import json,runpy,sys; m=runpy.run_path(sys.argv[1]); raise SystemExit(m['run_completion'](json.loads(sys.argv[2]))['exit_code'])"
    proc = subprocess.Popen([sys.executable, "-c", driver, update_completion.__file__, json.dumps(request)],
                            cwd=root, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 20
        while b"CONFIG?" not in output:
            assert time.monotonic() < deadline, output.decode(errors="replace")
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 8192)
        os.write(master, b"yes\n")
        assert proc.wait(timeout=20) == 0
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        assert next(e for e in events if e["name"] == "answer")["value"] == "yes"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        os.close(master)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_interrupt_reaps_completion_descendants_before_return(transition, monkeypatch):
    import io
    import psutil
    import time
    from hermes_cli import update_completion

    root, _git, _old, _new, request = transition
    (root / "hermes_cli/update_completion.py").write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print('READY ' + str(child.pid), flush=True)\n"
        "time.sleep(600)\n"
    )
    pids = []

    class Interrupt(io.StringIO):
        def write(self, value):
            if 'READY ' in value:
                pids.append(int(value.split('READY ')[1].strip()))
                raise KeyboardInterrupt()
            return super().write(value)

    monkeypatch.setattr(sys, "stdout", Interrupt())
    try:
        with pytest.raises(KeyboardInterrupt):
            update_completion.run_completion(request)
        assert pids
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pids[0]) or psutil.Process(pids[0]).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.02)
        else:
            pytest.fail("completion descendant survived parent cancellation")
    finally:
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("failure", ["launch", "timeout", "nonzero"])
def test_taskkill_failure_still_reaps_child_and_preserves_interrupt(tmp_path, monkeypatch, failure):
    """Only native Windows exercises taskkill dispatch and retained-handle kill."""
    import io
    from hermes_cli import update_completion

    package = tmp_path / "hermes_cli"
    package.mkdir()
    (package / "update_completion.py").write_text(
        "import time\nprint('READY', flush=True)\ntime.sleep(60)\n"
    )
    request = {"source": str(tmp_path), "home": str(tmp_path), "receipt": {"update_id": "test"}}
    interrupted = KeyboardInterrupt("cancel completion")
    child = None
    waits = []
    popen, run = subprocess.Popen, subprocess.run

    def capture_child(*args, **kwargs):
        nonlocal child
        proc = popen(*args, **kwargs)
        # The nonzero fault uses a real command, but it is not the completion child.
        if child is None:
            child = proc
            wait = proc.wait

            def bounded_wait(timeout=None):
                waits.append(timeout)
                return wait(timeout=timeout)

            monkeypatch.setattr(proc, "wait", bounded_wait)
        return proc

    def failed_taskkill(command, **kwargs):
        assert child is not None
        assert command == ["taskkill", "/T", "/F", "/PID", str(child.pid)]
        if failure == "launch":
            raise FileNotFoundError("taskkill unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        # Keep subprocess.run's real nonzero/check behavior, without killing a tree.
        return run([sys.executable, "-c", "raise SystemExit(9)"], **kwargs)

    class Interrupt(io.StringIO):
        def write(self, value):
            result = super().write(value)
            if "READY" in self.getvalue():
                raise interrupted
            return result

    monkeypatch.setattr(subprocess, "Popen", capture_child)
    monkeypatch.setattr(subprocess, "run", failed_taskkill)
    monkeypatch.setattr(sys, "stdout", Interrupt())
    try:
        with pytest.raises(KeyboardInterrupt) as error:
            update_completion.run_completion(request)
        assert error.value is interrupted
        expected = {"launch": FileNotFoundError, "timeout": subprocess.TimeoutExpired,
                    "nonzero": subprocess.CalledProcessError}[failure]
        assert isinstance(error.value.__cause__, expected)
        assert child is not None
        assert child.poll() is not None, "retained completion child survived failed taskkill"
        assert child.stdout.closed
        assert waits and all(timeout is not None and timeout > 0 for timeout in waits)
    finally:
        # Only this disposable child is ours; no service or process scan is involved.
        if child is not None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("correlated", [False, True])
def test_only_correlated_terminal_receipt_can_acknowledge_success(transition, encoding, correlated):
    from hermes_cli.update_completion import run_completion

    root, _git, _old, _new, request = transition
    receipt = {"update_id": request["receipt"]["update_id"] if correlated else "wrong",
               "outcome": "success", "finished_at": "now", "detail": "日本 café"}
    (root / "hermes_cli/update_completion.py").write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps(dict(\n"
        "    schema=1, update_id=request['receipt']['update_id'], exit_code=0,\n"
        f"    windows_resume={{}}, receipt={receipt!r}), ensure_ascii=False), encoding={encoding!r})\n",
        encoding="utf-8",
    )
    response = run_completion(request)
    assert response["exit_code"] == (0 if correlated else 1)
    assert response["receipt"] == (receipt if correlated else None)
