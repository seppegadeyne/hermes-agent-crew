"""Launch, guard, wait, collect, limits: AgentsService against a fake host."""
import json
from types import SimpleNamespace

from conftest import DIRECT


def run(rig, tasks, **extra):
    return rig.service.run({"tasks": tasks, **extra}, session_id="lead-1")


def finish_on_sleep(rig, index=0, **result):
    def callback(now):
        if len(rig.lifecycle.requests) > index:
            rig.lifecycle.finish(f"sa-0-{index:08x}", **result)
    rig.clock.on_sleep.append(callback)


def test_parallel_swarm_returns_compact_reports(rig):
    def finish_all(now):
        for i in range(len(rig.lifecycle.requests)):
            rig.lifecycle.finish(f"sa-0-{i:08x}", summary=f"report {i}")
    rig.clock.on_sleep.append(finish_all)
    out = run(rig, [{"agent": "explorer", "goal": "find the router"},
                    {"agent": "researcher", "goal": "look up the API"},
                    {"agent": "reviewer", "goal": "check the plan", "context": "plan text"}])
    assert out["ok"] and "pending" not in out
    assert [r["status"] for r in out["results"]] == ["succeeded"] * 3
    assert [r["summary"] for r in out["results"]] == ["report 0", "report 1", "report 2"]
    assert [r.model for r in rig.lifecycle.requests] == ["haiku", "haiku", "opus"]
    assert all(r.role == "leaf" and r.correlation_id.startswith("ag_") for r in rig.lifecycle.requests)
    assert out["results"][2]["readonly"] is True and out["results"][2]["api_calls"] == 3


def test_agent_brief_travels_in_context(rig):
    finish_on_sleep(rig)
    run(rig, [{"agent": "reviewer", "goal": "review", "context": "the plan: step 1, step 2"}])
    context = rig.lifecycle.requests[0].context
    assert context.startswith("## Agent: reviewer")
    assert "senior engineer" in context and "You are read-only" in context
    assert "## Context from the lead\nthe plan: step 1, step 2" in context
    assert rig.lifecycle.requests[0].goal == "review"


def test_read_only_child_is_guarded_before_launch_returns(rig):
    run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    # The fake lifecycle asks the guard for write_file from inside launch(), like a fast child would.
    blocked = rig.lifecycle.checks_during_launch[0]
    assert blocked["action"] == "block" and "read-only" in blocked["message"]
    sid = "sa-0-00000000"
    assert rig.service.pre_tool_call(tool_name="read_file", task_id=sid) is None
    assert rig.service.pre_tool_call(tool_name="terminal", task_id=sid)["action"] == "block"
    assert rig.service.pre_tool_call(tool_name="agents_run", task_id=sid)["action"] == "block"


def test_policy_registered_even_without_the_start_hook(rig):
    rig.lifecycle.fire_start_hook = False
    run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    assert rig.service.pre_tool_call(tool_name="patch", task_id="sa-0-00000000")["action"] == "block"


def test_guard_ignores_everything_it_did_not_launch(rig):
    for kwargs in ({"tool_name": "write_file", "task_id": ""}, {"tool_name": "write_file", "task_id": "parent-task"},
                   {"tool_name": "terminal", "task_id": "sa-0-deadbeef"}, {}):
        assert rig.service.pre_tool_call(**kwargs) is None


def test_builder_may_write_but_not_delegate(rig):
    run(rig, [{"agent": "builder", "goal": "implement"}], wait_seconds=0)
    sid = "sa-0-00000000"
    assert rig.service.pre_tool_call(tool_name="patch", task_id=sid) is None
    assert rig.service.pre_tool_call(tool_name="terminal", task_id=sid) is None
    assert rig.service.pre_tool_call(tool_name="delegate_task", task_id=sid)["action"] == "block"
    assert rig.lifecycle.requests[0].model == "sonnet"
    assert not hasattr(rig.lifecycle.requests[0], "allowed_toolsets")


