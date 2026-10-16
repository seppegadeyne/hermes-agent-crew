"""Agent files, settings and roster loading."""
import os
from pathlib import Path

import pytest

from hermes_agent_crew import agents_config as cfg

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


DIRECT = cfg.DEFAULT_PROVIDER


def test_builtin_agents_use_tiers_and_default_to_todays_models(hermes_home):
    settings, problems = defaults()
    assert not problems
    roster = cfg.load_roster(settings, hermes_home)
    routes = {name: (spec.tier, spec.model, spec.provider) for name, spec in roster.agents.items()}
    assert routes == {"reviewer": ("strong", "opus", DIRECT), "builder": ("balanced", "sonnet", DIRECT),
                      "explorer": ("fast", "haiku", DIRECT), "researcher": ("fast", "haiku", DIRECT)}


def test_default_tiers_keep_provider_inherit(hermes_home):
    settings, _ = defaults({"provider": "inherit"})
    roster = cfg.load_roster(settings, hermes_home)
    assert roster.agents["explorer"].model == "haiku" and roster.agents["explorer"].provider is None


def test_a_tier_can_move_to_another_provider(hermes_home):
    settings, problems = defaults({"tiers": {"fast": {"provider": "strata-qwen", "model": "strata"}}})
    assert not problems
    roster = cfg.load_roster(settings, hermes_home)
    assert (roster.agents["explorer"].model, roster.agents["explorer"].provider) == ("strata", "strata-qwen")
    assert (roster.agents["reviewer"].model, roster.agents["reviewer"].provider) == ("opus", DIRECT)


def test_tier_fields_resolve_one_at_a_time(hermes_home):
    write(hermes_home / "claude-agents", "local.md",
          "---\nname: local\ntier: fast\nprovider: my-local\nreadonly: true\n---\nScout.")
    settings, _ = defaults({"overrides": {"explorer": {"model": "haiku-next"}}})
    roster = cfg.load_roster(settings, hermes_home)
    assert (roster.agents["local"].model, roster.agents["local"].provider) == ("haiku", "my-local")
    assert (roster.agents["explorer"].model, roster.agents["explorer"].provider) == ("haiku-next", DIRECT)


def test_a_file_model_ignores_its_tier(hermes_home):
    write(hermes_home / "claude-agents", "pinned.md", "---\nname: pinned\ntier: strong\nmodel: mine\n---\nX.")
    settings, _ = defaults({"tiers": {"strong": {"model": "other"}}})
    assert cfg.load_roster(settings, hermes_home).agents["pinned"].model == "mine"


def test_bad_tiers_are_problems_not_crashes(hermes_home):
    write(hermes_home / "claude-agents", "odd.md", "---\nname: odd\ntier: huge\n---\nX.")
    settings, problems = defaults({"tiers": {"fast": {"provider": "x"}, "giant": {"model": "m"},
                                             "strong": "opus", "balanced": {"model": "s", "url": "u"}}})
    assert settings["tiers"] == {"strong": {}, "balanced": {}, "fast": {}}
    assert len(problems) == 4 and any("needs a model" in p for p in problems)
    roster = cfg.load_roster(settings, hermes_home)
    assert "odd" not in roster.agents and any("odd.md" in p and "tier" in p for p in roster.problems)
    # An invalid mapping never falls back to the default (possibly paid) route: its agents are left out.
    assert set(roster.agents) == set()
    assert any("explorer: left out" in p and "tiers.fast" in p for p in roster.problems)
    _, problems = defaults({"tiers": ["fast"]})
    assert any("settings.tiers must be a mapping" in p for p in problems)


def test_roster_cache_reloads_when_tiers_change(hermes_home):
    cache = cfg.RosterCache()
    settings, _ = defaults()
    first = cache.get(settings, hermes_home)
    settings, _ = defaults({"tiers": {"fast": {"model": "strata", "provider": "strata-qwen"}}})
    second = cache.get(settings, hermes_home)
    assert second is not first and second.agents["explorer"].model == "strata"


@pytest.mark.parametrize("key", ["model", "provider"])
def test_tier_with_explicit_inherit_is_a_problem(hermes_home, key):
    write(hermes_home / "claude-agents", "mixed.md", f"---\nname: mixed\ntier: fast\n{key}: inherit\n---\nX.")
    roster = cfg.load_roster(defaults()[0], hermes_home)
    assert "mixed" not in roster.agents
    assert any("mixed.md" in p and f"{key}: inherit" in p for p in roster.problems)


def test_override_model_inherit_beats_the_tier(hermes_home):
    roster = cfg.load_roster(defaults({"overrides": {"explorer": {"model": "inherit"}}})[0], hermes_home)
    assert roster.agents["explorer"].model is None  # the lead's model


def test_a_tier_can_list_fallback_routes(hermes_home):
    settings, problems = defaults({"tiers": {"fast": [{"provider": "strata-qwen", "model": "strata"},
                                                      {"model": "haiku"}]}})
    assert not problems
    roster = cfg.load_roster(settings, hermes_home)
    explorer = roster.agents["explorer"]
    assert (explorer.provider, explorer.model) == ("strata-qwen", "strata")
    assert explorer.routes == (("strata-qwen", "strata"), (DIRECT, "haiku"))  # route 2 takes the plugin provider
    pinned = cfg.load_roster(defaults({"tiers": settings["tiers"],
                                       "overrides": {"explorer": {"model": "haiku"}}})[0], hermes_home)
    explorer = pinned.agents["explorer"]  # an override pins one route, from the route that agrees with it
    assert (explorer.provider, explorer.model, explorer.routes) == (DIRECT, "haiku", ())
    local = cfg.load_roster(defaults({"tiers": settings["tiers"],
                                      "overrides": {"explorer": {"provider": "strata-qwen"}}})[0], hermes_home)
    assert (local.agents["explorer"].provider, local.agents["explorer"].model) == ("strata-qwen", "strata")


def test_a_file_pinning_one_field_never_mixes_routes(hermes_home):
    write(hermes_home / "claude-agents", "scout.md", "---\nname: scout\ntier: fast\nmodel: haiku\n---\nX.")
    write(hermes_home / "claude-agents", "local.md", "---\nname: local\ntier: fast\nprovider: my-gpu\n---\nX.")
    settings, _ = defaults({"tiers": {"fast": [{"provider": "strata-qwen", "model": "strata"}, {"model": "haiku"}]}})
    roster = cfg.load_roster(settings, hermes_home)
    assert (roster.agents["scout"].provider, roster.agents["scout"].model) == (DIRECT, "haiku")
    assert (roster.agents["local"].provider, roster.agents["local"].model) == ("my-gpu", None)


def test_bad_route_lists_are_problems(hermes_home):
    for bad in ([], [{"model": "a"}] * 5, [{"model": "a"}, {"provider": "x"}]):
        settings, problems = defaults({"tiers": {"fast": bad}})
        assert settings["tiers"]["fast"] == {} and problems
