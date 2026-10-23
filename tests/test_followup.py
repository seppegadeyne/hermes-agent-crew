"""Concrete scouting nudge, follow_up re-reviews and the call shape in the lead's prompt."""
import json
import subprocess

import pytest
from test_service import finish_on_sleep, lead_result, run


@pytest.fixture(autouse=True)
def no_outer_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def f():\n    return 1\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def review(rig, repo, **task):
    index = len(rig.lifecycle.requests)
    finish_on_sleep(rig, index, summary=task.pop("summary", "SHOULD-FIX: app.py:2 returns a constant"))
    out = run(rig, [{"agent": "reviewer", "goal": "review app", "workdir": str(repo), **task}])
    return out, rig.lifecycle.requests[index].context


# ------------------------------------------------------------------ follow_up


def test_follow_up_sends_only_the_changes_and_the_earlier_report(rig, tmp_path):
    repo = make_repo(tmp_path)
    first, context = review(rig, repo)
    assert "Earlier report" not in context and "Changes since" not in context
    run_id = first["results"][0]["run_id"]
    (repo / "app.py").write_text("def f():\n    return compute()\n")
    git(repo, "commit", "-qam", "fix")
    (repo / "extra.py").write_text("x = 1\n")
    git(repo, "add", "extra.py")  # staged, not committed: still part of the follow-up diff
    _, context = review(rig, repo, follow_up=run_id, summary="fixed")
    assert f"## Earlier report (run {run_id})" in context and "returns a constant" in context
    assert "## Changes since the earlier run" in context
    assert "+    return compute()" in context and "extra.py" in context
    assert "check whether each earlier finding was fixed" in context


def test_follow_up_after_a_rebase_reviews_in_full(rig, tmp_path):
    repo = make_repo(tmp_path)
    first, _ = review(rig, repo)
    git(repo, "commit", "-q", "--amend", "-m", "rewritten")  # the old HEAD is no longer an ancestor
    _, context = review(rig, repo, follow_up=first["results"][0]["run_id"])
    assert "not an ancestor of HEAD" in context and "review in full" in context
    assert "## Changes since the earlier run" not in context
    assert "returns a constant" in context  # the earlier findings still come along


def test_follow_up_refusals(rig, tmp_path):
    repo = make_repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    first, _ = review(rig, repo)
    run_id = first["results"][0]["run_id"]
    cases = [
        ({"agent": "reviewer", "goal": "g", "workdir": str(repo), "follow_up": "nope"}, "invalid_follow_up"),
        ({"agent": "reviewer", "goal": "g", "workdir": str(repo), "follow_up": "ag_0123456789"}, "unknown_follow_up"),
        ({"agent": "reviewer", "goal": "g", "workdir": str(other), "follow_up": run_id}, "invalid_follow_up"),
        ({"agent": "builder", "goal": "g", "workdir": str(repo), "follow_up": run_id}, "invalid_follow_up"),
        ({"agent": "reviewer", "goal": "g", "workdir": str(repo), "follow_up": run_id, "diff_base": "main"},
         "invalid_follow_up"),
    ]
    for task, code in cases:
        out = run(rig, [task])
        assert out["ok"] is False and out["error"] == code, (task, out)
    # Another conversation (other session id, other agent object) cannot build on this run.
    from types import SimpleNamespace
    mine = rig.host.parent_agent
    rig.host.parent_agent = SimpleNamespace(**{**vars(mine), "session_id": "lead-2"})
    out = rig.service.run({"tasks": [{"agent": "reviewer", "goal": "g", "workdir": str(repo),
                                      "follow_up": run_id}]}, session_id="lead-2")
    rig.host.parent_agent = mine
    assert out["error"] == "unknown_follow_up"


def test_failed_run_cannot_be_followed_up(rig, tmp_path):
    repo = make_repo(tmp_path)
    finish_on_sleep(rig, 0, state="FAILED", summary=None, error="boom")
    first = run(rig, [{"agent": "reviewer", "goal": "review", "workdir": str(repo)}])
    out = run(rig, [{"agent": "reviewer", "goal": "again", "workdir": str(repo),
                     "follow_up": first["results"][0]["run_id"]}])
    assert out["error"] == "invalid_follow_up"


def test_plan_stages_do_not_accept_follow_up(plugin):
    from hermes_agent_crew import agents_tools
    stage = agents_tools.schema("agents_run")["parameters"]["properties"]["plan"]["properties"]["stages"]["items"]
    assert "follow_up" not in stage["properties"]
    task = agents_tools.schema("agents_run")["parameters"]["properties"]["tasks"]["items"]
    assert "follow_up" in task["properties"]


# ------------------------------------------------------------------ concrete nudge


def test_nudge_gives_the_count_and_a_ready_call(rig, tmp_path):
    rig.host.settings = {"nudge_min_chars": 0}
    for n in range(7):
        lead_result(rig, "read_file", {"path": f"/srv/app/src/m{n}.py"})
    hinted = lead_result(rig, "terminal", {"command": "cd /srv/app && grep -rn token src"})
    hint = hinted.split("\n\n", 1)[1]
    assert "8 separate reads/searches" in hint and "m6.py" not in hint  # numbers, not a list of lookups
    line = next(x for x in hint.splitlines() if x.strip().startswith("tool_call "))
    call = json.loads(line.strip()[len("tool_call "):])
    task = call["calls"][0]["arguments"]["tasks"][0]
    assert call["calls"][0]["name"] == "agents_run"
    assert task["agent"] == "explorer" and task["workdir"] == "/srv/app"