def test_read_only_child_schema_is_narrowed_to_read_toolsets(rig):
    run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    toolsets = set(rig.lifecycle.requests[0].allowed_toolsets)
    assert {"file", "web", "skills"} <= toolsets and "terminal" not in toolsets


def test_narrowing_respects_the_parent_toolsets(rig):
    rig.host.parent_agent.enabled_toolsets = ["file", "terminal"]
    run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    assert rig.lifecycle.requests[0].allowed_toolsets == ("file",)


def test_toolset_refusal_retries_without_narrowing(rig):
    calls = []
    original = rig.lifecycle.launch

    def launch(request):
        calls.append(request)
        if len(calls) == 1:
            raise ValueError("Unknown toolsets: skills.")
        return original(request)
    rig.lifecycle.launch = launch
    out = run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    assert len(calls) == 2 and not hasattr(calls[1], "allowed_toolsets")
    assert out["results"][0]["status"] == "running" and out["results"][0]["note"] == "toolsets_not_narrowed"


def test_wait_budget_stays_below_the_tool_deadline(rig):
    rig.host.deadline = 100.0
    out = run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=1800)
    assert rig.clock.now <= 75.0 + 0.5
    assert out["pending"] == [out["results"][0]["run_id"]] and "agents_status" in out["next"]


def test_no_deadline_means_the_requested_wait(rig):
    rig.host.deadline = None
    run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=30)
    assert 30.0 <= rig.clock.now <= 30.5


def test_status_collects_late_results_once(rig):
    out = run(rig, [{"agent": "reviewer", "goal": "long review"}], wait_seconds=0)
    run_id = out["results"][0]["run_id"]
    pending = rig.service.status({}, session_id="lead-1")
    assert pending["results"][0]["status"] == "running"
    rig.lifecycle.finish("sa-0-00000000", summary="BLOCKER at a.py:3")
    done = rig.service.status({"run_ids": [run_id]}, session_id="lead-1")
    assert done["results"][0]["summary"] == "BLOCKER at a.py:3"
    assert rig.service.status({}, session_id="lead-1")["note"].startswith("No unreported")


def test_status_waits(rig):
    run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    finish_on_sleep(rig)
    out = rig.service.status({"wait_seconds": 60}, session_id="lead-1")
    assert out["results"][0]["status"] == "succeeded"


def test_cancel(rig):
    out = run(rig, [{"agent": "builder", "goal": "x"}], wait_seconds=0)
    run_id = out["results"][0]["run_id"]
    assert rig.service.status({"cancel": True}, session_id="lead-1")["error"] == "run_ids_required"
    out = rig.service.status({"run_ids": [run_id], "cancel": True, "reason": "wrong plan"}, session_id="lead-1")
    assert rig.lifecycle.cancels == [("sa-0-00000000", "wrong plan")]
    assert out["results"][0]["cancel_requested"] is True


def test_runs_belong_to_their_conversation(rig):
    out = run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    run_id = out["results"][0]["run_id"]
    rig.host.parent_agent = SimpleNamespace(session_id="other", provider=DIRECT, model="sonnet",
                                            enabled_toolsets=None, _delegate_depth=0)
    other = rig.service.status({"run_ids": [run_id]}, session_id="other")
    assert other["unknown_run_ids"] == [run_id] and other["results"] == []


def test_runs_survive_a_compression_session_rotation(rig):
    out = run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    run_id = out["results"][0]["run_id"]
    rig.host.parent_agent.session_id = "lead-1-compressed"
    rig.lifecycle.finish("sa-0-00000000", summary="found it")
    out = rig.service.status({"run_ids": [run_id]}, session_id="lead-1-compressed")
    assert out["results"][0]["summary"] == "found it"


