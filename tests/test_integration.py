"""The real Hermes plugin loader, hook dispatch and lifecycle API; only the child's run is stubbed.

Needs a Hermes checkout on sys.path (run pytest with Hermes' venv, or set HERMES_SOURCE). With
HERMES_SOURCE set (CI does) a Hermes that does not import fails the run instead of skipping it.
"""
import dataclasses
import json
import os
from types import SimpleNamespace

import pytest
from conftest import DIRECT, PLUGIN_ROOT

if os.environ.get("HERMES_SOURCE"):
    import hermes_cli.plugins  # noqa: F401
else:
    pytest.importorskip("hermes_cli.plugins")

CHILD_ID = "sa-0-c0ffee01"
CHILD_SESSION = "child-session-1"


@pytest.fixture
def loaded(hermes_home, monkeypatch):
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "hermes-claude-agents").symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled: [hermes-claude-agents]\n", encoding="utf-8")
    from hermes_cli import plugins as hermes_plugins
    hermes_plugins._reset_plugin_managers_for_tests()
    manager = hermes_plugins.get_plugin_manager()
    manager.discover_and_load()
    yield SimpleNamespace(manager=manager, plugins=hermes_plugins)
    hermes_plugins._reset_plugin_managers_for_tests()


def test_loader_registers_tools_hooks_command_and_prompt(loaded):
    record = loaded.manager._plugins["hermes-claude-agents"]
    assert not record.error, record.error
    assert {"agents_run", "agents_status"} <= loaded.manager._plugin_tool_names
    for hook in ("pre_tool_call", "subagent_start", "subagent_stop", "transform_tool_result"):
        assert loaded.plugins.has_hook(hook), hook
    text = loaded.manager._plugin_commands["claude-agents"]["handler"]("")
    assert "reviewer (opus" in text and "explorer (haiku" in text and "builder (sonnet" in text
    sections = loaded.plugins.render_system_prompt_sections({"session_id": "s", "model": "sonnet"})
    assert any("agents_run" in section.content for section in sections)


def test_real_lifecycle_launch_guard_and_result(loaded, monkeypatch):
    from agent.subagent_lifecycle import SubagentLaunchRequest, bind_subagent_parent
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    from tools import delegate_tool
    from tools.registry import registry

    built, guard_seen_at_start, resolved = [], [], []
    per_launch_provider = "provider" in {f.name for f in dataclasses.fields(SubagentLaunchRequest)}

    def resolve(cfg, parent):
        # Builds with per-launch provider routing resolve the route's credentials; the test home has no
        # provider plugins, so answer like the runtime provider system would.
        resolved.append({"provider": cfg.get("provider"), "model": cfg.get("model")})
        provider = cfg.get("provider") or None
        return {"model": cfg.get("model") or None, "provider": provider,
                "base_url": f"process://{provider}" if provider else None,
                "api_key": "test-placeholder" if provider else None,
                "api_mode": "chat_completions" if provider else None}

    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", resolve, raising=False)

    def build(**kwargs):
        built.append(kwargs)
        # Hermes fires subagent_start inside child construction, before launch() returns.
        invoke_hook("subagent_start", parent_session_id="lead", parent_turn_id="t1", parent_subagent_id=None,
                    child_session_id=CHILD_SESSION, child_subagent_id=CHILD_ID, child_role="leaf",
                    child_goal=kwargs["goal"])
        guard_seen_at_start.append(_dispatch_pre_tool_call_hooks("write_file", {"path": "x"}, task_id=CHILD_ID)[0])
        return SimpleNamespace(_subagent_id=CHILD_ID, provider=DIRECT, model=kwargs.get("model"),
                               _delegate_role="leaf", _delegate_depth=1, session_id=CHILD_SESSION)

    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", lambda *args: {
        "status": "completed", "summary": "explorer report: router lives in app/routes.py:12",
        "api_calls": 2, "duration_seconds": 1.5})
    parent = SimpleNamespace(session_id="lead", provider=DIRECT, model="sonnet", enabled_toolsets=None,
                             _delegate_depth=0)
    entry = registry.get_entry("agents_run", scope=loaded.manager.scope_key)
    with bind_subagent_parent(parent):
        out = json.loads(entry.handler({"tasks": [{"agent": "explorer", "goal": "Where is the router?"}],
                                        "wait_seconds": 20}, session_id="lead"))
    assert out["ok"], out
    result = out["results"][0]
    assert result["status"] == "succeeded", json.dumps(result)
    assert "app/routes.py:12" in result["summary"]
    assert result["model"] == "haiku" and result["api_calls"] == 2
    assert built[0]["model"] == "haiku" and built[0]["role"] == "leaf"
    assert built[0]["context"].startswith("## Agent: explorer")
    if per_launch_provider:  # Hermes main since 2026-10-12 (NousResearch/hermes-agent#137167)
        assert resolved == [{"provider": DIRECT, "model": "haiku"}]
        assert built[0]["override_provider"] == DIRECT
    else:  # older builds: the child runs on the lead's provider
        assert resolved == [] and "override_provider" not in built[0]
    # The guard was live before the child could have made its first tool call ...
    assert guard_seen_at_start[0] and "read-only" in guard_seen_at_start[0]
    # ... still holds afterwards, through Hermes' real pre_tool_call dispatch ...
    assert "read-only" in _dispatch_pre_tool_call_hooks("terminal", {"command": "ls"}, task_id=CHILD_ID)[0]
    assert _dispatch_pre_tool_call_hooks("read_file", {"path": "x"}, task_id=CHILD_ID)[0] is None
    # ... and never touches tool calls of the lead or of other agents.
    assert _dispatch_pre_tool_call_hooks("write_file", {"path": "x"}, task_id="lead-task")[0] is None


