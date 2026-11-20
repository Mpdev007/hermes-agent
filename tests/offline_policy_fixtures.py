"""Minimal dependencies for the explicit no-conftest policy lane only.

No host/container/process discovery, ambient dotenv, child reaping or credential
providers. Real policy/registration/dispatch implementations remain under test.
"""
import os
from pathlib import Path
import sys

import pytest


def pytest_configure(config):
    if not config.getoption("noconftest"):
        raise RuntimeError("offline policy fixtures require --noconftest")
    config.addinivalue_line("markers", "platforms(*names): native platform selection")


def pytest_collection_modifyitems(items):
    for item in items:
        marker = item.get_closest_marker("platforms")
        if marker and not any(name == sys.platform or name == os.name or name == "any"
                              for name in marker.args):
            item.add_marker(pytest.mark.skip(reason="not this native platform"))


@pytest.fixture(autouse=True)
def isolated_policy_dependencies(tmp_path, monkeypatch):
    home = tmp_path / "offline-home"
    home.mkdir()
    hermes = home / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(hermes))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr(Path, "home", lambda: home)
    # These controlled fixtures never exercise bootstrap's C-level environ
    # replacement. Keep ordinary process-local putenv and mark that seam handled.
    original_putenv = os.putenv

    def fixture_putenv(key, value):
        original_putenv(key, value)

    fixture_putenv._hermes_never_free_environ = True
    monkeypatch.setattr(os, "putenv", fixture_putenv)
    from hermes_cli import version_info
    monkeypatch.setattr(version_info, "_cached_version_info", version_info.VersionInfo(
        "0.23.1", "git.fixture", None, "0" * 40, "fixture", "git"))
    from hermes_platform.host import runtime
    # These fixtures make no claims about container/WSL detection itself.
    monkeypatch.setattr(runtime, "_container_detected", False)
    monkeypatch.setattr(runtime, "_wsl_detected", False)
    monkeypatch.setattr(runtime, "_detect_container", lambda: False)
    from hermes_cli import config_backups
    # Config backup/copy2 metadata is a separate workflow. These cases supply
    # synthetic config bytes and do not qualify backup or restore behavior.
    monkeypatch.setattr(config_backups, "backup_config", lambda *_a, **_k: None)
    from hermes_cli import env_loader, plugins
    # Config inputs are supplied by the individual fixture. Never probe the
    # checkout's .env merely to learn that this isolated run has no credentials.
    monkeypatch.setattr(env_loader, "_dotenv_assignments", lambda *_a, **_k: {})
    from gateway import status
    # Live process attestation is outside these synthetic policy cases. An
    # unavailable identity stays unknown; the forwarding case overrides this seam.
    monkeypatch.setattr(status, "get_process_start_time", lambda _pid: None)
    empty = tmp_path / "offline-bundled"
    empty.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: empty)
    plugins._reset_plugin_managers_for_tests()
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(secret_scope, "_AUTO_PINNED_HOME", None)
    yield
    plugins._reset_plugin_managers_for_tests()