def test_limits(rig):
    rig.host.settings = {"max_parallel": 2}
    many = [{"agent": "explorer", "goal": str(i)} for i in range(3)]
    assert run(rig, many)["error"] == "too_many_tasks"
    rig.host.settings = {}
    two_writers = [{"agent": "builder", "goal": "a"}, {"agent": "builder", "goal": "b"}]
    assert run(rig, two_writers)["error"] == "too_many_writers"
    run(rig, [{"agent": "builder", "goal": "a"}], wait_seconds=0)
    assert run(rig, [{"agent": "builder", "goal": "b"}])["error"] == "too_many_writers"
    assert run(rig, [{"agent": "explorer", "goal": "read while the builder works"}], wait_seconds=0)["ok"]
    rig.host.settings = {"max_live_runs": 2}
    assert run(rig, [{"agent": "explorer", "goal": "one too many"}])["error"] == "too_many_live_runs"


def test_refusals(rig):
    assert run(rig, [{"agent": "ghost", "goal": "x"}])["error"] == "unknown_agent"
    assert run(rig, [{"agent": "explorer", "goal": " "}])["error"] == "invalid_goal"
    assert run(rig, [{"agent": "explorer", "goal": "x", "context": 3}])["error"] == "invalid_context"
    assert run(rig, [{"agent": "reviewer", "goal": "x", "diff_base": "--output=/x"}])["error"] == "invalid_diff_base"
    assert rig.service.run("nope")["error"] == "invalid_arguments"
    assert rig.service.run({"tasks": []})["error"] == "invalid_arguments"
    rig.host.paused = True
    assert run(rig, [{"agent": "explorer", "goal": "x"}])["error"] == "spawn_paused"
    rig.host.paused = False
    rig.host.parent_agent._delegate_depth = 1
    assert run(rig, [{"agent": "explorer", "goal": "x"}])["error"] == "nested_agent"
    rig.host.parent_agent = None
    assert run(rig, [{"agent": "explorer", "goal": "x"}])["error"] == "no_active_turn"
    rig.host.isolated = True
    assert run(rig, [{"agent": "explorer", "goal": "x"}])["error"] == "unsupported_isolation"
    assert rig.lifecycle.requests == []


def test_tasks_as_json_string_and_single_agent_shorthand(rig):
    finish_on_sleep(rig)
    out = rig.service.run({"tasks": json.dumps([{"agent": "explorer", "goal": "x"}])}, session_id="lead-1")
    assert out["ok"]
    out = rig.service.run({"agent": "researcher", "goal": "y", "wait_seconds": 0}, session_id="lead-1")
    assert out["ok"] and rig.lifecycle.requests[-1].model == "haiku"


def test_provider_mismatch_is_refused_on_builds_without_per_launch_provider(rig):
    rig.host.parent_agent.provider = "zai"
    out = run(rig, [{"agent": "explorer", "goal": "x"}])
    assert out["results"][0]["status"] == "failed"
    assert out["results"][0]["note"] == "provider_unsupported" and "#137167" in out["results"][0]["error"]
    assert rig.lifecycle.requests == []


def test_per_launch_provider_when_the_build_supports_it(rig):
    rig.host.fields = rig.host.fields | {"provider", "reasoning_effort"}
    rig.host.parent_agent.provider = "zai"
    rig.host.settings = {"overrides": {"explorer": {"effort": "low"}}}
    finish_on_sleep(rig)
    out = run(rig, [{"agent": "explorer", "goal": "x"}])
    request = rig.lifecycle.requests[0]
    assert (request.provider, request.model, request.reasoning_effort) == (DIRECT, "haiku", "low")
    assert out["results"][0]["status"] == "succeeded"


def test_inherited_model_on_another_provider_is_refused(rig):
    rig.host.fields = rig.host.fields | {"provider"}
    rig.host.parent_agent.provider = "zai"
    rig.host.settings = {"overrides": {"explorer": {"model": "inherit"}}}
    out = run(rig, [{"agent": "explorer", "goal": "x"}])
    assert out["results"][0]["note"] == "model_required" and rig.lifecycle.requests == []


