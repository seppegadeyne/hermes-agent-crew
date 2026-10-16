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
    (plugins_dir / "hermes-agent-crew").symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled: [hermes-agent-crew]\n", encoding="utf-8")
    from hermes_cli import plugins as hermes_plugins
    hermes_plugins._reset_plugin_managers_for_tests()
    manager = hermes_plugins.get_plugin_manager()
    manager.discover_and_load()
    yield SimpleNamespace(manager=manager, plugins=hermes_plugins)
    hermes_plugins._reset_plugin_managers_for_tests()


def test_loader_registers_tools_hooks_command_and_prompt(loaded):
    record = loaded.manager._plugins["hermes-agent-crew"]
    assert not record.error, record.error
    assert {"agents_run", "agents_status"} <= loaded.manager._plugin_tool_names
    for hook in ("pre_tool_call", "subagent_start", "subagent_stop", "transform_tool_result"):
        assert loaded.plugins.has_hook(hook), hook
    text = loaded.manager._plugin_commands["crew"]["handler"]("")
    assert "reviewer    strong" in text and "haiku @" in text and "builder     balanced  sonnet @" in text
    alias = loaded.manager._plugin_commands["claude-agents"]["handler"]("")
    assert alias.startswith("/claude-agents is deprecated; use /crew.") and "explorer" in alias
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
    assert "[hermes-agent-crew] 8 read/search calls" in out[7] and "explorer" in out[7]


def _stub_child(monkeypatch, built):
    from tools import delegate_tool

    def build(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(_subagent_id=CHILD_ID, provider=kwargs.get("override_provider"),
                               model=kwargs.get("model"), _delegate_role="leaf", _delegate_depth=1,
                               session_id=CHILD_SESSION)

    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", lambda *args: {
        "status": "completed", "summary": "local report", "api_calls": 1, "duration_seconds": 0.5})


LOCAL_PROVIDER = (
    "providers:\n"
    "  local-test:\n"
    "    name: Local test server\n"
    "    base_url: http://127.0.0.1:9/v1\n"
    "    api_mode: chat_completions\n"
    "    default_model: local-model\n")


def _launch_explorer(hermes_home, monkeypatch, config_tail):
    """Load the plugin with LOCAL_PROVIDER + config_tail, run one explorer, return the child's build kwargs."""
    from agent.subagent_lifecycle import bind_subagent_parent
    from hermes_cli import plugins as hermes_plugins
    from tools.registry import registry

    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "hermes-agent-crew").symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (hermes_home / "config.yaml").write_text(LOCAL_PROVIDER + config_tail, encoding="utf-8")
    hermes_plugins._reset_plugin_managers_for_tests()
    manager = hermes_plugins.get_plugin_manager()
    manager.discover_and_load()
    try:
        built = []
        _stub_child(monkeypatch, built)
        # The test endpoint does not exist: let the reachability probe (urllib, whichever copy of the
        # plugin module Hermes' loader created) see it as up.
        import contextlib
        import urllib.request
        probed = []

        class Opener:
            def open(self, request, timeout=None):
                probed.append(request.full_url)
                return contextlib.nullcontext()

        monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
        parent = SimpleNamespace(session_id="lead", provider=DIRECT, model="sonnet", enabled_toolsets=None,
                                 _delegate_depth=0, api_key="lead-key", base_url="process://lead")
        entry = registry.get_entry("agents_run", scope=manager.scope_key)
        with bind_subagent_parent(parent):
            out = json.loads(entry.handler({"tasks": [{"agent": "explorer", "goal": "Where is the router?"}],
                                            "wait_seconds": 20}, session_id="lead"))
        assert out["ok"], out
        assert out["results"][0]["status"] == "succeeded", out
        assert built, "the child was never built"
        built[0]["_probed"] = probed
        return built[0]
    finally:
        hermes_plugins._reset_plugin_managers_for_tests()


def _needs_per_launch_provider():
    from agent.subagent_lifecycle import SubagentLaunchRequest
    if "provider" not in {f.name for f in dataclasses.fields(SubagentLaunchRequest)}:
        pytest.skip("this Hermes build has no per-launch provider routing")


def test_named_custom_provider_resolves_through_hermes(hermes_home, monkeypatch):
    """A tier on a named `providers:` entry (a keyless local endpoint) reaches that endpoint through
    Hermes' own runtime provider resolution: no URL or key in the agent file, never the lead's key."""
    _needs_per_launch_provider()
    kwargs = _launch_explorer(hermes_home, monkeypatch, (
        "plugins:\n"
        "  enabled: [hermes-agent-crew]\n"
        "  entries:\n"
        "    hermes-agent-crew:\n"
        "      settings:\n"
        "        tiers: {fast: {provider: local-test, model: local-model}}\n"))
    assert kwargs["model"] == "local-model"
    assert kwargs.get("override_base_url", "").rstrip("/") == "http://127.0.0.1:9/v1", kwargs
    assert kwargs.get("override_api_key") not in (None, "", "lead-key"), kwargs
    assert kwargs["_probed"] == ["http://127.0.0.1:9/v1/models"]  # the real host probed the endpoint


