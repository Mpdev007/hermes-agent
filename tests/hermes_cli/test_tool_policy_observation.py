"""Process-bound observation of captured policy registrations, without a model/tool call."""
import asyncio
import os
from types import SimpleNamespace

import pytest


@pytest.fixture
def policy_manager(tmp_path, monkeypatch):
    from hermes_cli import plugins

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    directory = home / "plugins" / "policy-fixture"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        "name: policy-fixture\nversion: 1.0.0\ndescription: policy fixture\n"
        "provides_hooks: [pre_tool_dispatch]\n")
    (directory / "__init__.py").write_text('''
def final_policy(tool_name, **kwargs):
    if tool_name != "fixture_read":
        return {"action": "block", "message": "fixture final veto"}
def register(ctx):
    ctx.register_hook("pre_tool_dispatch", final_policy, policy_fingerprint="a" * 64)
''')
    (home / "config.yaml").write_text("plugins:\n  enabled: [policy-fixture]\n")
    empty = tmp_path / "bundled"
    empty.mkdir()
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: empty)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "discover_plugins", lambda **_k: None)
    monkeypatch.setattr(plugins, "get_plugin_toolsets", list)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
    # The offline fence deliberately denies /proc. Inject the owner identity
    # seam; this case tests forwarding it, not live process attestation.
    from gateway import status
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 12345 if pid == os.getpid() else None)
    return home, directory, manager


def test_real_discovery_captures_active_policy_and_observation_is_process_bound(policy_manager):
    from gateway.session_discovery import list_plugins

    home, directory, manager = policy_manager
    row = next(p for p in manager.list_plugins() if p["name"] == "policy-fixture")
    evidence = row["policy_evidence"]
    assert evidence[0]["hook"] == "pre_tool_dispatch"
    assert evidence[0]["policy_sha256"] == "a" * 64
    assert len(evidence[0]["callback_code_sha256"]) == 64
    (directory / "__init__.py").write_text("raise AssertionError('new disk bytes must not run during observation')\n")
    actor = SimpleNamespace(capabilities={"session:read"}, profile_id=str(home))
    authority = SimpleNamespace(profile_id=str(home))
    result = asyncio.run(list_plugins(authority, actor, {"observe_only": True}))
    observed = next(p for p in result["plugins"] if p["name"] == "policy-fixture")
    assert observed["policy_evidence"] == evidence
    assert result["policy_observation"]["pid"] == os.getpid()
    assert result["policy_observation"]["start_time"] == 12345
    assert result["policy_observation"]["profile_id"] == str(home)
    manager.unload()
    assert manager.policy_evidence(row["key"]) == []


@pytest.mark.parametrize("wrong_profile,can_read", [(True, True), (False, False)])
def test_policy_observation_retains_existing_authorization(policy_manager, wrong_profile, can_read):
    from gateway.session_discovery import list_plugins
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError

    home, _, _ = policy_manager
    authority = SimpleNamespace(profile_id=str(home))
    actor = SimpleNamespace(capabilities={"session:read"} if can_read else set(),
                            profile_id=str(home) + "/other" if wrong_profile else str(home))
    with pytest.raises(RuntimeStoreError, match="permission_denied"):
        asyncio.run(list_plugins(authority, actor, {}))


def test_observation_never_initiates_discovery_or_toolset_loading(policy_manager, monkeypatch):
    from gateway.session_discovery import list_plugins
    from hermes_cli import plugins

    home, _, manager = policy_manager
    manager.unload()

    def prohibited(**_kwargs):
        raise AssertionError("read-only observation must not load plugins or toolsets")

    monkeypatch.setattr(plugins, "discover_plugins", prohibited)
    monkeypatch.setattr(plugins, "get_plugin_toolsets", prohibited)
    actor = SimpleNamespace(capabilities={"session:read"}, profile_id=str(home))
    result = asyncio.run(list_plugins(SimpleNamespace(profile_id=str(home)), actor, {"observe_only": True}))
    assert result["plugins"] == []
    assert result["policy_observation"]["pid"] == os.getpid()


@pytest.mark.parametrize("params", [{"observe_only": "true"}, {"observe_only": 1}, {"extra": True}, []])
def test_observation_rejects_invalid_parameters(policy_manager, params):
    from gateway.session_discovery import list_plugins
    from persistence.core.errors import AuthorityRefusal as RuntimeStoreError

    home, _, _ = policy_manager
    actor = SimpleNamespace(capabilities={"session:read"}, profile_id=str(home))
    with pytest.raises(RuntimeStoreError, match="invalid_params"):
        asyncio.run(list_plugins(SimpleNamespace(profile_id=str(home)), actor, params))


def test_final_policy_event_rejects_shell_registration(caplog):
    from agent.shell_hooks import _parse_hooks_block

    assert _parse_hooks_block({"pre_tool_dispatch": [{"command": "fixture-command-not-executed"}]}) == []
    assert "Python-plugin-only" in caplog.text