def test_inherited_model_on_the_lead_provider_is_the_lead_model(rig):
    rig.host.fields = rig.host.fields | {"provider"}
    rig.host.parent_agent.model = "claude-sonnet-5-5"
    rig.host.settings = {"overrides": {"explorer": {"model": "inherit"}}}
    run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    request = rig.lifecycle.requests[0]
    assert (request.provider, request.model) == (DIRECT, "claude-sonnet-5-5")


def test_inherit_provider_runs_on_the_lead_route(rig):
    rig.host.settings = {"provider": "inherit"}
    rig.host.parent_agent.provider = "zai"
    run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    assert not hasattr(rig.lifecycle.requests[0], "provider") and rig.lifecycle.requests[0].model == "haiku"


def test_effort_is_reported_when_unsupported(rig):
    rig.host.settings = {"overrides": {"explorer": {"effort": "low"}}}
    out = run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    assert out["results"][0]["unsupported"] == ["effort"]


def test_launch_failure_is_reported(rig):
    rig.lifecycle.raise_on_launch = RuntimeError("provider exploded")
    out = run(rig, [{"agent": "explorer", "goal": "x"}])
    assert out["results"][0]["status"] == "failed" and "provider exploded" in out["results"][0]["error"]


def test_failed_child(rig):
    finish_on_sleep(rig, state="FAILED", summary=None, error="HTTP 400: not a valid model ID")
    out = run(rig, [{"agent": "reviewer", "goal": "x"}])
    assert out["results"][0]["status"] == "failed" and "not a valid model" in out["results"][0]["error"]


def test_unknown_handle_falls_back_to_the_stop_hook(rig):
    out = run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    rig.lifecycle.state["sa-0-00000000"] = "UNKNOWN"
    rig.service.on_subagent_stop(child_session_id="child-sa-0-00000000", child_summary="late report",
                                 child_status="completed", duration_ms=4200)
    out = rig.service.status({"run_ids": [out["results"][0]["run_id"]]}, session_id="lead-1")
    assert out["results"][0]["summary"] == "late report" and out["results"][0]["duration_seconds"] == 4.2


def test_attach_diff_puts_the_snapshot_in_the_child_context(rig, monkeypatch):
    from hermes_claude_agents import agents_git
    seen = {}

    def collect(cwd, base, budget):
        seen.update(cwd=cwd, base=base, budget=budget)
        return "### git diff\n+new line"
    monkeypatch.setattr(agents_git, "collect", collect)
    run(rig, [{"agent": "reviewer", "goal": "done check", "attach_diff": True, "diff_base": "main"}], wait_seconds=0)
    assert seen["base"] == "main" and 1000 <= seen["budget"] <= 20_000
    assert "## Working tree snapshot" in rig.lifecycle.requests[0].context
    assert "+new line" in rig.lifecycle.requests[0].context


def test_workdir_scopes_the_agent_and_the_diff(rig, monkeypatch, tmp_path):
    from hermes_claude_agents import agents_git
    repo = tmp_path / "repo"
    repo.mkdir()
    seen = {}
    monkeypatch.setattr(agents_git, "collect", lambda cwd, base, budget: seen.setdefault("cwd", cwd) and "+x")
    run(rig, [{"agent": "reviewer", "goal": "done check", "attach_diff": True, "workdir": str(repo)}],
        wait_seconds=0)
    assert seen["cwd"] == repo
    assert f"- Work in {repo}:" in rig.lifecycle.requests[0].context


def test_without_workdir_the_session_folder_is_used(rig, monkeypatch):
    from hermes_claude_agents import agents_git
    seen = {}
    monkeypatch.setattr(agents_git, "collect", lambda cwd, base, budget: seen.setdefault("cwd", cwd) and "+x")
    run(rig, [{"agent": "reviewer", "goal": "done check", "attach_diff": True}], wait_seconds=0)
    assert seen["cwd"] == rig.host.repo
    assert "- Work in" not in rig.lifecycle.requests[0].context


