"""Agent files, settings and roster loading."""
import os
from pathlib import Path

import pytest

from hermes_claude_agents import agents_config as cfg

CLAUDE_CODE_AGENT = """---
name: code-reviewer
description: Reviews diffs
tools: Read, Grep, Glob
model: opus
color: purple
---
Review carefully.
"""


def write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def defaults(raw=None):
    raw = raw or {}
    return cfg.read_settings(lambda key, default=None: raw.get(key, default))


def test_use_when_is_parsed_and_clipped():
    spec, warnings = cfg.spec_from_markdown("---\nname: a\nwhen_to_use: >\n  a file\n  needs checks\n---\nx\n")
    assert spec.use_when == "a file needs checks" and not warnings
    spec, warnings = cfg.spec_from_markdown("---\nname: a\nuse_when: " + "x" * 300 + "\n---\nx\n")
    assert len(spec.use_when) == cfg.MAX_USE_WHEN_CHARS and warnings
    spec, warnings = cfg.spec_from_markdown("---\nname: a\nuse_when: one\nwhen_to_use: two\n---\nx\n")
    assert spec.use_when == "one" and any("when_to_use ignored" in w for w in warnings)


def test_builtin_agents_have_a_trigger(hermes_home):
    settings, _ = defaults()
    roster = cfg.load_roster(settings, hermes_home)
    assert all(roster.agents[n].use_when for n in ("builder", "explorer", "researcher", "reviewer"))


def test_builtin_roster_has_the_four_roles(hermes_home):
    settings, problems = defaults()
    roster = cfg.load_roster(settings, hermes_home)
    assert problems == [] and roster.problems == []
    assert set(roster.agents) == {"reviewer", "explorer", "researcher", "builder"}
    assert (roster.agents["reviewer"].model, roster.agents["reviewer"].readonly) == ("opus", True)
    assert (roster.agents["explorer"].model, roster.agents["explorer"].readonly) == ("haiku", True)
    assert (roster.agents["researcher"].model, roster.agents["researcher"].readonly) == ("haiku", True)
    assert (roster.agents["builder"].model, roster.agents["builder"].readonly) == ("sonnet", False)
    assert all(spec.provider == cfg.DEFAULT_PROVIDER for spec in roster.agents.values())
    assert all(spec.instructions for spec in roster.agents.values())


def test_claude_code_agent_file_maps_tools_and_is_read_only():
    spec, warnings = cfg.spec_from_markdown(CLAUDE_CODE_AGENT)
    assert spec.name == "code-reviewer" and spec.model == "opus"
    assert spec.tools == ("read_file", "search_files")
    assert spec.readonly is True
    assert warnings == []
    assert spec.instructions == "Review carefully."


def test_agent_with_bash_is_not_read_only():
    spec, _ = cfg.spec_from_markdown("---\nname: tester\ntools: [Read, Bash(npm test:*)]\n---\nRun tests.")
    assert spec.tools == ("read_file", "terminal", "process_manage")
    assert spec.readonly is False


def test_read_only_agent_cannot_list_write_tools():
    with pytest.raises(ValueError, match="read-only"):
        cfg.spec_from_markdown("---\nname: x\nreadonly: true\ntools: [read_file, write_file]\n---\n")


def test_read_only_without_tools_gets_the_read_tool_set():
    spec, _ = cfg.spec_from_markdown("---\nname: x\nread-only: yes\n---\n")
    assert spec.readonly and set(spec.tools) == cfg.READONLY_TOOLS


def test_model_inherit_means_the_lead_model():
    spec, _ = cfg.spec_from_markdown("---\nname: x\nmodel: inherit\nprovider: inherit\n---\nbody")
    assert spec.model is None and spec.provider is None


def test_frontmatter_shapes():
    spec, warnings = cfg.spec_from_markdown(
        '---\nname: "scout"\ndescription: >\n  Finds things\n  fast.\ntools:\n  - Read\n  - "WebFetch"\n'
        "effort: low  # inline comment\nmood: calm\n---\nBody")
    assert spec.name == "scout" and spec.description == "Finds things fast."
    assert spec.tools == ("read_file", "web_extract") and spec.readonly
    assert spec.effort == "low"
    assert warnings == ["ignored key 'mood'"]


@pytest.mark.parametrize("text, message", [
    ("---\nname: Bad Name\n---\n", "invalid agent name"),
    ("---\nname: x\nmodel: 'rm -rf'\n---\n", "invalid model"),
    ("---\nname: x\nreadonly: maybe\n---\n", "true or false"),
    ("---\nname: x\n", "closing"),
    ("---\nname: x\nname: y\n---\n", "twice"),
    ("---\nname: x\neffort: turbo\n---\n", "effort"),
    ("---\nname: x\ntools: [Read, rm -rf]\n---\n", "invalid tool"),
])
def test_invalid_agent_files(text, message):
    with pytest.raises(ValueError, match=message):
        cfg.spec_from_markdown(text)


def test_markdown_without_frontmatter_is_skipped(hermes_home):
    write(hermes_home / "claude-agents", "README.md", "# My agents\n")
    settings, _ = defaults()
    assert cfg.load_roster(settings, hermes_home).problems == []


def test_user_folder_overrides_builtin_and_reports_bad_files(hermes_home):
    agents = hermes_home / "claude-agents"
    write(agents, "reviewer.md", "---\nname: reviewer\nmodel: sonnet\nreadonly: true\n---\nMine.")
    write(agents, "broken.md", "---\nname: Broken!\n---\n")
    settings, _ = defaults()
    roster = cfg.load_roster(settings, hermes_home)
    assert roster.agents["reviewer"].model == "sonnet"
    assert roster.agents["reviewer"].instructions == "Mine."
    assert any("broken.md" in problem for problem in roster.problems)


def test_settings_overrides_and_validation(hermes_home):
    settings, problems = defaults({
        "overrides": {"reviewer": {"model": "sonnet"}, "builder": {"enabled": False},
                      "ghost": {"model": "haiku"}, "explorer": {"color": "red"}},
        "max_parallel": 99, "provider": "inherit", "prompt_section": "yes"})
    assert settings["max_parallel"] == 12 and any("max_parallel" in p for p in problems)
    assert settings["prompt_section"] is True and any("prompt_section" in p for p in problems)
    roster = cfg.load_roster(settings, hermes_home)
    assert roster.agents["reviewer"].model == "sonnet"
    assert "builder" not in roster.agents
    assert any("ghost" in p for p in roster.problems)
    assert any("explorer" in p and "color" in p for p in roster.problems)
    assert all(spec.provider is None for spec in roster.agents.values())


def test_import_claude_code_agents(hermes_home):
    write(Path(os.environ["HOME"]) / ".claude" / "agents", "code-reviewer.md", CLAUDE_CODE_AGENT)
    settings, _ = defaults()
    assert "code-reviewer" not in cfg.load_roster(settings, hermes_home).agents
    settings, _ = defaults({"import_claude_code_agents": True})
    assert "code-reviewer" in cfg.load_roster(settings, hermes_home).agents


def test_roster_cache_reloads_when_a_file_changes(hermes_home):
    cache = cfg.RosterCache()
    settings, _ = defaults()
    first = cache.get(settings, hermes_home)
    assert cache.get(settings, hermes_home) is first
    write(hermes_home / "claude-agents", "tester.md", "---\nname: tester\nmodel: haiku\n---\nTest.")
    second = cache.get(settings, hermes_home)
    assert second is not first and "tester" in second.agents
