"""Durable run ledger: runs survive a restart, crashed runs show as lost, the wait cap is reported."""
import json
import os
import subprocess

import pytest
from test_service import finish_on_sleep, run


@pytest.fixture(autouse=True)
def no_outer_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def status(rig, **args):
    return rig.service.status(args, session_id="lead-1")


def ledger_lines(rig):
    path = rig.host.data_dir() / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def restart(rig):
    """A new service on the same host and profile: what a Hermes restart looks like to the plugin."""
    from conftest import FakeLifecycle

    from hermes_agent_crew.agents_service import AgentsService

    holder = {}
    rig.host.lifecycle = FakeLifecycle(lambda: holder["service"])
    service = AgentsService(rig.host, clock=rig.clock.monotonic, wall=rig.clock.wall, sleep=rig.clock.sleep)
    holder["service"] = service
    rig.service, rig.lifecycle = service, rig.host.lifecycle
    return service


def make_repo(tmp_path, branch="feat/x"):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", branch], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                        *args], cwd=repo, check=True, capture_output=True)
    return repo


def test_launch_and_finish_are_recorded_with_folder_and_branch(rig, tmp_path):
    repo = make_repo(tmp_path)
    finish_on_sleep(rig, summary="added the importer")
    out = run(rig, [{"agent": "builder", "goal": "build the importer", "workdir": str(repo),
                     "write_scope": ["src"]}])
    run_id = out["results"][0]["run_id"]
    lines = ledger_lines(rig)
    assert [line["status"] for line in lines] == ["running", "succeeded"]
    last = lines[-1]
    assert last["run_id"] == run_id and last["workdir"] == str(repo.resolve()) and last["branch"] == "feat/x"
    assert last["summary"] == "added the importer" and last["write_scope"] and last["pid"] == os.getpid()
    assert last["owner_session_id"] == "lead-1" and last["finished_at"]


def test_after_a_restart_status_returns_the_report_from_the_ledger(rig):
    finish_on_sleep(rig, summary="REPORT-XYZ")
    run_id = run(rig, [{"agent": "explorer", "goal": "map it"}])["results"][0]["run_id"]
    restart(rig)
    out = status(rig, run_ids=[run_id])
    assert "unknown_run_ids" not in out
    (item,) = out["results"]
    assert item["source"] == "ledger" and item["status"] == "succeeded" and item["summary"] == "REPORT-XYZ"


def test_a_run_whose_process_died_is_listed_as_lost(rig, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, branch="feat/contracts")
    run(rig, [{"agent": "builder", "goal": "import the contracts", "workdir": str(repo)}], wait_seconds=0)
    path = rig.host.data_dir() / "runs.jsonl"
    lines = path.read_text().splitlines()
    dead = json.loads(lines[-1])
    dead["pid"] = 2_147_483_000  # a process that does not exist
    path.write_text(json.dumps(dead) + "\n")
    restart(rig)
    out = status(rig, recent=5)
    assert out["results"] == [] and "note" not in out
    (row,) = out["recent"]
    assert row["status"] == "lost" and row["branch"] == "feat/contracts" and row["workdir"] == str(repo.resolve())
    assert "ledger" in out["next"]
    detail = status(rig, run_ids=[row["run_id"]])["results"][0]
    assert detail["status"] == "lost" and "workdir/branch" in detail["error"]


def test_recent_does_not_report_or_wait_on_live_runs(rig):
    run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=0)
    out = status(rig, recent=3, wait_seconds=60)
    assert out["results"] == [] and rig.clock.now == 0
    assert out["recent"][0]["status"] == "running"
    assert status(rig)["results"][0]["status"] == "running"  # still unreported for the normal call


def test_recent_is_validated(rig):
    run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)
    for bad in (0, 21, True, "5"):
        assert status(rig, recent=bad)["error"] == "invalid_arguments"


def test_follow_up_works_from_the_ledger_after_a_restart(rig, tmp_path):
    repo = make_repo(tmp_path)
    finish_on_sleep(rig, summary="SHOULD-FIX: app.py:2")
    first = run(rig, [{"agent": "reviewer", "goal": "review", "workdir": str(repo)}])["results"][0]["run_id"]
    restart(rig)
    finish_on_sleep(rig, summary="fixed")
    out = run(rig, [{"agent": "reviewer", "goal": "re-review", "workdir": str(repo), "follow_up": first}])
    assert out["results"][0]["status"] == "succeeded"
    context = rig.lifecycle.requests[0].context
    assert f"## Earlier report (run {first})" in context and "SHOULD-FIX: app.py:2" in context


def test_ledger_is_compacted_and_survives_a_torn_line(rig):
    from hermes_agent_crew import agents_ledger

    path = rig.host.data_dir() / "runs.jsonl"
    path.parent.mkdir(parents=True)
    records = [{"run_id": f"ag_{i:010x}", "status": "succeeded", "agent": "explorer", "started_at": i}
               for i in range(agents_ledger.MAX_RECORDS + 50)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records * 2) + '{"run_id": "ag_torn')
    finish_on_sleep(rig)
    run(rig, [{"agent": "explorer", "goal": "x"}])
    ids = [line["run_id"] for line in ledger_lines(rig)]
    # Compacted to the newest MAX_RECORDS (duplicates and the torn line gone), then this run's two snapshots.
    assert len(ids) == agents_ledger.MAX_RECORDS + 2 and len(set(ids)) == agents_ledger.MAX_RECORDS + 1
    assert records[0]["run_id"] not in ids and records[-1]["run_id"] in ids
    assert len(rig.service.ledger.load()) == agents_ledger.MAX_RECORDS + 1