def test_invalid_workdir_is_refused(rig, tmp_path):
    for bad in ("relative/path", str(tmp_path / "missing"), 42):
        out = run(rig, [{"agent": "explorer", "goal": "x", "workdir": bad}])
        assert out["error"] == "invalid_workdir", bad
    assert rig.lifecycle.requests == []


def test_context_stays_under_the_lifecycle_cap(rig):
    run(rig, [{"agent": "reviewer", "goal": "x", "context": "y" * 100_000}], wait_seconds=0)
    assert len(rig.lifecycle.requests[0].context) <= 31_500


def test_report_budget_shrinks_with_many_results(rig):
    def finish_all(now):
        for i in range(len(rig.lifecycle.requests)):
            rig.lifecycle.finish(f"sa-0-{i:08x}", summary="z" * 50_000)
    rig.clock.on_sleep.append(finish_all)
    out = run(rig, [{"agent": "explorer", "goal": str(i)} for i in range(6)])
    assert len(json.dumps(out)) < 70_000


def test_prompt_section(rig):
    text = rig.service.prompt_section({})
    assert "agents_run" in text and "reviewer (opus, read-only)" in text and "explorer (haiku, read-only)" in text
    assert "tool_call" in text and len(text) <= 3_800
    rig.host.settings = {"prompt_section": False}
    assert rig.service.prompt_section({}) == ""
    rig.host.settings = {}
    rig.host.parent_agent._delegate_depth = 1
    assert rig.service.prompt_section({}) == ""


def test_prompt_section_keeps_the_trigger_and_the_fan_out_rule(rig):
    (rig.home / "claude-agents").mkdir()
    (rig.home / "claude-agents" / "lang-checker.md").write_text(
        "---\nname: lang-checker\nmodel: haiku\nreadonly: true\n"
        "use_when: a whole text file needs the language MCP checks\n"
        "description: " + "Mechanical language-check worker that reads a file. " * 6 + "\n---\nCheck.\n")
    text = rig.service.prompt_section({})
    line = next(x for x in text.splitlines() if x.startswith("- lang-checker"))
    assert "Use when a whole text file needs the language MCP checks." in line
    assert len(line) < 220 and "characters omitted" not in text
    assert "Mechanical fan-out" in text and "COMPLETE input" in text and len(text) <= 3_800
    assert "use when: a whole text file" in rig.service.roster_text()


def test_a_full_roster_keeps_every_trigger_and_the_usage_guide(rig):
    folder = rig.home / "claude-agents"
    folder.mkdir()
    for i in range(14):
        name = f"worker-{i:02d}-" + "x" * 22
        folder.joinpath(f"{name}.md").write_text(
            f"---\nname: {name}\nmodel: haiku\nreadonly: true\n"
            f"use_when: trigger {i:02d} " + "t" * 185 + "\n"
            "description: " + "d" * 300 + "\n---\nWork.\n")
    text = rig.service.prompt_section({})
    assert len(text) <= 3_800 and "characters omitted" not in text
    for marker in ("Call agents_run", "Mechanical fan-out", "wait come back as run_ids for agents_status."):
        assert marker in text
    shown = [line for line in text.splitlines() if line.startswith("- worker-")]
    assert shown and all("t" * 185 in line for line in shown)
    assert "more (/claude-agents lists them)" in text


def test_trigger_phrasing():
    from hermes_claude_agents.agents_config import AgentSpec
    from hermes_claude_agents.agents_service import roster_line
    assert roster_line(AgentSpec(name="a", use_when="when a file needs checks…")) == "Use when a file needs checks."
    assert roster_line(AgentSpec(name="a", use_when="Use for X.", description="D")) == "Use for X. D"


def test_roster_text_lists_agents_and_problems(rig):
    (rig.home / "claude-agents").mkdir()
    (rig.home / "claude-agents" / "bad.md").write_text("---\nname: BAD\n---\n")
    text = rig.service.roster_text()
    assert "reviewer (opus, claude-subscription-directsdk-experimental, read-only)" in text
    assert "problem:" in text and "bad.md" in text


