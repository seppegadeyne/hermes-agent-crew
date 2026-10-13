"""Model-facing tool schemas and total JSON handlers."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

TOOLSET = "claude_agents"
TOOL_NAMES = ("agents_run", "agents_status")
_WAIT_MAX = 1800

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
        "attach_diff": {"type": "boolean",
                        "description": "Attach the working tree's git status, stat and diff to the agent's context "
                                       "(for reviews; keeps the diff out of your own context)."},
        "diff_base": {"type": "string",
                      "description": "Git ref to diff against when attach_diff is true (default HEAD)."},
    },
    "required": ["agent", "goal"],
}


def schema(name: str, description: str = "") -> dict:
    if name == "agents_run":
        return {"name": name, "description": description, "parameters": {
            "type": "object",
            "properties": {
                "tasks": {"type": "array", "minItems": 1, "maxItems": 8, "items": _TASK,
                          "description": "One entry per agent run; entries run in parallel."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": _WAIT_MAX,
                                 "description": "How long to wait for results before returning run_ids "
                                                "(default from settings, capped below Hermes' tool deadline)."},
            },
            "required": ["tasks"],
        }}
    return {"name": name, "description": (
        "Check, wait for or cancel agent runs started by agents_run in this conversation. Without run_ids it "
        "returns every run whose result you have not seen yet."), "parameters": {
        "type": "object",
        "properties": {
            "run_ids": {"type": "array", "items": {"type": "string"},
                        "description": "Runs to check; omit for all unreported runs."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": _WAIT_MAX,
                             "description": "Wait up to this long for unfinished runs (default 0)."},
            "cancel": {"type": "boolean", "description": "Cancel the runs named in run_ids."},
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
