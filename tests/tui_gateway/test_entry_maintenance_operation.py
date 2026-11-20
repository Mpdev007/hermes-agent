"""Port adaptation A7 (Hermes eb98a565): the TUI gateway entry ends a default-only start-up operation only when its
``main`` begins serving independent requests, never at import. ``tui_gateway.entry`` is also imported as a helper
(``session_registry`` and ``agent_factory`` at agent build, ``server`` for MCP discovery) inside processes whose own
restricted operation must keep its restriction."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="default-only maintenance refuses Windows")

REPO = Path(__file__).resolve().parents[2]

HELPER_IMPORT = (
    "import hermes_bootstrap\n"  # what the entry imports first; that process's own launch admission runs here
    "from hermes_cli import maintenance_scope as ms\n"
    "ms._admitted = ms.MaintenanceScope.select('default')\n"  # a restricted operation is live in this process
    "import tui_gateway.entry\n"  # the helper import (agent build / MCP discovery)
    "print('KEPT' if ms._admitted is not None and ms._admitted.mode == 'default' else 'ENDED')\n"
)


def test_a_helper_import_keeps_the_processes_restricted_operation(tmp_path):
    """A fresh process, so the entry's module code really runs on this import."""
    home = tmp_path / ".hermes"
    home.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1",
           "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", HELPER_IMPORT], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "KEPT", "importing the entry ended another process's operation"


def test_main_ends_the_operation_before_it_serves(tmp_path, monkeypatch):
    from gateway import runtime_ownership
    from hermes_cli import maintenance_scope
    from tui_gateway import entry

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    monkeypatch.setattr(maintenance_scope, "_admitted", maintenance_scope.MaintenanceScope.select("default"))
    served = []
    monkeypatch.setattr(entry, "_claim_launch_home", lambda: tmp_path / ".hermes")
    monkeypatch.setattr(entry, "_serve", lambda: served.append(maintenance_scope._admitted))
    monkeypatch.setattr(runtime_ownership.process_ownership, "release", lambda home: None)
    entry.main()
    assert served == [None], "the entry served its client with the start-up restriction"