def lead_result(rig, tool, args=None, turn="t1", session="lead-1", result='{"content": "x"}'):
    return rig.service.on_transform_tool_result(tool_name=tool, args=args or {}, result=result, task_id=session,
                                                session_id=session, turn_id=turn)


def test_nudge_fires_once_after_a_run_of_manual_scouting(rig):
    for _ in range(7):
        assert lead_result(rig, "read_file") is None
    assert lead_result(rig, "write_file") is None  # edits do not count
    hinted = lead_result(rig, "terminal", {"command": "cd /repo && grep -n foo src/*.py"})
    assert hinted.startswith('{"content": "x"}\n\n[hermes-claude-agents] 8 read/search calls')
    assert "explorer" in hinted and "agents_run" in hinted and "builder" not in hinted
    assert lead_result(rig, "read_file") is None  # once per turn
    for _ in range(7):
        assert lead_result(rig, "search_files", turn="t2") is None
    assert lead_result(rig, "web_search", turn="t2") is not None  # a new turn counts afresh


def test_no_nudge_once_the_turn_delegates_or_for_other_commands(rig):
    lead_result(rig, "agents_run")
    for _ in range(12):
        assert lead_result(rig, "read_file") is None
    for _ in range(12):
        assert lead_result(rig, "terminal", {"command": "python -m pytest -q"}, turn="t2") is None


def test_no_nudge_for_children_or_when_switched_off(rig):
    run(rig, [{"agent": "explorer", "goal": "map it"}], wait_seconds=0)
    for _ in range(12):
        assert lead_result(rig, "read_file", session="sa-0-00000000") is None  # our own child
    rig.host.parent_agent._delegate_depth = 1
    for _ in range(12):
        assert lead_result(rig, "read_file", session="other-child") is None
    rig.host.parent_agent._delegate_depth = 0
    rig.host.settings = {"nudge_after": 0}
    for _ in range(12):
        assert lead_result(rig, "read_file", session="lead-2") is None


def test_nudge_never_raises_and_ignores_odd_input(rig):
    assert rig.service.on_transform_tool_result() is None
    assert lead_result(rig, "read_file", result=None) is None
    rig.host.parent = None  # a broken host must not break the tool result
    assert lead_result(rig, "read_file") is None


def test_schema_allows_what_the_settings_allow(plugin):
    from hermes_claude_agents.agents_config import INT_RANGES
    from hermes_claude_agents.agents_tools import schema
    assert schema("agents_run")["parameters"]["properties"]["tasks"]["maxItems"] == INT_RANGES["max_parallel"][1]


def test_nudge_leaves_script_and_error_results_alone(rig):
    # execute_code scripts call tools without a session or turn id and parse every result as JSON.
    for _ in range(12):
        assert rig.service.on_transform_tool_result(tool_name="read_file", args={}, result='{"content": "x"}',
                                                    task_id="lead-1", session_id="", turn_id="") is None
        assert rig.service.on_transform_tool_result(tool_name="read_file", args={}, result='{"content": "x"}',
                                                    task_id="lead-1", session_id="lead-1", turn_id="") is None
    for _ in range(7):
        lead_result(rig, "read_file")
    assert lead_result(rig, "read_file", result='{"error": "no such file"}') is None
    assert lead_result(rig, "read_file") is not None  # the hint waits for a usable result


def test_scouting_commands():
    from hermes_claude_agents.agents_service import _is_scouting
    for command in ("grep -rn foo src 2>/dev/null", "cd /r && git log --oneline -5 2>&1", "sed -n 1,40p a.py",
                    "find . -name '*.py' | head", "ls -la; wc -l *.py"):
        assert _is_scouting("terminal", {"command": command}), command
    for command in ("cat > notes.txt", "find . -name x -delete", "sed -n 1p -i a.py", "grep -l x | xargs rm",
                    "ls; rm x", "python -m pytest -q", "git commit -m x", "echo hi"):
        assert not _is_scouting("terminal", {"command": command}), command
