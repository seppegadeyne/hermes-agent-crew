"""Agent files, settings and roster loading."""
import os
from pathlib import Path

import pytest

from heremes_codex_agents import agents_config as cfg

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


def test_builtin_roster_preserves_claude_and_adds_codex_roles(hermes_home):
    settings, problems = defaults()
    roster = cfg.load_roster(settings, hermes_home)
    assert problems == [] and roster.problems == []
    assert set(roster.agents) == {"reviewer", "explorer", "researcher", "builder",
                                  "codex-reviewer", "codex-builder", "codex-explorer", "codex-researcher"}
    assert (roster.agents["reviewer"].model, roster.agents["reviewer"].readonly) == ("opus", True)
    assert (roster.agents["explorer"].model, roster.agents["explorer"].readonly) == ("haiku", True)
    assert (roster.agents["researcher"].model, roster.agents["researcher"].readonly) == ("haiku", True)
    assert (roster.agents["builder"].model, roster.agents["builder"].readonly) == ("sonnet", False)
    assert all(roster.agents[n].provider == cfg.DEFAULT_PROVIDER
               for n in ("reviewer", "builder", "explorer", "researcher"))
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


def test_explicit_provider_inherit_survives_global_default(hermes_home):
    settings, _ = defaults({"overrides": {"reviewer": {"provider": "inherit"}}})
    roster = cfg.load_roster(settings, hermes_home)
    assert roster.agents["reviewer"].provider is None
    assert roster.agents["builder"].provider == "claude-subscription-directsdk-experimental"


def test_agent_file_provider_inherit_survives_global_default(hermes_home):
    write(hermes_home / "codex-agents", "local.md",
          "---\nname: local\nmodel: inherit\nprovider: inherit\n---\nFollow the lead.")
    settings, _ = defaults()
    roster = cfg.load_roster(settings, hermes_home)
    assert roster.agents["local"].provider is None
    assert roster.agents["local"].model is None


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
    write(hermes_home / "codex-agents", "README.md", "# My agents\n")
    settings, _ = defaults()
    assert cfg.load_roster(settings, hermes_home).problems == []


def test_user_folder_overrides_builtin_and_reports_bad_files(hermes_home):
    agents = hermes_home / "codex-agents"
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
    assert all(roster.agents[n].provider is None for n in ("reviewer", "explorer", "researcher"))


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
    write(hermes_home / "codex-agents", "tester.md", "---\nname: tester\nmodel: haiku\n---\nTest.")
    second = cache.get(settings, hermes_home)
    assert second is not first and "tester" in second.agents


def test_codex_defaults_and_builtin_efforts(hermes_home):
    settings, problems = defaults()
    assert not problems
    assert cfg.DEFAULT_PROVIDER == settings["provider"] == "claude-subscription-directsdk-experimental"
    assert settings["agent_dirs"] == ["{hermes_home}/claude-agents", "{hermes_home}/codex-agents"]
    roster = cfg.load_roster(settings, hermes_home)
    assert {name: (spec.model, spec.provider, spec.effort) for name, spec in roster.agents.items()
            if name.startswith("codex-")} == {
        "codex-reviewer": ("gpt-6-sol", "openai-codex", "high"),
        "codex-builder": ("gpt-6-sol", "openai-codex", "medium"),
        "codex-explorer": ("gpt-6-luna", "openai-codex", "low"),
        "codex-researcher": ("gpt-6-luna", "openai-codex", "low")}
    assert all(roster.agents[n].effort is None for n in ("reviewer", "builder", "explorer", "researcher"))


def test_none_reasoning_effort_is_preserved(hermes_home):
    spec, warnings = cfg.spec_from_markdown("---\nname: x\neffort: none\n---\nBody")
    assert spec.effort == "none" and not warnings
    settings, _ = defaults({"overrides": {"reviewer": {"effort": "none"}}})
    assert cfg.load_roster(settings, hermes_home).agents["reviewer"].effort == "none"


def test_codex_folder_overrides_claude_folder(hermes_home):
    write(hermes_home / "claude-agents", "custom.md", "---\nname: custom\nmodel: sonnet\n---\nClaude")
    write(hermes_home / "codex-agents", "custom.md", "---\nname: custom\nmodel: gpt-6-sol\nprovider: openai-codex\n---\nCodex")
    settings, _ = defaults()
    custom = cfg.load_roster(settings, hermes_home).agents["custom"]
    assert (custom.model, custom.provider, custom.instructions) == ("gpt-6-sol", "openai-codex", "Codex")
