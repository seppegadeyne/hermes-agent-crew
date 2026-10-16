"""Agent plans: validation, item and verdict parsing, the scope rule, and the stage engine."""
import json
import os

import pytest

from hermes_agent_crew import agents_plan as ap


def block(data) -> str:
    return "Report text.\n\n```json\n" + json.dumps(data) + "\n```\n"


# --------------------------------------------------------------------------- parse_plan

def test_parse_plan_shapes():
    plan = ap.parse_plan({"workdir": "/srv", "stages": [
        {"name": "scan", "agent": "explorer", "goal": "g", "split": ["a/", "b/"]},
        {"name": "check", "agent": "verifier", "goal": "v", "for_each": "scan", "verify": True, "per_run": 4},
        {"name": "fix", "agent": "builder", "goal": "f", "for_each": "check"},
        {"name": "review", "agent": "reviewer", "goal": "r", "attach_diff": True}]}, 24)
    scan, check, fix, review = plan["stages"]
    assert scan["produces"] and not check["produces"] and not fix["produces"]
    assert scan["task"]["workdir"] == "/srv" and check["per_run"] == 4 and review["for_each"] is None


@pytest.mark.parametrize("raw, code, text", [
    ({"stages": []}, "invalid_plan", "non-empty"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g"}] * 9}, "invalid_plan", "at most 8"),
    ({"stages": [{"name": "A b", "agent": "x", "goal": "g"}]}, "invalid_plan", "name"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g"}, {"name": "a", "agent": "x", "goal": "g"}]},
     "invalid_plan", "twice"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g", "for_each": "b"},
                 {"name": "b", "agent": "x", "goal": "g"}]}, "invalid_plan", "EARLIER"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g"},
                 {"name": "b", "agent": "x", "goal": "g", "for_each": "a", "split": ["x"]}]}, "invalid_plan", "not both"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g", "split": ["p"] * 25}]}, "plan_too_large", "at most 24"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g", "verify": True}]}, "invalid_plan", "needs for_each"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g", "per_run": 3}]}, "invalid_plan", "per_run only"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g"},
                 {"name": "b", "agent": "x", "goal": "g", "for_each": "a", "write_scope": ["/"]}]},
     "invalid_plan", "write scope"),
    ({"stages": [{"name": "a", "agent": "x", "goal": "g", "eval": "rm"}]}, "invalid_plan", "unknown key"),
    ("not json", "invalid_plan", "object"),
])
def test_parse_plan_refusals(raw, code, text):
    with pytest.raises(ap.PlanError) as err:
        ap.parse_plan(raw, 24)
    assert err.value.code == code and text in str(err.value)


# --------------------------------------------------------------------------- items and verdicts

def test_items_come_from_the_last_json_block():
    text = block({"items": [{"title": "old"}]}) + "more\n" + block(
        {"items": [{"title": "  a   b ", "files": "/r/x.py", "detail": "d" * 3000, "extra": 1}, {"nope": 1}, "x"]})
    items = ap.parse_items(text)
    assert items == [{"title": "a b", "files": ["/r/x.py"], "detail": "d" * 2000}]


@pytest.mark.parametrize("text", [
    None, "", "no block", "```json\n[1, 2]\n```", "```json\n{\"other\": 1}\n```",
    block({"items": [{"title": "ok"}]}) + "```json\n{broken\n```",  # the LAST block is the contract
])
def test_missing_or_malformed_items_block(text):
    assert ap.parse_items(text) is None


def test_empty_items_block_is_valid():
    assert ap.parse_items(block({"items": []})) == []


def test_oversized_report_only_parses_its_tail():
    text = block({"items": [{"title": "early"}]}) + "x" * (ap.MAX_SUMMARY_PARSE_CHARS + 10)
    assert ap.parse_items(text) is None


def test_verdicts_silence_never_confirms():
    incoming = [{"id": "scan-1", "title": "A"}, {"id": "scan-2", "title": "B"}, {"id": "scan-3", "title": "C"}]
    text = block({"items": [{"id": "scan-1"}, {"title": "b"}, {"id": "ghost"}],
                  "rejected": [{"id": "scan-1", "reason": "wrong"}]})
    confirmed, rejected = ap.parse_verdicts(text, incoming)
    assert confirmed == ["scan-2"]
    assert rejected == {"scan-1": "wrong", "scan-3": "verdict_missing"}
    assert ap.parse_verdicts("no block", incoming) == ([], {i["id"]: "verdict_missing" for i in incoming})