def test_provider_less_launch_follows_the_delegation_config(hermes_home, monkeypatch):
    """With provider: inherit the plugin sends no provider; Hermes then routes through `delegation:`,
    not to the lead. Pinned here so the README and the tier rules stay true."""
    _needs_per_launch_provider()
    kwargs = _launch_explorer(hermes_home, monkeypatch, (
        "delegation:\n"
        "  provider: local-test\n"
        "plugins:\n"
        "  enabled: [hermes-agent-crew]\n"
        "  entries:\n"
        "    hermes-agent-crew:\n"
        "      settings:\n"
        "        provider: inherit\n"))
    assert kwargs["model"] == "haiku"
    assert kwargs.get("override_base_url", "").rstrip("/") == "http://127.0.0.1:9/v1", kwargs


def test_endpoint_lookup_and_probe_against_a_real_socket(hermes_home):
    """endpoint_url reads Hermes' own config; endpoint_up treats refused connections as down and any
    HTTP answer below 500 (even 404) as up."""
    import http.server
    import threading

    from hermes_agent_crew.agents_host import HermesHost

    (hermes_home / "config.yaml").write_text(LOCAL_PROVIDER, encoding="utf-8")
    assert HermesHost.endpoint_url("local-test") == "http://127.0.0.1:9/v1"
    assert HermesHost.endpoint_url("openrouter") is None  # hosted providers are not probed
    (hermes_home / "config.yaml").write_text(
        "providers:\n"
        "  gpu-box:\n"
        "    name: GPU Box\n"
        "    api: http://127.0.0.1:7/v1\n"
        "  parked:\n"
        "    enabled: false\n"
        "    base_url: http://127.0.0.1:6/v1\n"
        "custom_providers:\n"
        "  - name: legacy-box\n"
        "    base_url: http://127.0.0.1:5/v1\n", encoding="utf-8")
    assert HermesHost.endpoint_url("gpu-box") == "http://127.0.0.1:7/v1"   # api: key, as Hermes reads it
    assert HermesHost.endpoint_url("GPU Box") == "http://127.0.0.1:7/v1"   # Hermes' name normalization
    assert HermesHost.endpoint_url("parked") is None                       # disabled entries are not used
    assert HermesHost.endpoint_url("legacy-box") == "http://127.0.0.1:5/v1"  # the legacy list form
    assert HermesHost.endpoint_up("http://127.0.0.1:9/v1", timeout=0.5) is False

    class Handler(http.server.BaseHTTPRequestHandler):
        code = 404

        def do_GET(self):
            self.send_response(Handler.code)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        assert HermesHost.endpoint_up(url, timeout=2) is True
        Handler.code = 503  # llama.cpp while it loads a model
        assert HermesHost.endpoint_up(url, timeout=2) is False
    finally:
        server.shutdown()


def test_two_stage_plan_through_the_real_lifecycle(loaded, monkeypatch):
    """scan -> verify through Hermes' real loader, tool registry and lifecycle API; only child runs are stubbed."""
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools import delegate_tool
    from tools.registry import registry

    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda cfg, parent: {
        "model": cfg.get("model") or None, "provider": cfg.get("provider") or None,
        "base_url": "process://x" if cfg.get("provider") else None,
        "api_key": "placeholder" if cfg.get("provider") else None,
        "api_mode": "chat_completions" if cfg.get("provider") else None}, raising=False)
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(_subagent_id=f"sa-0-plan{len(built):04d}", provider=DIRECT, model=kwargs.get("model"),
                               _delegate_role="leaf", _delegate_depth=1, session_id=f"child-plan-{len(built)}")

    def run_child(task_index, goal, child, parent):
        if "## Items" in (built[int(child._subagent_id[-4:]) - 1]["context"] or ""):
            return {"status": "completed", "summary": 'ok\n```json\n{"items": [{"id": "scan-1"}], "rejected": []}\n```'}
        return {"status": "completed", "summary": 'found\n```json\n{"items": [{"title": "off by one in mean"}]}\n```'}

    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", run_child)
    parent = SimpleNamespace(session_id="lead", provider=DIRECT, model="sonnet", enabled_toolsets=None,
                             _delegate_depth=0)
    entry = registry.get_entry("agents_run", scope=loaded.manager.scope_key)
    plan = {"stages": [{"name": "scan", "agent": "explorer", "goal": "Find bugs in stats.py."},
                       {"name": "verify", "agent": "verifier", "goal": "Check them.", "for_each": "scan",
                        "verify": True}]}
    with bind_subagent_parent(parent):
        out = json.loads(entry.handler({"plan": plan, "wait_seconds": 30}, session_id="lead"))
    assert out["ok"] and out["plan"]["status"] == "succeeded", out
    assert out["plan"]["items"] == [{"id": "scan-1", "title": "off by one in mean", "status": "confirmed"}]
    assert [k["model"] for k in built] == ["haiku", "sonnet"]
    assert "ONE fenced ```json block" in built[0]["context"] and "You verify the findings" in built[1]["context"]
