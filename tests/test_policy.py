"""The per-child tool policy."""
from hermes_agent_crew.agents_config import spec_from_markdown
from hermes_agent_crew.agents_policy import ChildPolicy, PolicyRegistry


def policy_for(text):
    spec, _ = spec_from_markdown(text)
    return ChildPolicy.for_spec(spec)


def test_read_only_policy_allows_reads_and_blocks_everything_else():
    policy = policy_for("---\nname: r\nreadonly: true\n---\n")
    for tool in ("read_file", "search_files", "web_search", "web_extract", "skill_view", "tool_search"):
        assert policy.decide(tool) is None, tool
    for tool in ("write_file", "patch", "terminal", "process_manage", "execute_code", "browser_click", "memory",
                 "cronjob_manage", "skill_manage", "mcp__storyblok__update_story"):
        message = policy.decide(tool)
        assert message and "read-only" in message and tool in message, tool
    # Hermes unwraps a valid tool_call to the real tool before the guard runs; only a broken one lands here.
    message = policy.decide("tool_call")
    assert message and "did not resolve" in message and "tool_describe" in message


def test_tool_list_agent_can_reach_its_deferred_mcp_tools():
    # With tool search on, MCP tools are deferred: an agent limited to them needs the catalog reads.
    policy = policy_for("---\nname: l\nreadonly: true\ntools: [mcp__language__check_dutch_text]\n---\n")
    assert policy.decide("tool_search") is None and policy.decide("tool_describe") is None
    assert policy.decide("mcp__language__check_dutch_text") is None
    assert policy.decide("read_file") and policy.decide("mcp__language__other")
    writer = policy_for("---\nname: w\nmodel: sonnet\ntools: [write_file]\n---\n")
    assert writer.decide("tool_describe") is None and writer.decide("terminal")
    assert "cannot start other agents" in writer.decide("agents_run")


def test_agents_never_start_agents():
    for text in ("---\nname: r\nreadonly: true\n---\n", "---\nname: b\nmodel: sonnet\n---\n"):
        policy = policy_for(text)
        for tool in ("agents_run", "agents_status", "delegate_task"):
            assert "cannot start other agents" in policy.decide(tool)


def test_unrestricted_agent_may_use_every_other_tool():
    policy = policy_for("---\nname: b\nmodel: sonnet\n---\n")
    for tool in ("write_file", "patch", "terminal", "execute_code"):
        assert policy.decide(tool) is None


def test_tool_list_with_mcp_globs():
    policy = policy_for("---\nname: s\nreadonly: true\ntools: [read_file, 'mcp__storyblok__search*']\n---\n")
    assert policy.decide("mcp__storyblok__search") is None
    assert policy.decide("mcp__storyblok__search_stories") is None
    assert policy.decide("mcp__storyblok__update_story")


def test_registry_is_bounded():
    registry, policy = PolicyRegistry(max_entries=2), ChildPolicy("a", True, frozenset())
    for key in ("1", "2", "3"):
        registry.register(key, policy)
    assert registry.get("1") is None and registry.get("3") is policy and len(registry) == 2