# --------------------------------------------------------------------------- scope rule

def test_plan_scope_stays_inside_the_workdir(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "deep" / "er").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    r = str(root)
    assert ap.plan_scope(["src/a.py", f"{r}/src/b.py"], r) == (f"{r}/src/a.py", f"{r}/src/b.py")
    assert ap.plan_scope([f"{r}/src"], r) == (f"{r}/src",)
    (root / ".git" / "hooks").mkdir(parents=True)
    for bad in ([".git/hooks"], [".git/config"], ["/"], [r], ["../outside/x"], [f"{r}/../outside"], ["link/x.py"], [f"{r}/src/deep/er"],
                ["a"] * 9, [], ["x\x00y"]):
        assert ap.plan_scope(bad, r) is None, bad
    assert os.path.realpath(r) == r


# --------------------------------------------------------------------------- the engine

STAGES = [
    {"name": "scan", "agent": "explorer", "goal": "Find old calls.", "split": ["src/a/", "src/b/"]},
    {"name": "verify", "agent": "verifier", "goal": "Check them.", "for_each": "scan", "verify": True, "per_run": 4},
    {"name": "fix", "agent": "builder", "goal": "Fix it.", "for_each": "verify"},
    {"name": "check", "agent": "reviewer", "goal": "Done check.", "attach_diff": True},
]


def driver(rig, answers):
    """Finish each launched run with the summary answers(index, request) returns (or leave it running)."""
    finished = set()

    def tick(now):
        for index, request in enumerate(rig.lifecycle.requests):
            if index in finished:
                continue
            answer = answers(index, request)
            if answer is None:
                continue
            finished.add(index)
            state, summary = answer if isinstance(answer, tuple) else ("SUCCEEDED", answer)
            rig.lifecycle.finish(f"sa-0-{index:08x}", state=state, summary=summary)
    rig.clock.on_sleep.append(tick)
    return finished


def run_plan(rig, stages=STAGES, **extra):
    return rig.service.run({"plan": {"workdir": str(rig.home), "stages": stages}, **extra}, session_id="lead-1")


def scan_answer(rig, titles_by_part):
    def answer(index, request):
        for part, titles in titles_by_part.items():
            if f"Your part: {part}" in request.goal:
                return block({"items": [{"title": t, "files": [str(rig.home / f"{t}.py")], "detail": "swap"}
                                        for t in titles]})
        return None
    return answer


def test_full_plan_scan_verify_fix_check(rig):
    scan = scan_answer(rig, {"src/a/": ["one", "two"], "src/b/": ["three", "one"]})

    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return scan(index, request)
        if request.metadata["agent"] == "verifier":
            assert '"id": "scan-1"' in request.context and "You verify the findings" in request.context
            return block({"items": [{"id": "scan-1"}, {"id": "scan-3"}],
                          "rejected": [{"id": "scan-2", "reason": "already fixed"}]})
        if request.metadata["agent"] == "builder":
            return "patched"
        return "LGTM: no blockers."

    driver(rig, answers)
    out = run_plan(rig)
    plan = out["plan"]
    assert plan["status"] == "succeeded", plan
    stages = {s["name"]: s for s in plan["stages"]}
    assert stages["scan"]["items"] == 3  # "one" twice with the same files: deduplicated
    assert (stages["verify"]["runs"], stages["verify"]["confirmed"], stages["verify"]["rejected"]) == (1, 2, 1)
    assert stages["fix"]["runs"] == 2 and stages["check"]["runs"] == 1
    status = {i["id"]: i["status"] for i in plan["items"]}
    assert status == {"scan-1": "fix: done", "scan-2": "rejected", "scan-3": "fix: done"}
    assert plan["final"] == "LGTM: no blockers."
    builders = [r for r in rig.lifecycle.requests if r.metadata["agent"] == "builder"]
    assert all("Items" in r.context and "next stage" not in r.context for r in builders)
    assert len(json.dumps(out)) < 9_000
    # Plan runs never show up as unreported runs for the lead.
    assert rig.service.status({}, "lead-1")["note"] == "No unreported agent runs in this conversation."
    trail = rig.service.status({"plan_id": plan["plan_id"], "item": "scan-1"}, "lead-1")["item"]
    assert [t["stage"] for t in trail["trail"]] == ["scan", "verify", "fix"]


