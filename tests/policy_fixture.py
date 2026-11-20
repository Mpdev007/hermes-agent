"""Reviewed local profiles and real guard discovery for offline regression fixtures."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace


def discover_guard(tmp_path, monkeypatch, state="ready"):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import plugins, plugins_manifest
    empty = tmp_path / "bundled"
    empty.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: empty)
    # Conftest seeds an unknown version to prevent git subprocesses; this fixture models
    # the verified v0.23.1 base explicitly and still executes the real manifest gate.
    monkeypatch.setattr(plugins_manifest, "running_hermes_version", lambda: "0.23.1")
    config = {"model": {"default": "offline-fixture"}, "platform_toolsets": {"acp": ["terminal"], "cli": ["terminal"]},
              "plugins": {"enabled": [] if state == "disabled" else ["unattended-guard"],
                          "required_tool_policies": {source: [{"key": "unattended-guard", "source": "user"}] for source in
                                                     ("acp", "a2a", "api_server", "cron", "oneshot", "webhook")}}}
    if state == "missing_declaration":
        config["plugins"].pop("required_tool_policies")
    (home / "config.yaml").write_text(json.dumps(config))
    if state != "absent":
        target = home / "plugins/unattended-guard"
        target.parent.mkdir()
        target.mkdir()
        original = Path(__file__).resolve().parents[2] / "guard-policy-v5"
        for filename in ("__init__.py", "allowlist.json", "plugin.yaml"):
            (target / filename).write_bytes((original / filename).read_bytes())
        if state == "import_failed":
            (target / "__init__.py").write_text("raise RuntimeError('fixture import failed')\n")
        if state == "manifest_failed":
            manifest = target / "plugin.yaml"
            manifest.write_text(manifest.read_text().replace(">=0.23.1", ">=99.0.0"))
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
    manager.discover_and_load()
    import model_tools
    from gateway.session_policy import build_policy
    from gateway import session_policy
    root = Path(__file__).resolve().parents[1]
    for module in (plugins, model_tools, session_policy):
        assert Path(module.__file__).resolve().is_relative_to(root), "mixed source checkout"
    policy = build_policy({"source": "acp", "cwd": str(tmp_path), "model": "offline-fixture"}, config)
    return SimpleNamespace(home=home, manager=manager, config=config, policy=policy)


@contextmanager
def protected(fixture, policy=None):
    from gateway.session_policy import policy_scope
    with policy_scope(policy or fixture.policy):
        yield