def test_nudge_picks_the_researcher_for_web_lookups_and_the_direct_shape(rig):
    rig.host.settings = {"nudge_min_chars": 0}
    rig.host.deferred = False
    for n in range(8):
        hinted = lead_result(rig, "web_search", {"query": f"api limits {n}"})
    hint = hinted.split("\n\n", 1)[1]
    line = next(x for x in hint.splitlines() if x.strip().startswith("agents_run "))
    task = json.loads(line.strip()[len("agents_run "):])["tasks"][0]
    assert task["agent"] == "researcher" and "workdir" not in task


def test_nudge_only_names_agents_in_the_roster(rig):
    from hermes_agent_crew.agents_service import nudge_text
    text = nudge_text(8, ["checker"], [("web_search", {"query": "x"})], deferred=True)
    assert '"agent": "checker"' in text and "researcher" not in text and "explorer" not in text


def test_nudge_workdir_skips_root_and_home():
    import os

    from hermes_agent_crew.agents_service import nudge_workdir
    assert nudge_workdir([("read_file", {"path": "/etc/hosts"}), ("read_file", {"path": "/srv/x.py"})]) is None
    home = os.path.expanduser("~")
    assert nudge_workdir([("read_file", {"path": f"{home}/a.md"}), ("read_file", {"path": f"{home}/b/c.md"})]) is None
    assert nudge_workdir([("read_file", {"path": "/srv/app/a.py"})]) == "/srv/app"
    assert nudge_workdir([("web_search", {"query": "x"})]) is None


# ------------------------------------------------------------------ prompt call shape


def test_prompt_shows_the_tool_call_shape_when_agents_run_is_deferred(rig):
    text = rig.service.prompt_section({})
    assert 'Call agents_run through tool_call, no search or describe needed: {"calls": [{"name": "agents_run"' in text
    rig.host.deferred = False
    text = rig.service.prompt_section({})
    assert 'Call agents_run with {"tasks"' in text and "through tool_call" not in text
    assert "follow_up" in text


def test_tool_description_leads_with_the_trigger(rig):
    from hermes_agent_crew.agents_service import tool_description
    _, _, roster = rig.service.config()
    assert tool_description(roster).startswith("Delegate lookups, reviews and builds to subagents.")


def test_follow_up_of_a_follow_up_keeps_open_findings_detailed(rig, tmp_path):
    repo = make_repo(tmp_path)
    first, _ = review(rig, repo)
    (repo / "app.py").write_text("def f():\n    return 2\n")
    git(repo, "commit", "-qam", "tweak")
    second, context = review(rig, repo, follow_up=first["results"][0]["run_id"],
                             summary="still open: constant return, app.py:2, value is hard-coded")
    assert "title, file:line" in context
    (repo / "app.py").write_text("def f():\n    return compute()\n")
    git(repo, "commit", "-qam", "fix")
    _, context = review(rig, repo, follow_up=second["results"][0]["run_id"])
    assert "app.py:2, value is hard-coded" in context and "+    return compute()" in context
    assert "return 2" in context  # the diff starts where the second review started


def test_follow_up_of_a_run_nobody_collected(rig, tmp_path):
    repo = make_repo(tmp_path)
    out = run(rig, [{"agent": "reviewer", "goal": "review", "workdir": str(repo)}], wait_seconds=0)
    assert out["results"][0]["status"] == "running"
    rig.lifecycle.finish("sa-0-00000000", summary="SHOULD-FIX: app.py:2")
    finish_on_sleep(rig, 1, summary="ok")
    again = run(rig, [{"agent": "reviewer", "goal": "again", "workdir": str(repo),
                       "follow_up": out["results"][0]["run_id"]}])
    assert again["ok"] and "SHOULD-FIX: app.py:2" in rig.lifecycle.requests[1].context


def test_nudge_workdir_keeps_dotted_folders():
    from hermes_agent_crew.agents_service import nudge_workdir
    reads = [("read_file", {"path": "/srv/app.v2/a.py"}), ("read_file", {"path": "/srv/app.v2/b.py"})]
    assert nudge_workdir(reads) == "/srv/app.v2"
    assert nudge_workdir([("search_files", {"path": "/srv/app.v2", "pattern": "x"})]) == "/srv/app.v2"


def test_failed_lookups_do_not_pick_the_workdir(rig):
    rig.host.settings = {"nudge_min_chars": 0}
    for n in range(7):
        lead_result(rig, "read_file", {"path": f"/srv/app/m{n}.py"})
    hinted = lead_result(rig, "read_file", {"path": "/nope/gone.py"}, result='{"error": "no such file"}')
    assert hinted is None  # an error result never carries the hint
    hinted = lead_result(rig, "read_file", {"path": "/srv/app/m9.py"})
    assert '"workdir": "/srv/app"' in hinted
    assert "/nope" not in hinted
    assert "9 separate reads/searches" in hinted  # the failed lookup still counted