def test_builders_get_scopes_from_their_items_and_run_in_parallel(rig):
    scan = scan_answer(rig, {"src/a/": ["one", "two", "three"]})
    stages = [{**STAGES[0], "split": ["src/a/"]}, {"name": "fix", "agent": "builder", "goal": "Fix.", "for_each": "scan"}]

    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return scan(index, request)
        return None  # builders keep running

    driver(rig, answers)
    out = run_plan(rig, stages, wait_seconds=30)
    assert out["plan"]["status"] == "running" and "agents_status" in out["next"]
    assert "progress" in out["plan"]
    builders = [r for r in rig.lifecycle.requests if r.metadata["agent"] == "builder"]
    assert len(builders) == 3  # disjoint scopes: all three at once
    assert all("Your write scope" in r.context for r in builders)


def test_unsafe_item_files_are_refused_not_fixed(rig):
    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return block({"items": [{"title": "evil", "files": ["/etc/passwd"]},
                                    {"title": "fine", "files": [str(rig.home / "ok.py")]}]})
        return "done"
    driver(rig, answers)
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    plan = run_plan(rig, stages)["plan"]
    status = {i["title"]: (i["status"], i.get("reason")) for i in plan["items"]}
    assert status["evil"][0] == "refused_scope" and status["fine"][0] == "fix: done"
    assert sum(1 for r in rig.lifecycle.requests if r.metadata["agent"] == "builder") == 1


def test_items_without_files_get_the_workdir_scope_and_run_one_at_a_time(rig):
    driver(rig, lambda i, r: block({"items": [{"title": "a"}, {"title": "b"}]})
           if r.metadata["agent"] == "explorer" else None)
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    out = run_plan(rig, stages, wait_seconds=30)
    builders = [r for r in rig.lifecycle.requests if r.metadata["agent"] == "builder"]
    assert len(builders) == 1 and f"Your write scope: {os.path.realpath(rig.home)}." in builders[0].context
    assert "0/2 runs finished" in out["plan"]["progress"]  # the second waits for the first, no note needed


def test_empty_scan_ends_the_plan_early(rig):
    driver(rig, lambda i, r: block({"items": []}))
    plan = run_plan(rig)["plan"]
    assert plan["status"] == "succeeded" and "skipped" in plan["note"]
    assert [s.get("skipped") for s in plan["stages"]] == [None, True, True, True]
    assert len(rig.lifecycle.requests) == 2


def test_missing_items_block_fails_loudly(rig):
    driver(rig, lambda i, r: "I found three things but forgot the block.")
    plan = run_plan(rig)["plan"]
    assert plan["status"] == "failed" and "items_missing" in plan["error"]
    assert "forgot the block" in plan["error"]


def test_a_failed_run_stops_the_plan(rig):
    driver(rig, lambda i, r: ("FAILED", "boom") if "src/b/" in r.goal else block({"items": [{"title": "x"}]}))
    plan = run_plan(rig)["plan"]
    assert plan["status"] == "failed" and "stage scan" in plan["error"]
    assert not any(r.metadata["agent"] == "verifier" for r in rig.lifecycle.requests)


def test_too_many_items_fails_and_lists_them(rig):
    rig.host.settings = {"plan_max_items": 2}
    driver(rig, lambda i, r: block({"items": [{"title": f"t{n}"} for n in range(3)]}))
    plan = run_plan(rig, [STAGES[0] | {"split": ["only"]}, STAGES[1]])["plan"]
    assert plan["status"] == "failed" and "plan_max_items" in plan["error"] and "t2" in plan["error"]


def test_one_live_plan_per_conversation_and_cancel(rig):
    out = run_plan(rig, wait_seconds=0)
    plan_id = out["plan"]["plan_id"]
    refused = run_plan(rig, wait_seconds=0)
    assert refused["error"] == "plan_running"
    cancelled = rig.service.status({"plan_id": plan_id, "cancel": True}, "lead-1")
    assert cancelled["plan"]["status"] == "cancelled" and len(rig.lifecycle.cancels) == 2
    assert rig.service.status({"plan_id": "pl_nope"}, "lead-1")["error"] == "unknown_plan"


