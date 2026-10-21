"""Model-facing tool schemas and total JSON handlers."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

TOOLSET = "claude_agents"
TOOL_NAMES = ("agents_run", "agents_status")
_WAIT_MAX = 1800
_TASKS_MAX = 16  # the cap of the max_parallel setting; the schema must never refuse what settings allow

_TASK = {
    "type": "object",
    "properties": {
        "agent": {"type": "string", "description": "Agent name from the roster."},
        "goal": {"type": "string",
                 "description": "The complete task, as you would brief a colleague who has not seen this "
                                "conversation: what to find, check or change, and what to report."},
        "context": {"type": "string",
                    "description": "Facts the agent needs: paths, constraints, decisions, what you already know or "
                                   "ruled out. The agent sees only goal + context."},
        "workdir": {"type": "string",
                    "description": "Absolute path of the folder the work is in (a repository root, usually). "
                                   "Defaults to this session's working directory; attach_diff reads git there."},
        "write_scope": {"type": "array", "items": {"type": "string"},
                        "description": "For agents that change files: the files or folders this agent may change "
                                       "(relative to workdir or absolute). write_file and patch outside it are "
                                       "blocked. Writers with disjoint scopes run in parallel; without a scope, "
                                       "writers run one at a time."},
        "attach_diff": {"type": "boolean",
                        "description": "Attach the working tree's git status, stat and diff to the agent's context "
                                       "(for reviews; keeps the diff out of your own context)."},
        "diff_base": {"type": "string",
                      "description": "Git ref to diff against when attach_diff is true (default HEAD)."},
        "follow_up": {"type": "string",
                      "description": "Re-review: run_id of an earlier read-only run in the same workdir. The agent "
                                     "gets that report plus only the changes since that run started, and checks "
                                     "whether its findings were fixed, instead of reading everything again."},
    },
    "required": ["agent", "goal"],
}


_STAGE = {
    "type": "object",
    "properties": {
        **{k: v for k, v in _TASK["properties"].items() if k != "follow_up"},
        "name": {"type": "string", "description": "Unique stage name (lowercase), used by for_each and in item ids."},
        "split": {"type": "array", "items": {"type": "string"},
                  "description": "One run per entry (a folder, file or page); the entry is added to the goal as "
                                 "'Your part: ...'. Not together with for_each."},
        "for_each": {"type": "string",
                     "description": "Name of an earlier stage: run over the findings (items) it produced, each "
                                    "item in the run's context. A writer's write_scope comes from the items' files."},
        "per_run": {"type": "integer", "minimum": 1, "maximum": 8,
                    "description": "Items per run in a for_each stage (default 1; batch verify stages, e.g. 4)."},
        "retry": {"type": "integer", "minimum": 0, "maximum": 2,
                  "description": "Writer for_each stages: extra attempts for a run that reports failed (tests "
                                 "still red), with its report in context; stops early when it fails the same way."},
        "verify": {"type": "boolean",
                   "description": "This for_each stage (a read-only agent) confirms or rejects the items; only "
                                  "confirmed ones go on."},
    },
    "required": ["name", "agent", "goal"],
}
_PLAN = {
    "type": "object",
    "description": ("A plan for big jobs: stages run one after another inside the plugin, each fanning out in "
                    "parallel; you get one compact result. Instead of tasks, never together."),
    "properties": {
        "workdir": {"type": "string", "description": "Default absolute workdir for every stage."},
        "stages": {"type": "array", "minItems": 1, "maxItems": 8, "items": _STAGE},
    },
    "required": ["stages"],
}


def schema(name: str, description: str = "") -> dict:
    if name == "agents_run":
        return {"name": name, "description": description, "parameters": {
            "type": "object",
            "properties": {
                "tasks": {"type": "array", "minItems": 1, "maxItems": _TASKS_MAX, "items": _TASK,
                          "description": "One entry per agent run; entries run in parallel (the operator's "
                                         "max_parallel setting may allow fewer per call)."},
                "plan": _PLAN,
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": _WAIT_MAX,
                                 "description": "How long to wait for results before returning run_ids "
                                                "(default from settings, capped below Hermes' tool deadline). "
                                                "0 only when you have other work to do meanwhile."},
            },
            "required": [],
        }}
    return {"name": name, "description": (
        "Check, wait for or cancel agent runs started by agents_run in this conversation. Without run_ids it "
        "returns every run whose result you have not seen yet. With plan_id it continues and reports a plan "
        "(a plan only moves on while you wait), or with item gives one item's full trail."), "parameters": {
        "type": "object",
        "properties": {
            "run_ids": {"type": "array", "items": {"type": "string"},
                        "description": "Runs to check; omit for all unreported runs."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": _WAIT_MAX,
                             "description": "Wait up to this long for unfinished runs (default 0)."},
            "plan_id": {"type": "string",
                        "description": "A plan started by agents_run: continue, report or cancel it."},
            "item": {"type": "string", "description": "With plan_id: one item id; returns that item's full trail."},
            "cancel": {"type": "boolean", "description": "Cancel the runs named in run_ids, or the plan in plan_id."},
            "reason": {"type": "string", "description": "Why they are cancelled."},
        },
        "required": [],
    }}


def handler(name: str, get_service: Callable[[], Any]) -> Callable[..., str]:
    def handle(args: dict, **kwargs: Any) -> str:
        try:
            service = get_service()
            session_id = kwargs.get("session_id")
            value = service.run(args, session_id) if name == "agents_run" else service.status(args, session_id)
            return json.dumps(value, ensure_ascii=False)
        except Exception as exc:  # a tool handler must always answer with JSON
            return json.dumps({"ok": False, "error": "internal_error", "message": str(exc)[:2000]})
    handle.__name__ = f"{name}_handler"
    return handle
