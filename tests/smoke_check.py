"""Dependency-free orchestration smoke tests; run: python3 tests/smoke_check.py.

Uses a fake Hermes host. This does not authenticate or call the Codex backend.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "heremes_codex_agents"
spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = plugin
spec.loader.exec_module(plugin)
load_roster = plugin.agents_config.load_roster
read_settings = plugin.agents_config.read_settings
AgentsService = plugin.agents_service.AgentsService


class Host:
    isolated = False

    def __init__(self, home):
        self.folder = home
        self.settings = {}
        self.parent_agent = SimpleNamespace(session_id="lead", provider="anthropic", model="claude-sonnet-4-5",
                                            enabled_toolsets=None, _delegate_depth=0)
        self.fields = {"provider", "reasoning_effort"}
        self.lifecycle = Lifecycle(self)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)
    def home(self):
        return self.folder
    def cwd(self):
        return self.folder
    def parent(self):
        return self.parent_agent
    def request_fields(self):
        return self.fields
    def make_request(self, **kwargs):
        return SimpleNamespace(**kwargs)
    def toolset_for_tool(self, name):
        return None
    def spawn_paused(self):
        return False
    def tool_deadline(self):
        return 420
    def interrupted(self):
        return False
    def take_yield(self):
        return False
    def in_child_context(self):
        return False


class Lifecycle:
    def __init__(self, host):
        self.host = host
        self.requests, self.guards, self.cancelled = [], [], []
        self.states, self.results = {}, {}

    def launch(self, request):
        child = "child-" + str(len(self.requests))
        self.requests.append(request)
        self.host.service.on_subagent_start(child_subagent_id=child, child_session_id=child)
        self.guards.append({name: self.host.service.pre_tool_call(tool_name=name, task_id=child)
                            for name in ("read_file", "write_file", "agents_run", "delegate_task")})
        self.states[child] = "RUNNING"
        return SimpleNamespace(subagent_id=child, provider=getattr(request, "provider", None), model=request.model)

    def status(self, handle):
        return SimpleNamespace(state=self.states[handle.subagent_id])
    def result(self, handle):
        return self.results[handle.subagent_id]
    def cancel(self, handle, *, reason):
        self.cancelled.append((handle.subagent_id, reason))
        self.states[handle.subagent_id] = "CANCELLED"
        self.results[handle.subagent_id] = SimpleNamespace(summary=None)
        return SimpleNamespace(accepted=True)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.host = Host(self.home)
        self.service = self.host.service = AgentsService(self.host)

    def launch(self, agent="codex-explorer"):
        return self.service.run({"tasks": [{"agent": agent, "goal": "Inspect"}], "wait_seconds": 0})

    def test_defaults(self):
        settings, problems = read_settings(self.host.get_config)
        roster = load_roster(settings, self.home)
        self.assertEqual(problems + roster.problems, [])
        self.assertEqual({n: (s.model, s.provider, s.effort) for n, s in roster.agents.items()}, {
            "reviewer": ("opus", "claude-subscription-directsdk-experimental", None),
            "builder": ("sonnet", "claude-subscription-directsdk-experimental", None),
            "explorer": ("haiku", "claude-subscription-directsdk-experimental", None),
            "researcher": ("haiku", "claude-subscription-directsdk-experimental", None),
            "codex-reviewer": ("gpt-6-sol", "openai-codex", "high"),
            "codex-builder": ("gpt-6-sol", "openai-codex", "medium"),
            "codex-explorer": ("gpt-6-luna", "openai-codex", "low"),
            "codex-researcher": ("gpt-6-luna", "openai-codex", "low")})

    def test_registration(self):
        tools, hooks, commands, sections = {}, {}, {}, {}
        ctx = SimpleNamespace(
            register_tool=lambda **kw: tools.update({kw["name"]: kw}),
            register_hook=lambda name, fn: hooks.update({name: fn}),
            register_command=lambda name, **kw: commands.update({name: kw}),
            register_system_prompt_section=lambda name, fn, **kw: sections.update({name: fn}))
        with patch.object(plugin, "HermesHost", return_value=self.host):
            plugin.register(ctx)
        self.assertEqual(set(tools), {"agents_run", "agents_status"})
        self.assertEqual({t["toolset"] for t in tools.values()}, {"claude_agents"})
        self.assertEqual(set(commands), {"claude-agents", "codex-agents"})
        self.assertEqual(set(sections), {"codex-agents.guide"})
        self.assertIn("pre_tool_call", hooks)

    def test_cross_provider_and_early_guards(self):
        out = self.launch()
        self.assertTrue(out["ok"])
        req = self.host.lifecycle.requests[0]
        self.assertEqual((req.provider, req.model, req.reasoning_effort), ("openai-codex", "gpt-6-luna", "low"))
        guard = self.host.lifecycle.guards[0]
        self.assertIsNone(guard["read_file"])
        for name in ("write_file", "agents_run", "delegate_task"):
            self.assertEqual(guard[name]["action"], "block")
        self.host.parent_agent._delegate_depth = 1
        self.assertEqual(self.launch()["error"], "nested_agent")

    def test_mixed_provider_call_and_both_guards(self):
        out = self.service.run({"tasks": [{"agent": name, "goal": "Inspect"}
                                         for name in ("explorer", "codex-explorer")], "wait_seconds": 0})
        self.assertEqual(len(out["pending"]), 2)
        self.assertEqual([(r.provider, r.model) for r in self.host.lifecycle.requests], [
            ("claude-subscription-directsdk-experimental", "haiku"), ("openai-codex", "gpt-6-luna")])
        for guard in self.host.lifecycle.guards:
            self.assertIsNone(guard["read_file"])
            for name in ("write_file", "agents_run", "delegate_task"):
                self.assertEqual(guard[name]["action"], "block")

    def test_writer_limit_spans_families(self):
        tasks = [{"agent": name, "goal": "Build"} for name in ("builder", "codex-builder")]
        out = self.service.run({"tasks": tasks, "wait_seconds": 0})
        self.assertEqual(out["error"], "too_many_writers")
        self.assertEqual(self.host.lifecycle.requests, [])
        self.launch("builder")
        self.assertEqual(self.launch("codex-builder")["error"], "too_many_writers")

    def test_old_host_refuses_cross_provider(self):
        self.host.fields = set()
        out = self.launch()
        self.assertEqual(out["results"][0]["note"], "provider_unsupported")
        self.assertEqual(self.host.lifecycle.requests, [])

    def test_provider_inherit_override(self):
        self.host.settings = {"overrides": {"codex-explorer": {"provider": "inherit", "model": "inherit"}}}
        self.launch()
        request = self.host.lifecycle.requests[0]
        self.assertFalse(hasattr(request, "provider"))
        self.assertIsNone(request.model)

    def test_provider_inherit_file(self):
        folder = self.home / "codex-agents"
        folder.mkdir()
        (folder / "codex-explorer.md").write_text(
            "---\nname: codex-explorer\nprovider: inherit\nmodel: inherit\nreadonly: true\n---\nInspect.",
            encoding="utf-8")
        self.launch()
        request = self.host.lifecycle.requests[0]
        self.assertFalse(hasattr(request, "provider"))
        self.assertIsNone(request.model)

    def test_none_effort(self):
        self.host.settings = {"overrides": {"codex-explorer": {"effort": "none"}}}
        self.launch()
        self.assertEqual(self.host.lifecycle.requests[0].reasoning_effort, "none")

    def test_collection_and_cancel(self):
        out = self.launch()
        run_id = out["pending"][0]
        lifecycle = self.host.lifecycle
        lifecycle.states["child-0"] = "SUCCEEDED"
        lifecycle.results["child-0"] = SimpleNamespace(summary="Found it", usage_metadata={"api_calls": 2})
        result = self.service.status({"run_ids": [run_id], "wait_seconds": 0})["results"][0]
        self.assertEqual((result["status"], result["summary"], result["api_calls"]), ("succeeded", "Found it", 2))
        self.assertEqual(self.service.status({})["results"], [])
        run_id = self.launch()["pending"][0]
        result = self.service.status({"run_ids": [run_id], "cancel": True, "wait_seconds": 0})["results"][0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(lifecycle.cancelled[0][0], "child-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