def test_plan_continues_only_inside_agents_status(rig):
    out = run_plan(rig, wait_seconds=0)
    plan_id = out["plan"]["plan_id"]
    assert len(rig.lifecycle.requests) == 2  # the first wave started
    driver(rig, lambda i, r: block({"items": []}))
    rig.clock.now += 100  # nothing moves on without the lead
    assert len(rig.lifecycle.requests) == 2
    done = rig.service.status({"plan_id": plan_id, "wait_seconds": 60}, "lead-1")
    assert done["plan"]["status"] == "succeeded"


def test_spawn_pause_holds_the_next_wave(rig):
    def answers(index, request):
        rig.host.paused = True  # the operator pauses while scan runs
        return block({"items": [{"title": "x", "files": [str(rig.home / "x.py")]}]})
    driver(rig, answers)
    out = run_plan(rig, [STAGES[0] | {"split": ["only"]}, STAGES[1]], wait_seconds=30)
    assert out["plan"]["status"] == "running" and "paused" in out["plan"]["note"]
    assert len(rig.lifecycle.requests) == 1


def test_plan_survives_a_session_id_change(rig):
    out = run_plan(rig, wait_seconds=0)
    rig.host.parent_agent.session_id = "lead-2"  # context compression
    driver(rig, lambda i, r: block({"items": []}))
    done = rig.service.status({"plan_id": out["plan"]["plan_id"], "wait_seconds": 60}, "lead-2")
    assert done["plan"]["status"] == "succeeded"


def test_verify_stage_needs_a_read_only_agent(rig):
    stages = [STAGES[0], {**STAGES[1], "agent": "builder"}]
    assert run_plan(rig, stages)["error"] == "invalid_plan"
    assert rig.lifecycle.requests == []


def test_tasks_and_plan_together_are_refused(rig):
    out = rig.service.run({"tasks": [{"agent": "explorer", "goal": "x"}], "plan": {"stages": STAGES}},
                          session_id="lead-1")
    assert out["error"] == "invalid_plan"


def test_concurrent_writer_waits_for_the_plan_builder(rig):
    rig.host.settings = {"max_parallel_writers": 1}
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    driver(rig, lambda i, r: block({"items": [{"title": "a"}]}) if r.metadata["agent"] == "explorer" else None)
    out = run_plan(rig, stages, wait_seconds=30)
    assert out["plan"]["status"] == "running"
    other = rig.service.run({"tasks": [{"agent": "builder", "goal": "another change"}]}, session_id="lead-1")
    assert other["error"] == "too_many_writers"  # the plan's unscoped builder holds the single writer slot


def test_concurrent_steps_never_close_a_stage_twice(rig):
    import threading
    out = run_plan(rig, wait_seconds=0)
    plan = rig.service._plans[out["plan"]["plan_id"]]
    for i in range(len(rig.lifecycle.requests)):
        rig.lifecycle.finish(f"sa-0-{i:08x}", summary=block({"items": [{"title": f"t{i}"}]}))
    settings, _, _ = rig.service.config()
    features = rig.service.features()
    gate = threading.Event()
    real = rig.service._close_stage

    def slow_close(*a):
        gate.wait(1)
        return real(*a)

    rig.service._close_stage = slow_close
    threads = [threading.Thread(target=rig.service._step, args=(plan, rig.host.parent_agent, settings, features))
               for _ in range(4)]
    for th in threads:
        th.start()
    gate.set()
    for th in threads:
        th.join()
    assert plan.stage_index == 1 and [s["name"] for s in plan.stats] == ["scan"]
    assert sum(1 for r in rig.lifecycle.requests if r.metadata["agent"] == "verifier") == 1