def test_real_launch_batch_binds_each_childs_policy(loaded, monkeypatch):
    """Two agents through Hermes' real launch_batch and hook dispatch: one delegation, one policy per child."""
    from agent.subagent_lifecycle import SubagentLifecycleService, bind_subagent_parent
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    from tools import delegate_tool
    from tools.registry import registry

    if not hasattr(SubagentLifecycleService, "launch_batch"):
        pytest.skip("this Hermes build has no launch_batch")
    built, guard_seen_at_start = [], {}

    def resolve(cfg, parent):
        return {"model": cfg.get("model") or None, "provider": cfg.get("provider") or None,
                "base_url": "process://x" if cfg.get("provider") else None,
                "api_key": "test-placeholder" if cfg.get("provider") else None,
                "api_mode": "chat_completions" if cfg.get("provider") else None}

    def build(**kwargs):
        child_id = f"child-{kwargs['task_index']}"
        built.append(kwargs)
        invoke_hook("subagent_start", parent_session_id="lead", parent_turn_id="t1", parent_subagent_id=None,
                    child_session_id=f"{child_id}-session", child_subagent_id=child_id, child_role="leaf",
                    child_goal=kwargs["goal"])
        guard_seen_at_start[child_id] = _dispatch_pre_tool_call_hooks("write_file", {"path": "x"}, task_id=child_id)[0]
        return SimpleNamespace(_subagent_id=child_id, provider=DIRECT, model=kwargs.get("model"),
                               _delegate_role="leaf", _delegate_depth=1, session_id=f"{child_id}-session",
                               _progress_identity_ref={"session_id": f"{child_id}-session"})

    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", resolve, raising=False)
    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", lambda index, *args: {
        "task_index": index, "status": "completed", "summary": f"report {index}", "api_calls": 1,
        "duration_seconds": 0.5})
    parent = SimpleNamespace(session_id="lead", provider=DIRECT, model="sonnet", enabled_toolsets=None,
                             _delegate_depth=0)
    entry = registry.get_entry("agents_run", scope=loaded.manager.scope_key)
    with bind_subagent_parent(parent):
        out = json.loads(entry.handler({"tasks": [{"agent": "explorer", "goal": "Where is the router?"},
                                                  {"agent": "builder", "goal": "Fix the router"}],
                                        "wait_seconds": 20}, session_id="lead"))
    assert out["ok"], out
    assert [r["status"] for r in out["results"]] == ["succeeded", "succeeded"]
    assert [r["summary"] for r in out["results"]] == ["report 0", "report 1"]
    assert [(b["task_index"], b["task_count"]) for b in built] == [(0, 2), (1, 2)]
    # One delegation for the batch, stamped on every child, and a live transcript each.
    assert len({b.get("_delegation_id") for b in built}) <= 1
    # The read-only explorer was guarded before its first tool call; the builder may write.
    assert guard_seen_at_start["child-0"] and "read-only" in guard_seen_at_start["child-0"]
    assert guard_seen_at_start["child-1"] is None


def test_agents_run_declares_the_delegation_card_when_hermes_takes_it(loaded):
    """On a Hermes build with `register_tool(ui_card=...)`, agents_run renders like delegate_task."""
    import inspect

    from hermes_cli.plugins import PluginContext
    from tools.registry import registry

    entry = registry.get_entry("agents_run", scope=loaded.manager.scope_key)
    if "ui_card" in inspect.signature(PluginContext.register_tool).parameters:
        assert getattr(entry, "ui_card", None) == "delegate"
        assert getattr(registry.get_entry("agents_status", scope=loaded.manager.scope_key), "ui_card", None) is None
    else:  # older builds: registered without the card, nothing else changes
        assert getattr(entry, "ui_card", None) is None


def test_outside_an_agent_turn_the_tool_refuses(loaded):
    from tools.registry import registry
    entry = registry.get_entry("agents_run", scope=loaded.manager.scope_key)
    out = json.loads(entry.handler({"tasks": [{"agent": "explorer", "goal": "x"}]}))
    assert out["error"] == "no_active_turn"


def test_scouting_nudge_through_the_real_result_hook(loaded):
    from agent.subagent_lifecycle import bind_subagent_parent
    from model_tools import _apply_transform_tool_result_hook, _CallIds

    parent = SimpleNamespace(session_id="lead", provider=DIRECT, model="sonnet", enabled_toolsets=None,
                             _delegate_depth=0)
    ids = _CallIds(task_id="lead", session_id="lead", tool_call_id="c", turn_id="turn-1", api_request_id="")
    with bind_subagent_parent(parent):
        out = [_apply_transform_tool_result_hook("read_file", {"path": "x"}, '{"content": "x"}', 1, ids)
               for _ in range(8)]
    assert out[:7] == ['{"content": "x"}'] * 7
    assert "[hermes-claude-agents] 8 read/search calls" in out[7] and "explorer" in out[7]