def test_ledger_errors_never_break_a_run(rig):
    blocker = rig.host.data_dir()
    blocker.parent.mkdir(parents=True)
    blocker.write_text("a file where the folder should be")
    finish_on_sleep(rig, summary="ok")
    out = run(rig, [{"agent": "explorer", "goal": "x"}])
    assert out["results"][0]["status"] == "succeeded"
    assert status(rig, recent=2)["recent"] == []


def test_same_pid_from_an_earlier_process_is_lost(rig):
    """Containers reuse low pids (Hermes as pid 1): an equal pid alone must not keep a dead run alive."""
    run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=0)
    path = rig.host.data_dir() / "runs.jsonl"
    item = json.loads(path.read_text().splitlines()[-1])
    assert item["pid"] == os.getpid()
    item["proc"] = "an-earlier-process"
    path.write_text(json.dumps(item) + "\n")
    assert rig.service.ledger.get(item["run_id"])["status"] == "lost"


def test_reused_pid_with_another_start_time_is_lost(rig, monkeypatch):
    from hermes_agent_crew import agents_ledger

    child = subprocess.Popen(["sleep", "30"])  # a live process that is not this one (getppid is 0 under pid 1)
    try:
        monkeypatch.setattr(agents_ledger, "_start_time", lambda pid: "999")
        alive = {"run_id": "ag_0000000001", "status": "running", "pid": child.pid, "pid_start": "999"}
        reused = {**alive, "run_id": "ag_0000000002", "pid_start": "123"}
        assert agents_ledger._alive(alive) is True and agents_ledger._alive(reused) is False
        assert agents_ledger._alive({"pid": True}) is False
    finally:
        child.kill()
        child.wait()


def test_a_finished_run_nobody_collected_is_not_lost_after_a_crash(rig):
    run_id = run(rig, [{"agent": "explorer", "goal": "x"}], wait_seconds=0)["results"][0]["run_id"]
    rig.service.on_subagent_stop(child_session_id="child-sa-0-00000000", child_summary="REPORT", child_status="completed")
    path = rig.host.data_dir() / "runs.jsonl"
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["run_id"] == run_id and last["status"] == "succeeded" and last["summary"] == "REPORT"
    restart(rig)
    assert status(rig, recent=1)["recent"][0]["status"] == "succeeded"


def test_compaction_repeats_in_a_long_running_process(rig, monkeypatch):
    from hermes_agent_crew import agents_ledger

    monkeypatch.setattr(agents_ledger, "MAX_RECORDS", 3)
    monkeypatch.setattr(agents_ledger, "COMPACT_FACTOR", 2)
    rig.clock.on_sleep.append(lambda now: [rig.lifecycle.finish(f"sa-0-{i:08x}")
                                           for i in range(len(rig.lifecycle.requests))])
    for n in range(12):
        run(rig, [{"agent": "explorer", "goal": f"x{n}"}])
    # 24 snapshots were written. Compaction every MAX_RECORDS * COMPACT_FACTOR appends keeps the file at
    # most MAX_RECORDS + that many lines; without the repeat it would hold all 24.
    lines = ledger_lines(rig)
    assert len(lines) <= 3 + 3 * 2
    assert [line["goal"] for line in lines][-1] == "x11"


def test_cancel_does_not_answer_from_the_ledger(rig):
    finish_on_sleep(rig, summary="done")
    run_id = run(rig, [{"agent": "explorer", "goal": "x"}])["results"][0]["run_id"]
    restart(rig)
    out = status(rig, run_ids=[run_id], cancel=True, recent=2)
    assert out["unknown_run_ids"] == [run_id] and out["results"] == []
    assert out["recent"][0]["run_id"] == run_id


def test_run_ledger_can_be_switched_off(rig):
    rig.host.settings = {"run_ledger": False}
    finish_on_sleep(rig)
    run(rig, [{"agent": "explorer", "goal": "x"}])
    assert not (rig.host.data_dir() / "runs.jsonl").exists()
    assert status(rig, recent=3)["recent"] == []


def test_plan_runs_record_their_branch(rig, tmp_path):
    repo = make_repo(tmp_path, branch="feat/plan")
    rig.clock.on_sleep.append(lambda now: [rig.lifecycle.finish(f"sa-0-{i:08x}", summary="ok")
                                           for i in range(len(rig.lifecycle.requests))])
    rig.service.run({"plan": {"workdir": str(repo), "stages": [
        {"name": "scan", "agent": "explorer", "goal": "scan", "split": ["a", "b"]}]}}, session_id="lead-1")
    lines = ledger_lines(rig)
    assert lines and all(line.get("branch") == "feat/plan" and line.get("plan_id") for line in lines)


def test_wait_note_when_the_tool_deadline_cut_the_wait(rig):
    rig.host.deadline = 420.0
    out = run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=1200)
    assert out["pending"] and "Waited 395 s, not 1200" in out["wait_note"]
    assert "timeouts.tools.sequential_call" in out["wait_note"]
    out = status(rig, wait_seconds=60)
    assert "wait_note" not in out


def test_no_wait_note_for_the_default_wait_or_a_nan(rig):
    rig.host.deadline = 420.0  # default wait_seconds 600 is capped too, but the lead did not ask for it
    out = run(rig, [{"agent": "explorer", "goal": "slow"}])
    assert out["pending"] and "wait_note" not in out
    out = run(rig, [{"agent": "explorer", "goal": "slow"}], wait_seconds=float("nan"))
    assert out["ok"] and out["pending"] and "wait_note" not in out


def test_no_wait_note_when_the_deadline_is_raised(rig):
    rig.host.deadline = 1800.0
    rig.host.settings = {"wait_seconds": 600}
    out = run(rig, [{"agent": "explorer", "goal": "slow"}])
    assert out["pending"] and "wait_note" not in out
    assert rig.clock.now >= 600