def test_cancelled_writer_frees_its_slot(rig):
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    driver(rig, lambda i, r: block({"items": [{"title": "a"}]}) if r.metadata["agent"] == "explorer" else None)
    plan_id = run_plan(rig, stages, wait_seconds=30)["plan"]["plan_id"]
    rig.service.status({"plan_id": plan_id, "cancel": True}, "lead-1")
    builder_index = next(i for i, r in enumerate(rig.lifecycle.requests) if r.metadata["agent"] == "builder")
    rig.lifecycle.finish(f"sa-0-{builder_index:08x}", state="CANCELLED", summary=None)
    other = rig.service.run({"tasks": [{"agent": "builder", "goal": "next change"}], "wait_seconds": 0},
                            session_id="lead-1")
    assert other["ok"], other


def test_plan_behind_an_unpolled_lead_writer_moves_on_once_it_ends(rig):
    lead = rig.service.run({"tasks": [{"agent": "builder", "goal": "lead change"}], "wait_seconds": 0},
                           session_id="lead-1")
    assert lead["ok"]
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    driver(rig, lambda i, r: block({"items": [{"title": "a"}]}) if r.metadata["agent"] == "explorer"
           else ("done" if i > 0 else None))
    out = run_plan(rig, stages, wait_seconds=30)
    assert out["plan"]["status"] == "running" and lead["results"][0]["run_id"] in out["plan"]["note"]
    rig.lifecycle.finish("sa-0-00000000", summary="lead done")  # nobody polls it: the plan must notice
    done = rig.service.status({"plan_id": out["plan"]["plan_id"], "wait_seconds": 60}, "lead-1")
    assert done["plan"]["status"] == "succeeded", done


def test_writers_disabled_refuses_the_plan_up_front(rig):
    rig.host.settings = {"max_parallel_writers": 0}
    assert run_plan(rig)["error"] == "writers_disabled" and rig.lifecycle.requests == []


def test_worst_case_report_stays_within_budget(rig):
    rig.host.settings = {"plan_max_items": 64}
    long = "x" * 190

    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return block({"items": [{"title": f"{n} {long}"} for n in range(60)]})
        if request.metadata["agent"] == "verifier":
            return block({"items": [], "rejected": [{"id": f"scan-{n}", "reason": long} for n in range(1, 61)]})
        return "y" * 50_000
    driver(rig, answers)
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "verify", "agent": "verifier", "goal": "v", "for_each": "scan", "verify": True, "per_run": 8},
              {"name": "check", "agent": "reviewer", "goal": "r"}]
    out = run_plan(rig, stages)
    assert out["plan"]["status"] == "succeeded"
    assert len(json.dumps(out["plan"], ensure_ascii=False)) <= ap_budget()
    assert out["plan"]["more_items"] > 0


def ap_budget():
    from hermes_agent_crew.agents_service import PLAN_REPORT_CHARS
    return PLAN_REPORT_CHARS


def test_a_mixed_batch_scopes_files_and_the_workdir(rig):
    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return block({"items": [{"title": "a", "files": [str(rig.home / "a.py")]}, {"title": "b"}]})
        return "done"
    driver(rig, answers)
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan", "per_run": 2}]
    assert run_plan(rig, stages)["plan"]["status"] == "succeeded"
    builder = next(r for r in rig.lifecycle.requests if r.metadata["agent"] == "builder")
    assert os.path.realpath(rig.home) in builder.context.split("Your write scope: ")[1].split(". Change")[0]


def test_a_failed_fix_marks_each_item_by_its_own_run(rig):
    def answers(index, request):
        if request.metadata["agent"] == "explorer":
            return block({"items": [{"title": "a", "files": [str(rig.home / "a.py")]},
                                    {"title": "b", "files": [str(rig.home / "b.py")]}]})
        return ("FAILED", "tests red") if "b.py" in request.context.split("## Items")[1][:400] else "done"
    driver(rig, answers)
    stages = [{"name": "scan", "agent": "explorer", "goal": "g"},
              {"name": "fix", "agent": "builder", "goal": "f", "for_each": "scan"}]
    plan = run_plan(rig, stages)["plan"]
    assert plan["status"] == "failed" and "final" not in plan
    assert {i["title"]: i["status"] for i in plan["items"]} == {"a": "fix: done", "b": "fix: failed"}


def test_status_without_args_lists_a_running_plan(rig):
    plan_id = run_plan(rig, wait_seconds=0)["plan"]["plan_id"]
    assert rig.service.status({}, "lead-1")["plans_running"] == [plan_id]
