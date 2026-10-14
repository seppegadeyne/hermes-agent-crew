"""Run orchestration for hermes-claude-agents: launch, wait, collect, cancel, guard.

Children are launched through Hermes' public subagent lifecycle API, so they take the same host
path as ``delegate_task`` children (cleanup, cost roll-up, ``/stop`` propagation, the live subagent
list). On top of that this module adds what a lead model needs:

* named agents from Markdown files: the model picks a name, never a model, provider or tool set;
* a ``pre_tool_call`` guard that makes read-only agents really read-only;
* parallel launches with one bounded wait, and ``agents_status`` for runs that outlive it;
* compact results: the lead gets each child's final report, never its transcript.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agents_git
from .agents_config import INT_RANGES, AgentSpec, Roster, RosterCache, read_settings
from .agents_policy import ChildPolicy, PolicyRegistry

PLUGIN_ID = "hermes-claude-agents"
MAX_GOAL_CHARS = 12_000
MAX_CALLER_CONTEXT_CHARS = 24_000
CONTEXT_BUDGET = 31_500          # the lifecycle API refuses a context over 32,000 characters
MIN_DIFF_CHARS = 1_000
POLL_SECONDS = 0.5
DEADLINE_MARGIN_SECONDS = 25.0   # stop waiting this long before Hermes abandons the tool call
MAX_TRACKED_RUNS = 256
TOTAL_SUMMARY_BUDGET = 60_000
BRIDGE_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})
STALE_UNKNOWN_SECONDS = 7_200
TERMINAL = {"SUCCEEDED": "succeeded", "FAILED": "failed", "INTERRUPTED": "interrupted", "CANCELLED": "cancelled"}
STOP_STATUS = {"completed": "succeeded", "interrupted": "interrupted", "cancelled": "cancelled"}
ISOLATION_MESSAGE = ("hermes-claude-agents needs plugins.isolation: in_process. Inside the plugin host process "
                     "Hermes cannot bind a launch to the running agent turn.")


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload = {"ok": False, "error": code, "message": message}


def clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    marker = f"\n[… {len(value) - limit} characters omitted]"
    return value[:max(0, limit - len(marker))].rstrip() + marker


def _state_name(state: Any) -> str:
    return str(getattr(state, "value", state) or "UNKNOWN").upper()


def _depth(agent: Any) -> int:
    try:
        return int(getattr(agent, "_delegate_depth", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _weak(obj: Any) -> Callable[[], Any] | None:
    if obj is None:
        return None
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda: obj


@dataclass
class Run:
    run_id: str
    agent: str
    owner_session_id: str
    goal: str
    readonly: bool
    owner_ref: Callable[[], Any] | None = None
    model: str | None = None
    provider: str | None = None
    started_at: float = 0.0
    handle: Any = None
    subagent_id: str | None = None
    child_session_id: str | None = None
    status: str = "running"
    summary: str | None = None
    error: str | None = None
    api_calls: int | None = None
    duration_seconds: float | None = None
    unsupported: list = field(default_factory=list)
    note: str | None = None
    stop: dict | None = None
    cancel_requested: bool = False
    reported: bool = False


@dataclass
class _Intent:
    """Set while one launch runs, so the subagent_start hook binds the policy before the child acts."""
    policy: ChildPolicy
    subagent_id: str | None = None
    child_session_id: str | None = None


class AgentsService:
    def __init__(self, host: Any, *, clock=time.monotonic, wall=time.time, sleep=time.sleep) -> None:
        self.host = host
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.cache = RosterCache()
        self.policies = PolicyRegistry()
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._by_child_session: dict[str, str] = {}
        self._intent: contextvars.ContextVar = contextvars.ContextVar("hermes_claude_agents_intent", default=None)

    # ------------------------------------------------------------------ configuration

    def config(self) -> tuple[dict, list, Roster]:
        settings, problems = read_settings(self.host.get_config)
        return settings, problems, self.cache.get(settings, self.host.home())

    def features(self) -> dict:
        fields = self.host.request_fields()
        return {"provider": "provider" in fields, "effort": "reasoning_effort" in fields}

    # ------------------------------------------------------------------ hooks

    def on_subagent_start(self, child_subagent_id: Any = None, child_session_id: Any = None, **_: Any) -> None:
        intent = self._intent.get()
        if intent is None or not child_subagent_id:
            return None
        intent.subagent_id = str(child_subagent_id)
        intent.child_session_id = str(child_session_id) if child_session_id else None
        self.policies.register(intent.subagent_id, intent.policy)
        return None

    def on_subagent_stop(self, child_session_id: Any = None, child_summary: Any = None, child_status: Any = None,
                         duration_ms: Any = None, **_: Any) -> None:
        if not child_session_id:
            return None
        with self._lock:
            run = self._runs.get(self._by_child_session.get(str(child_session_id), ""))
            if run is not None:
                run.stop = {"status": str(child_status or ""),
                            "summary": child_summary if isinstance(child_summary, str) else None,
                            "duration_ms": duration_ms if isinstance(duration_ms, (int, float)) else None}
        return None

    def pre_tool_call(self, tool_name: Any = "", task_id: Any = "", **_: Any) -> dict | None:
        # Hermes blocks EVERY tool call when a pre_tool_call callback raises or hangs, so this stays a
        # total, constant-time function that only speaks for children this plugin launched. For those
        # children an internal error blocks (fail closed); every other call passes untouched.
        if not task_id:
            return None
        try:
            policy = self.policies.get(str(task_id))
        except Exception:
            return None
        if policy is None:
            return None
        try:
            message = policy.decide(str(tool_name or ""))
        except Exception:
            message = f"hermes-claude-agents: the tool policy check for agent '{policy.agent}' failed; blocked."
        return {"action": "block", "message": message} if message else None

    # ------------------------------------------------------------------ tools

    def run(self, args: Any, session_id: str | None = None) -> dict:
        try:
            return self._run(args, session_id)
        except Refusal as refusal:
            return refusal.payload

    def status(self, args: Any, session_id: str | None = None) -> dict:
        try:
            return self._status(args, session_id)
        except Refusal as refusal:
            return refusal.payload

    def _run(self, args: Any, session_id: str | None) -> dict:
        started = self.clock()  # Hermes' tool deadline also covers launching and the git snapshot
        if not isinstance(args, dict):
            raise Refusal("invalid_arguments", "Arguments must be an object with a tasks list.")
        parent = self._parent_or_refuse()
        if _depth(parent) > 0 or self.host.in_child_context():
            raise Refusal("nested_agent", "Subagents cannot start agents. Finish the task yourself and report back.")
        if self.host.spawn_paused():
            raise Refusal("spawn_paused", "The operator paused delegation; new agents cannot start right now.")
        session_id = str(session_id or getattr(parent, "session_id", "") or "")
        settings, problems, roster = self.config()
        tasks = self._parse_tasks(args, settings, roster)
        with self._lock:
            mine = [r for r in self._runs.values() if self._owns(r, session_id, parent) and r.status == "running"]
        writers = sum(1 for spec, _ in tasks if not spec.readonly)
        live_writers = sum(1 for r in mine if not r.readonly)
        if writers and writers + live_writers > settings["max_parallel_writers"]:
            raise Refusal("too_many_writers",
                          f"{writers} agent(s) in this call can change files and {live_writers} such run(s) are "
                          f"still in progress; at most {settings['max_parallel_writers']} may run at once so they "
                          "do not edit the same tree in parallel. Run writers one at a time.")
        if len(mine) + len(tasks) > settings["max_live_runs"]:
            raise Refusal("too_many_live_runs",
                          f"This conversation already has {len(mine)} agent run(s) in progress (limit "
                          f"{settings['max_live_runs']}). Collect them with agents_status first.")
        features = self.features()
        parent_provider = str(getattr(parent, "provider", "") or "")
        runs = [self._launch(spec, task, parent, parent_provider, session_id, settings, features)
                for spec, task in tasks]
        early = self._wait(runs, started + self._wait_budget(args.get("wait_seconds"), settings))
        return self._report(runs, settings, problems, roster, early)

    def _status(self, args: Any, session_id: str | None) -> dict:
        started = self.clock()
        args = args if isinstance(args, dict) else {}
        parent = self._parent_or_refuse()
        session_id = str(session_id or getattr(parent, "session_id", "") or "")
        settings, problems, roster = self.config()
        ids = args.get("run_ids")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
            raise Refusal("invalid_arguments", "run_ids must be a list of run_id strings.")
        unknown: list[str] = []
        with self._lock:
            if ids:
                runs = []
                for run_id in dict.fromkeys(ids):
                    run = self._runs.get(run_id)
                    if run is None or not self._owns(run, session_id, parent):
                        unknown.append(run_id)
                    else:
                        runs.append(run)
            else:
                runs = [r for r in self._runs.values() if self._owns(r, session_id, parent) and not r.reported]
        if args.get("cancel"):
            if not ids:
                raise Refusal("run_ids_required", "Name the runs to cancel in run_ids.")
            reason = clip(args.get("reason") or "cancelled by the lead agent", 500)
            for run in runs:
                self._cancel(run, reason)
        early = self._wait(runs, started + self._wait_budget(args.get("wait_seconds", 0), settings))
        out = self._report(runs, settings, problems, roster, early)
        if unknown:
            out["unknown_run_ids"] = unknown
        if not runs and not unknown:
            out["note"] = "No unreported agent runs in this conversation."
        return out

    def _parent_or_refuse(self) -> Any:
        if self.host.isolated:
            raise Refusal("unsupported_isolation", ISOLATION_MESSAGE)
        parent = self.host.parent()
        if parent is None:
            raise Refusal("no_active_turn", "Agents can only be used from inside an agent turn.")
        return parent

    @staticmethod
    def _owns(run: Run, session_id: str, parent: Any) -> bool:
        if run.owner_session_id and run.owner_session_id == session_id:
            return True
        # Context compression gives the conversation a new session id; the agent object stays the same.
        return run.owner_ref is not None and parent is not None and run.owner_ref() is parent

    # ------------------------------------------------------------------ launching

    def _parse_tasks(self, args: dict, settings: dict, roster: Roster) -> list:
        raw = args.get("tasks")
        if isinstance(raw, str):  # some models send the array as a JSON string
            try:
                raw = json.loads(raw)
            except ValueError:
                raise Refusal("invalid_arguments", "tasks must be a list of task objects.") from None
        if raw is None and args.get("agent"):
            raw = [{key: args.get(key) for key in ("agent", "goal", "context", "attach_diff", "diff_base", "workdir")}]
        if not isinstance(raw, list) or not raw:
            raise Refusal("invalid_arguments", "Pass tasks: [{agent, goal, context?, workdir?, attach_diff?}].")
        if len(raw) > settings["max_parallel"]:
            raise Refusal("too_many_tasks", f"At most {settings['max_parallel']} tasks per call.")
        names = ", ".join(sorted(roster.agents)) or "none (see /claude-agents)"
        tasks = []
        for index, item in enumerate(raw):
            where = f"tasks[{index}]"
            if not isinstance(item, dict):
                raise Refusal("invalid_arguments", f"{where} must be an object.")
            name = str(item.get("agent") or "").strip().lower()
            spec = roster.agents.get(name)
            if spec is None:
                raise Refusal("unknown_agent", f"{where}: unknown agent {name!r}. Agents: {names}.")
            goal = item.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise Refusal("invalid_goal", f"{where}: goal must be a non-empty string.")
            if len(goal) > MAX_GOAL_CHARS:
                raise Refusal("invalid_goal", f"{where}: goal is longer than {MAX_GOAL_CHARS} characters; move "
                                              "background material into context.")
            context = item.get("context")
            if context is not None and not isinstance(context, str):
                raise Refusal("invalid_context", f"{where}: context must be a string.")
            base = item.get("diff_base")
            if base not in (None, "") and not agents_git.valid_ref(base):
                raise Refusal("invalid_diff_base", f"{where}: diff_base {base!r} is not a git ref.")
            attach = item.get("attach_diff")
            tasks.append((spec, {
                "goal": goal.strip(),
                "context": (context or "").strip(),
                "attach_diff": attach is True or (isinstance(attach, str) and attach.strip().lower() == "true"),
                "diff_base": base.strip() if isinstance(base, str) and base.strip() else None,
                "workdir": self._workdir(item.get("workdir"), where),
            }))
        return tasks

    @staticmethod
    def _workdir(raw: Any, where: str) -> Path | None:
        """The folder an agent works in: an existing absolute path, or None for the session's own."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str):
            path = Path(raw.strip()).expanduser()
            if path.is_absolute() and path.is_dir():
                return path
        raise Refusal("invalid_workdir", f"{where}: workdir {raw!r} is not an existing absolute folder path.")

    def _launch(self, spec: AgentSpec, task: dict, parent: Any, parent_provider: str, session_id: str,
                settings: dict, features: dict) -> Run:
        run = Run(run_id="ag_" + uuid.uuid4().hex[:10], agent=spec.name, owner_session_id=session_id,
                  goal=task["goal"], readonly=spec.readonly, owner_ref=_weak(parent), model=spec.model,
                  started_at=self.wall())
        self._track(run)
        kwargs: dict[str, Any] = dict(
            goal=task["goal"], context=self._compose_context(spec, task, settings), role="leaf", model=spec.model,
            correlation_id=run.run_id, metadata={"plugin": PLUGIN_ID, "agent": spec.name, "run_id": run.run_id})
        toolsets = self._child_toolsets(spec, parent)
        if toolsets:
            kwargs["allowed_toolsets"] = toolsets
        if spec.provider:
            if spec.model is None and parent_provider and spec.provider != parent_provider:
                return self._fail(run, "model_required",
                                  f"Agent '{spec.name}' inherits the lead's model but runs on provider "
                                  f"'{spec.provider}' while the lead runs on '{parent_provider}'. Give the agent a "
                                  "model in its file or in settings.overrides.")
            if features["provider"]:
                kwargs["provider"] = spec.provider
                # An explicit route resolves its own model; "inherit" must still mean the lead's model.
                kwargs["model"] = spec.model or (str(getattr(parent, "model", "") or "") or None)
            elif spec.provider != parent_provider:
                return self._fail(run, "provider_unsupported",
                                  f"Agent '{spec.name}' runs on provider '{spec.provider}', but this Hermes build can "
                                  f"only run agents on the lead's provider ('{parent_provider or 'unknown'}'). Switch "
                                  "the lead to that provider, or update Hermes to a build whose "
                                  "SubagentLaunchRequest has a provider field (NousResearch/hermes-agent#137167).")
        if spec.effort:
            if features["effort"]:
                kwargs["reasoning_effort"] = spec.effort
            else:
                run.unsupported.append("effort")
        # Narrowing derived from an agent's tool list only trims the schema (the guard enforces the
        # list), so a build that refuses those toolsets gets one retry without them. Narrowing from an
        # explicit `toolsets:` key is the only restriction an unguarded agent has: no retry there.
        retry_without_toolsets = "allowed_toolsets" in kwargs and not spec.toolsets and spec.tools is not None
        intent = _Intent(ChildPolicy.for_spec(spec))
        token = self._intent.set(intent)
        try:
            try:
                handle = self.host.lifecycle.launch(self.host.make_request(**kwargs))
            except Exception as exc:
                if not (retry_without_toolsets and "toolsets" in str(exc).lower()):
                    raise
                kwargs.pop("allowed_toolsets", None)
                run.note = "toolsets_not_narrowed"
                handle = self.host.lifecycle.launch(self.host.make_request(**kwargs))
        except Exception as exc:
            return self._fail(run, "launch_failed", f"{type(exc).__name__}: {clip(exc, 1500)}")
        finally:
            self._intent.reset(token)
        with self._lock:
            run.handle = handle
            run.subagent_id = str(getattr(handle, "subagent_id", "") or intent.subagent_id or "") or None
            run.provider = getattr(handle, "provider", None) or kwargs.get("provider") or parent_provider or None
            run.model = getattr(handle, "model", None) or run.model
            run.child_session_id = intent.child_session_id
            if run.child_session_id:
                self._by_child_session[run.child_session_id] = run.run_id
        if run.subagent_id:
            # subagent_start already bound the policy before the child could act; this repeats it for a
            # build that does not deliver that hook to plugins.
            self.policies.register(run.subagent_id, intent.policy)
        return run

    def _compose_context(self, spec: AgentSpec, task: dict, settings: dict) -> str:
        head = f"## Agent: {spec.name}" + (f"\n\n{spec.instructions}" if spec.instructions else "")
        rules = ["## Rules for this run",
                 "- You work for a lead agent and cannot see its conversation: the goal and this context are all "
                 "you get. Do not ask questions; state your assumptions in the report."]
        if task.get("workdir"):
            rules.append(f"- Work in {task['workdir']}: resolve relative paths against it and pass absolute paths "
                         "to tools. It replaces any other workspace path you were given.")
        if spec.readonly:
            rules.append("- You are read-only. Tools that change files, run commands or start other agents are "
                         "blocked: investigate and report instead.")
        elif spec.tools is not None:
            rules.append("- Only your configured tools work; other tool calls are blocked.")
        rules.append("- Finish with ONE compact report for the lead: findings first (with file:line where it "
                     "applies), then what you checked, then open questions. No transcripts, no full file dumps.")
        parts = [head, "\n".join(rules)]
        remaining = CONTEXT_BUDGET - sum(len(p) + 2 for p in parts)
        caller = clip(task["context"], MAX_CALLER_CONTEXT_CHARS) if task["context"] else ""
        diff = ""
        if task["attach_diff"]:
            reserve = min(len(caller), 8_000) + 64 if caller else 0
            budget = min(settings["diff_max_chars"], remaining - reserve - 80)
            where = task.get("workdir") or self.host.cwd()
            body = (agents_git.collect(where, task["diff_base"], budget) if budget >= MIN_DIFF_CHARS
                    else "(no room left for a diff; read the changed files with read_file)")
            diff = "## Working tree snapshot (read-only, taken when the agent started)\n" + body
            remaining -= len(diff) + 2
        if caller:
            parts.append("## Context from the lead\n" + clip(caller, max(0, remaining - 30)))
        if diff:
            parts.append(diff)
        return clip("\n\n".join(parts), CONTEXT_BUDGET)

    def _child_toolsets(self, spec: AgentSpec, parent: Any) -> tuple | None:
        """Narrow the child's tool schema to what the agent may use; the guard is what enforces it."""
        if spec.toolsets:
            wanted = list(spec.toolsets)
        elif spec.tools is not None:
            # Bridge tools come with tool search itself and MCP toolsets follow inherit_mcp_toolsets.
            exact = [t for t in spec.tools if not set("*?[") & set(t) and t not in BRIDGE_TOOLS
                     and not t.startswith("mcp__")]
            wanted = [ts for ts in dict.fromkeys(self.host.toolset_for_tool(t) for t in exact)
                      if ts and not ts.startswith("mcp-")]
        else:
            return None
        enabled = getattr(parent, "enabled_toolsets", None)
        if enabled is not None:
            allowed = set(enabled)
            wanted = [t for t in wanted if t in allowed]
        return tuple(wanted) or None

    def _fail(self, run: Run, code: str, message: str) -> Run:
        with self._lock:
            run.status, run.error, run.note = "failed", message, code
        return run

    def _track(self, run: Run) -> None:
        with self._lock:
            self._runs[run.run_id] = run
            while len(self._runs) > MAX_TRACKED_RUNS:
                victim = next((rid for rid, r in self._runs.items() if r.status != "running"), None)
                if victim is None:
                    break
                old = self._runs.pop(victim)
                if old.child_session_id:
                    self._by_child_session.pop(old.child_session_id, None)

    # ------------------------------------------------------------------ waiting and results

    def _wait_budget(self, requested: Any, settings: dict) -> float:
        default = float(settings["wait_seconds"])
        try:
            value = default if requested is None or isinstance(requested, bool) else float(requested)
        except (TypeError, ValueError):
            value = default
        if value != value:  # NaN
            value = default
        value = max(0.0, min(value, float(INT_RANGES["wait_seconds"][1])))
        deadline = self.host.tool_deadline()
        if deadline:
            value = min(value, max(0.0, float(deadline) - DEADLINE_MARGIN_SECONDS))
        return value

    def _wait(self, runs: list, stop_at: float) -> str | None:
        """Poll until every run is finished or ``stop_at`` (clock time) passes; returns why it stopped early."""
        pending = [run for run in runs if run.status == "running"]
        while pending:
            pending = [run for run in pending if not self._poll(run)]
            if not pending or self.clock() >= stop_at:
                return None
            if self.host.interrupted():
                return "interrupted"
            if self.host.take_yield():
                return "new_message"
            self.sleep(POLL_SECONDS)
        return None

    def _poll(self, run: Run) -> bool:
        """Refresh one run; True once it is finished."""
        if run.status != "running":
            return True
        if run.handle is None:
            return self._finish_unknown(run)
        lifecycle = self.host.lifecycle
        try:
            state = _state_name(lifecycle.status(run.handle).state)
        except Exception:
            state = "UNKNOWN"
        if state in TERMINAL:
            try:
                result = lifecycle.result(run.handle)
            except Exception:
                result = None
            self._finish(run, state, result)
            return True
        if state == "UNKNOWN":
            if run.stop is not None:
                self._finish_from_stop(run)
                return True
            run.note = "handle_unavailable"
            if self.wall() - run.started_at > STALE_UNKNOWN_SECONDS:
                return self._finish_unknown(run)
        return False

    def _finish(self, run: Run, state: str, result: Any) -> None:
        summary = getattr(result, "summary", None)
        if not summary and run.stop:
            summary = run.stop.get("summary")
        usage = getattr(result, "usage_metadata", None) or {}
        tools = getattr(result, "tool_execution_summary", None) or {}
        with self._lock:
            run.status = TERMINAL[state]
            run.summary = summary or None
            if run.status != "succeeded":
                run.error = (getattr(result, "error_message", None) or getattr(result, "error_classification", None)
                             or run.status)
            calls = usage.get("api_calls") if isinstance(usage, dict) else None
            run.api_calls = calls if isinstance(calls, int) else None
            duration = tools.get("duration_seconds") if isinstance(tools, dict) else None
            run.duration_seconds = round(float(duration), 1) if isinstance(duration, (int, float)) else None
            run.note = None if run.note == "handle_unavailable" else run.note

    def _finish_from_stop(self, run: Run) -> None:
        stop = run.stop or {}
        with self._lock:
            run.status = STOP_STATUS.get(stop.get("status", ""), "failed")
            run.summary = stop.get("summary")
            if run.status != "succeeded":
                run.error = f"child ended with status {stop.get('status') or 'unknown'}"
            if isinstance(stop.get("duration_ms"), (int, float)):
                run.duration_seconds = round(stop["duration_ms"] / 1000.0, 1)
            run.note = "result_from_stop_hook"

    def _finish_unknown(self, run: Run) -> bool:
        with self._lock:
            run.status = "unknown"
            run.error = ("Hermes no longer knows this run (restart, or the result expired); its result is not "
                         "recoverable.")
        return True

    def _cancel(self, run: Run, reason: str) -> None:
        if run.status != "running" or run.handle is None:
            return
        try:
            result = self.host.lifecycle.cancel(run.handle, reason=reason)
            accepted = bool(getattr(result, "accepted", False))
        except Exception as exc:
            accepted = False
            run.note = f"cancel_failed: {clip(exc, 200)}"
        run.cancel_requested = True
        if not accepted and not run.note:
            run.note = "cancel_not_accepted"

    def _public(self, run: Run, summary_limit: int) -> dict:
        item: dict[str, Any] = {"run_id": run.run_id, "agent": run.agent, "status": run.status,
                                "goal": clip(" ".join(run.goal.split()), 100)}
        if run.model:
            item["model"] = run.model
        if run.readonly:
            item["readonly"] = True
        if run.status == "running":
            if run.cancel_requested:
                item["cancel_requested"] = True
        else:
            if run.summary:
                item["summary"] = clip(run.summary, summary_limit)
            if run.error:
                item["error"] = clip(run.error, 2_000)
            if run.duration_seconds is not None:
                item["duration_seconds"] = run.duration_seconds
            if run.api_calls is not None:
                item["api_calls"] = run.api_calls
        if run.unsupported:
            item["unsupported"] = list(run.unsupported)
        if run.note:
            item["note"] = run.note
        return item

    def _report(self, runs: list, settings: dict, problems: list | None = None,
                roster: Roster | None = None, early: str | None = None) -> dict:
        results, pending = [], []
        finished = sum(1 for run in runs if run.status != "running") or 1
        # Keep the whole tool result well under Hermes' 100k-character spill threshold.
        summary_limit = max(2_000, min(settings["summary_max_chars"], TOTAL_SUMMARY_BUDGET // finished))
        for run in runs:
            results.append(self._public(run, summary_limit))
            if run.status == "running":
                pending.append(run.run_id)
            else:
                run.reported = True
        out: dict[str, Any] = {"ok": True, "results": results}
        if pending:
            out["pending"] = pending
            out["next"] = ("These agents are still working. Continue with other work and call agents_status "
                           "(wait_seconds up to a few minutes) to collect them.")
            if early == "new_message":
                out["next"] = ("Stopped waiting because a new user message arrived; the agents keep working. "
                               "Read the message, then collect them with agents_status.")
            elif early == "interrupted":
                out["next"] = "Stopped waiting after an interrupt; agents that were not stopped keep working."
        notes = list(problems or [])
        if roster is not None and roster.problems:
            notes.append(f"{len(roster.problems)} agent file problem(s); /claude-agents lists them.")
        if notes:
            out["config_problems"] = notes[:5]
        return out

    # ------------------------------------------------------------------ text surfaces

    def roster_text(self) -> str:
        settings, problems, roster = self.config()
        features = self.features()
        lines = [f"Claude agents ({len(roster.agents)}):" if roster.agents else "No agents configured."]
        for spec in sorted(roster.agents.values(), key=lambda s: s.name):
            flags = [spec.model or "lead's model"]
            if spec.provider:
                flags.append(spec.provider)
            if spec.readonly:
                flags.append("read-only")
            lines.append(f"  {spec.name} ({', '.join(flags)}) - {spec.description or '(no description)'}")
            if spec.use_when:
                lines.append(f"    use when: {spec.use_when}")
        lines.append("Agent folders (a later folder overrides an earlier one): " + "; ".join(roster.folders))
        lines.append("This Hermes build: per-agent provider=" + ("yes" if features["provider"] else "no")
                     + ", per-agent effort=" + ("yes" if features["effort"] else "no"))
        with self._lock:
            running = sum(1 for r in self._runs.values() if r.status == "running")
        lines.append(f"Runs in progress in this process: {running}")
        for problem in (problems + roster.problems)[:20]:
            lines.append(f"  problem: {problem}")
        for warning in roster.warnings[:10]:
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)

    def prompt_section(self, info: Any = None) -> str:
        if _depth(self.host.parent()) > 0 or self.host.in_child_context():
            return ""  # children get their brief through context, not this section
        try:
            settings, _, roster = self.config()
        except Exception:
            return ""
        if not settings.get("prompt_section") or not roster.agents:
            return ""
        return render_prompt(roster)


def agent_label(spec: AgentSpec) -> str:
    flags = [spec.model or "lead's model"] + (["read-only"] if spec.readonly else [])
    return f"{spec.name} ({', '.join(flags)})"


def _shorten(text: str, limit: int) -> str:
    """Inline clip for one roster line: a plain ellipsis, no line break or character count."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def roster_line(spec: AgentSpec, description_chars: int = 90) -> str:
    """The trigger (use_when) is kept whole; the description fills ``description_chars``."""
    if not spec.use_when:
        return _shorten(spec.description, max(60, description_chars + 70))
    trigger = spec.use_when.rstrip(".…").rstrip()
    lowered = trigger.lower()
    if lowered.startswith("when "):
        trigger = "Use " + trigger
    elif not lowered.startswith("use "):
        trigger = "Use when " + trigger
    return f"{trigger}. {_shorten(spec.description, description_chars)}".rstrip()


_PROMPT_HEAD = [
    "Claude agents: named subagents for token-efficient delegation. You plan, decide and verify; agents do "
    "bounded work in their own context and return compact reports, so your context stays small.",
    "Roster:",
]
_PROMPT_TAIL = [
    'Call agents_run with {"tasks": [{"agent": "<name>", "goal": "...", "context": "...", "workdir": '
    '"/abs/path/to/repo"}]}. If it is a deferred tool, call it through tool_call with name "agents_run". '
    "Several tasks in one call run in parallel. Set workdir to the folder the work is in; it defaults to this "
    "session's working directory.",
    "When to use it:",
    "- Swarm: send read-only scouts for file discovery, grep sweeps and docs lookups instead of reading many "
    "files yourself.",
    "- Senior review on call: ask the reviewer (1) before you lock a multi-file plan, (2) when the same test or "
    "build error fails twice, (3) before you call non-trivial work done; pass attach_diff: true so it reads "
    "the diff itself.",
    "- Mechanical fan-out: whole-document checks and extractions (language or lint MCP checks over a file, "
    "page extraction) go to one worker per file or URL, in parallel, over the COMPLETE input. Do not sample "
    "excerpts inline to save calls; a short text (under ~1,000 characters) you may check inline.",
    "- Skip agents for small lookups: every agent starts with a fresh context.",
    "Agents cannot see this conversation, so give each one a complete goal and context (paths, constraints, "
    "what you already know). Treat their reports as evidence to check, not as facts. Runs that outlive the "
    "wait come back as run_ids for agents_status.",
]


def render_prompt(roster: Roster, limit: int = 3_800) -> str:
    """Head and usage guide always survive; the roster gets the space that is left.

    Over budget, descriptions shrink first (triggers stay whole), then agents move to a "… N more" line.
    """
    agents = sorted(roster.agents.values(), key=lambda s: s.name)[:12]
    hidden = len(roster.agents) - len(agents)
    fixed = len("\n".join(_PROMPT_HEAD + _PROMPT_TAIL)) + 1
    budget = limit - fixed

    def roster_block(shown: list[AgentSpec], description_chars: int, more: int) -> list[str]:
        block = [f"- {agent_label(spec)}: {roster_line(spec, description_chars)}" for spec in shown]
        if more:
            block.append(f"- … {more} more (/claude-agents lists them)")
        return block

    block: list[str] = []
    for count in range(len(agents), -1, -1):
        for description_chars in (90, 40, 0):
            block = roster_block(agents[:count], description_chars, hidden + len(agents) - count)
            if len("\n".join(block)) + 1 <= budget:
                break
        else:
            continue
        break
    return clip("\n".join(_PROMPT_HEAD + block + _PROMPT_TAIL), limit)


def tool_description(roster: Roster) -> str:
    agents = sorted(roster.agents.values(), key=lambda s: s.name)
    names = ", ".join(agent_label(spec) for spec in agents[:10]) or "none configured yet"
    return ("Run named Claude subagents from the operator's roster (each has a fixed model and tool set; read-only "
            "agents cannot change anything) on self-contained tasks, in parallel, and get their compact final "
            "reports. Agents cannot see this conversation: give each task a full goal and context. Results that "
            "take longer than wait_seconds come back as run_ids for agents_status. Agents at startup: "
            f"{names}.")
