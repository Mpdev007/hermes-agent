"""A same-key plugin from a different native source cannot satisfy a user-source pin.

Directory collision and entry-point fallback run real native discovery. Fixtures
are trusted, harmless callbacks; importing an untrusted plugin is not contained.
"""
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from tests.policy_fixture import discover_guard, protected


@pytest.mark.parametrize("origin", ["project", "entrypoint"])
def test_same_key_and_declared_fingerprint_do_not_substitute_for_native_source(tmp_path, monkeypatch, origin):
    fixture = discover_guard(tmp_path, monkeypatch, "ready" if origin == "project" else "absent")
    from hermes_cli import plugins_discovery
    from hermes_cli.tool_dispatch_policy import final_tool_block
    calls = []
    if origin == "project":
        project = tmp_path / "project"
        target = project / ".hermes/plugins/unattended-guard"
        target.parent.mkdir(parents=True)
        # Exact same guard/table and same key; project wins normal discovery.
        target.mkdir()
        for name in ("__init__.py", "allowlist.json", "plugin.yaml"):
            (target / name).write_bytes((fixture.home / "plugins/unattended-guard" / name).read_bytes())
        monkeypatch.chdir(project)
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    else:
        def substitute(**_kwargs):
            calls.append(True)

        def register(ctx):
            ctx.register_hook("pre_tool_dispatch", substitute, policy_fingerprint="a" * 64)

        ep = SimpleNamespace(name="unattended-guard", value="tests.policy_fixture:register",
                             group=plugins_discovery.ENTRY_POINTS_GROUP, load=lambda: register)
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda: [ep])
    fixture.manager.discover_and_load(force=True)
    loaded = fixture.manager._plugins["unattended-guard"]
    assert loaded.enabled and loaded.manifest.source == origin
    assert fixture.manager.policy_evidence("unattended-guard")
    with protected(fixture):
        decision = final_tool_block("mcp__leann__leann_search", {})
    assert decision and "native source differs" in decision
    assert calls == []


def test_user_source_pin_allows_actual_owned_guard_decision(tmp_path, monkeypatch):
    fixture = discover_guard(tmp_path, monkeypatch)
    from hermes_cli.tool_dispatch_policy import final_tool_block
    assert fixture.manager._plugins["unattended-guard"].manifest.source == "user"
    with protected(fixture):
        assert final_tool_block("mcp__leann__leann_search", {}) is None
        assert final_tool_block("terminal", {}) is not None


@pytest.mark.parametrize("entry", ["unattended-guard", {"key": "unattended-guard"},
                                  {"key": "unattended-guard", "source": "unknown"},
                                  {"key": "unattended-guard", "source": "user", "policy_sha256": "a" * 64}])
def test_missing_or_self_declared_only_provenance_fails_closed(tmp_path, monkeypatch, entry):
    fixture = discover_guard(tmp_path, monkeypatch)
    fixture.config["plugins"]["required_tool_policies"]["acp"] = [entry]
    (fixture.home / "config.yaml").write_text(json.dumps(fixture.config))
    from hermes_cli.tool_dispatch_policy import final_tool_block
    with protected(fixture):
        assert final_tool_block("mcp__leann__leann_search", {}) is not None
