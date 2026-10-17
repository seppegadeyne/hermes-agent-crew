"""Run orchestration for hermes-agent-crew: launch, wait, collect, cancel, guard.

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
import fnmatch
import json
import os
import shlex
import textwrap
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import agents_git, agents_plan
from .agents_config import INT_RANGES, AgentSpec, Roster, RosterCache, read_settings
from .agents_plan import PlanError
from .agents_policy import ChildPolicy, PolicyRegistry, scope_path, scopes_overlap

PLUGIN_ID = "hermes-agent-crew"
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
# Lead tool calls that only look around. A run of them in one turn is the work a scout does cheaper.
SCOUT_TOOLS = frozenset({"read_file", "search_files", "web_search", "web_extract"})
# Shell programs that only read or filter. A terminal command counts as scouting when every segment
# (split on &&, ||, ; and |) runs one of these and nothing writes a file.
# echo and printf only print here: a segment that redirects into a file is refused before this list is used.
READ_PROGRAMS = frozenset({"cat", "grep", "egrep", "fgrep", "rg", "head", "tail", "ls", "wc", "tree", "find", "sed",
                           "git", "sort", "uniq", "cut", "tr", "nl", "column", "stat", "file", "du", "jq", "echo",
                           "printf", "pwd", "which", "realpath", "readlink", "basename", "dirname", "diff", "cmp",
                           "md5sum", "sha1sum", "sha256sum", "true"})
GIT_READ_COMMANDS = frozenset({"log", "show", "diff", "status", "grep", "blame", "ls-files", "branch", "rev-parse",
                               "describe", "ls-tree", "cat-file", "shortlog"})
# Allowed in a read command, but on their own they are not a lookup (`echo ---` separators, `pwd`).
PRINT_ONLY = frozenset({"echo", "printf", "pwd", "true"})
FIND_WRITE_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
                                "-fls"})
_SEGMENT_BREAKS = frozenset({"&&", "||", ";", "|", "&", ";;"})
_HARMLESS_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
MAX_ERROR_PARSE_CHARS = 100_000
MAX_NUDGE_SESSIONS = 512
MAX_SEEN_LOOKUPS = 64            # distinct lookups remembered per turn; far above any nudge threshold
LAUNCH_FAILURES = frozenset({"launch_failed", "endpoint_unavailable", "model_required", "provider_unsupported"})
PLAN_REPORT_CHARS = 8_000        # the lead reads at most this much of a plan's result
PLAN_TRAIL_CHARS = 4_000         # per run summary kept in an item's trail
MAX_TRACKED_PLANS = 32
PROBE_TTL_UP_SECONDS = 60.0     # an endpoint that answered is trusted this long
PROBE_TTL_DOWN_SECONDS = 15.0   # one that did not is retried sooner (a local server may be starting)
ISOLATION_MESSAGE = ("hermes-agent-crew needs plugins.isolation: in_process. Inside the plugin host process "
                     "Hermes cannot bind a launch to the running agent turn.")


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
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
    write_scope: tuple = ()
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
    fallback: str | None = None  # routes skipped because their endpoint did not answer
    plan_id: str | None = None   # set for runs a plan launched; the plan reports them, not agents_status
    stop: dict | None = None
    cancel_requested: bool = False
    reported: bool = False


@dataclass
class _Intent:
    """Set while one launch runs, so the subagent_start hook binds the policy before the child acts."""
    policy: ChildPolicy
    subagent_id: str | None = None
    child_session_id: str | None = None


@dataclass
class Plan:
    """A plan's state. It advances only inside the lead's own agents_run / agents_status waits."""
    plan_id: str
    owner_session_id: str
    owner_ref: Callable[[], Any] | None
    stages: list                     # parsed stages, each with "spec" and "task" (the template)
    started_at: float
    status: str = "running"          # running | succeeded | failed | cancelled
    stage_index: int = 0
    stage_started: float = 0.0
    items: dict = field(default_factory=dict)       # item id -> item (with status, reason, trail)
    outputs: dict = field(default_factory=dict)     # stage name -> item ids it passes on
    queue: list = field(default_factory=list)       # (spec, task, item ids) not launched yet
    current: list = field(default_factory=list)     # (run, item ids) of the current stage
    done: list = field(default_factory=list)        # (run, item ids) finished in the current stage
    stats: list = field(default_factory=list)       # per finished stage
    final: str | None = None
    error: str | None = None
    note: str | None = None
    finished_at: float | None = None
    reported: bool = False
    lock: Any = field(default_factory=threading.Lock, repr=False)  # one advancer at a time
    cancelled_runs: list = field(default_factory=list)  # still polled after a cancel, so admission frees up
    stage_spec: Any = None           # the current stage's agent (route picked once per stage)
    stage_template: dict = field(default_factory=dict)
    retried: int = 0                 # retry runs launched in the current stage


class AgentsService:
    def __init__(self, host: Any, *, clock=time.monotonic, wall=time.time, sleep=time.sleep) -> None:
        self.host = host
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.cache = RosterCache()
        self.policies = PolicyRegistry()
        self._lock = threading.RLock()
        self._admit = threading.Lock()  # serializes agents_run admission + launch
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._by_child_session: dict[str, str] = {}
        self._intent: contextvars.ContextVar = contextvars.ContextVar("hermes_agent_crew_intent", default=None)
        self._nudges: OrderedDict[str, dict] = OrderedDict()  # lead session id -> this turn's scouting count
        self._probes: dict[str, tuple[bool, float]] = {}  # endpoint base_url -> (up, checked at)
        self._plans: OrderedDict[str, Plan] = OrderedDict()

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

    def pre_tool_call(self, tool_name: Any = "", task_id: Any = "", args: Any = None, **_: Any) -> dict | None:
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
            message = policy.decide(str(tool_name or ""), args)
        except Exception:
            message = f"hermes-agent-crew: the tool policy check for agent '{policy.agent}' failed; blocked."
        return {"action": "block", "message": message} if message else None

    def on_transform_tool_result(self, tool_name: Any = "", args: Any = None, result: Any = None, task_id: Any = "",
                                 session_id: Any = "", turn_id: Any = "", status: Any = "", **_: Any) -> str | None:
        """Nudge a lead that keeps scouting by hand to hand the rest to read-only agents.

        Counts the lead's own read and search calls per turn; at ``nudge_after`` it appends one short
        hint to that tool result, at most once per turn and never after the turn used agents. Fail-open
        and cheap: anything unexpected returns None and leaves the result as it is.
        """
        try:
            return self._nudge(str(tool_name or ""), args, result, str(task_id or ""), str(session_id or ""),
                               str(turn_id or ""), status)
        except Exception:
            return None

    def _nudge(self, tool_name: str, args: Any, result: Any, task_id: str, session_id: str,
               turn_id: str, status: Any = "") -> str | None:
        # Only model-facing calls of a live turn carry both ids. Calls from execute_code scripts and other
        # nested paths come without them, and their consumers parse the result as JSON: never touch those.
        if not session_id or not turn_id or not isinstance(result, str) or self.policies.get(task_id) is not None:
            return None
        delegating = tool_name in ("agents_run", "agents_status")
        if not delegating and not _is_scouting(tool_name, args):
            return None
        if self.host.in_child_context() or _depth(self.host.parent()) > 0:
            return None  # children never get the hint, whoever launched them
        with self._lock:
            state = self._nudges.get(session_id)
            if state is None or state["turn"] != turn_id:
                state = {"turn": turn_id, "count": 0, "done": False, "fanouts": 0, "plan_hint": False,
                         "seen": set()}
                self._nudges[session_id] = state
            self._nudges.move_to_end(session_id)
            while len(self._nudges) > MAX_NUDGE_SESSIONS:
                self._nudges.popitem(last=False)
            if delegating:
                state["done"] = True
                plan_hint = self._count_fanout(state, tool_name, args, result)
            elif state["done"]:
                return None
            else:
                # Distinct targets, not calls: paging through one file (often the file the lead is about
                # to edit) is one lookup, not the spread of reads a scout would take over.
                key = _lookup_key(tool_name, args)
                if key in state["seen"]:
                    return None
                if len(state["seen"]) < MAX_SEEN_LOOKUPS:
                    state["seen"].add(key)
                state["count"] += 1
            count = state["count"]
        if delegating:
            if not plan_hint or is_error_result(result, status) or not self.config()[0]["nudge_after"]:
                return None
            return result + "\n\n" + PLAN_HINT
        settings, _, roster = self.config()
        threshold = settings["nudge_after"]
        if not threshold or count < threshold or is_error_result(result, status):
            return None
        scouts = sorted(spec.name for spec in roster.agents.values() if spec.readonly)
        if not scouts:
            return None
        with self._lock:
            if state["done"]:  # a parallel call of the same turn got there first
                return None
            state["done"] = True
        return result + "\n\n" + nudge_text(count, scouts)

    @staticmethod
    def _count_fanout(state: dict, tool_name: str, args: Any, result: Any) -> bool:
        """True once per turn, at the second agents_run tasks fan-out with 3+ reports: chained calls a plan
        would have kept out of the lead's context. Caller holds self._lock. Counting only, never blocking."""
        if tool_name != "agents_run" or not isinstance(args, dict) or args.get("plan") is not None:
            return False
        if _report_count(result) < 3:
            return False
        state["fanouts"] += 1
        if state["fanouts"] < 2 or state["plan_hint"]:
            return False
        state["plan_hint"] = True
        return True

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
        if args.get("plan") is not None:
            return self._run_plan(args, parent, session_id, settings, problems, roster, started)
        tasks = self._parse_tasks(args, settings, roster)
        features = self.features()
        parent_provider = str(getattr(parent, "provider", "") or "")
        # Probes run here, outside the admit lock; their time counts against this call's wait budget.
        tasks = [(self._pick_route(spec, None if features["provider"] else parent_provider), task)
                 for spec, task in tasks]
        # Admission and launch are one step: two agents_run calls at the same time must not both pass the
        # writer and live-run checks before either has tracked its runs. Launching only starts threads.
        with self._admit:
            mine = self._live_runs(session_id, parent)
            self._check_writers([(spec, task) for spec, task in tasks if not spec.readonly],
                                [r for r in mine if not r.readonly], settings)
            if len(mine) + len(tasks) > settings["max_live_runs"]:
                raise Refusal("too_many_live_runs",
                              f"This conversation already has {len(mine)} agent run(s) in progress (limit "
                              f"{settings['max_live_runs']}). Collect them with agents_status first.")
            runs = [self._launch(spec, task, parent, parent_provider, session_id, settings, features)
                    for spec, task in tasks]
        early = self._wait(runs, started + self._wait_budget(args.get("wait_seconds"), settings))
        return self._report(runs, settings, problems, roster, early)

    def _live_runs(self, session_id: str, parent: Any) -> list:
        """This conversation's runs that are really still running: each one is polled first, so a run that
        finished while nobody waited on it (a plan's cancelled run, a wait_seconds 0 run) frees its slot.
        Polling does not mark a run reported, so agents_status output is unchanged."""
        with self._lock:
            owned = [r for r in self._runs.values() if self._owns(r, session_id, parent) and r.status == "running"]
        return [r for r in owned if not self._poll(r)]

    @staticmethod
    def _check_writers(new: list, live: list, settings: dict) -> None:
        """Unscoped writers run one at a time (max_parallel_writers); writers with disjoint write scopes may
        run side by side, because the guard keeps each one's file edits inside its own scope."""
        if not new:
            return
        limit = settings["max_parallel_writers"]
        if limit == 0:
            raise Refusal("writers_disabled", "The operator turned off agents that change files "
                                              "(max_parallel_writers: 0). Make the change yourself.")
        scoped = [task["write_scope"] for _, task in new if task["write_scope"]] + \
                 [run.write_scope for run in live if run.write_scope]
        unscoped = sum(1 for _, task in new if not task["write_scope"]) + \
            sum(1 for run in live if not run.write_scope)
        if unscoped:
            total = len(new) + len(live)
            if total > limit:
                raise Refusal("too_many_writers",
                              f"{len(new)} agent(s) in this call can change files and {len(live)} such run(s) are "
                              f"still in progress; at most {limit} may run at once when one of them has no "
                              "write_scope, so they do not edit the same tree in parallel. Nothing was started. "
                              "Give every writer a write_scope (the files or folders it may change, disjoint "
                              "per writer) to run them in parallel, or run them one at a time.")
            return
        for i, a in enumerate(scoped):
            for b in scoped[i + 1:]:
                if scopes_overlap(a, b):
                    raise Refusal("overlapping_write_scopes",
                                  f"Write scopes overlap ({', '.join(a)} / {', '.join(b)}); writers that run at the "
                                  "same time need disjoint scopes. Nothing was started. Narrow the scopes, or run "
                                  "these writers one after the other.")

    def _status(self, args: Any, session_id: str | None) -> dict:
        started = self.clock()
        args = args if isinstance(args, dict) else {}
        parent = self._parent_or_refuse()
        session_id = str(session_id or getattr(parent, "session_id", "") or "")
        settings, problems, roster = self.config()
        if args.get("plan_id") is not None:
            return self._status_plan(args, parent, session_id, settings, problems, roster, started)
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
                runs = [r for r in self._runs.values()
                        if self._owns(r, session_id, parent) and not r.reported and not r.plan_id]
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
        with self._lock:
            live_plans = [p.plan_id for p in self._plans.values()
                          if p.status == "running" and self._owns(p, session_id, parent)]
        if not ids and live_plans:
            out["plans_running"] = live_plans
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
            keys = ("agent", "goal", "context", "attach_diff", "diff_base", "workdir", "write_scope")
            raw = [{key: args.get(key) for key in keys}]
        if not isinstance(raw, list) or not raw:
            raise Refusal("invalid_arguments",
                          "Pass tasks: [{agent, goal, context?, workdir?, write_scope?, attach_diff?}].")
        if len(raw) > settings["max_parallel"]:
            raise Refusal("too_many_tasks", f"At most {settings['max_parallel']} tasks per call; nothing was started. "
                                            "Send the rest in a second agents_run call, or merge related tasks.")
        return [self._parse_task(item, f"tasks[{index}]", roster) for index, item in enumerate(raw)]

    def _parse_task(self, item: Any, where: str, roster: Roster) -> tuple:
        """One task (or a plan stage's task fields) -> (AgentSpec, normalized task dict); raises Refusal."""
        if not isinstance(item, dict):
            raise Refusal("invalid_arguments", f"{where} must be an object.")
        names = ", ".join(sorted(roster.agents)) or "none (see /crew)"
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
        workdir = self._workdir(item.get("workdir"), where)
        write_scope = self._write_scope(item.get("write_scope"), workdir or self.host.cwd(), spec, where)
        return spec, {
            "goal": goal.strip(),
            "context": (context or "").strip(),
            "attach_diff": attach is True or (isinstance(attach, str) and attach.strip().lower() == "true"),
            "diff_base": base.strip() if isinstance(base, str) and base.strip() else None,
            "workdir": workdir,
            "write_scope": write_scope,
        }

    @staticmethod
    def _write_scope(raw: Any, base: Path, spec: AgentSpec, where: str) -> tuple:
        """Normalized absolute paths a writer may change (relative ones resolve against its workdir)."""
        if raw is None or raw == "" or raw == []:
            return ()
        if spec.readonly:
            raise Refusal("invalid_write_scope", f"{where}: agent '{spec.name}' is read-only; write_scope is only "
                                                 "for agents that change files.")
        items = [raw] if isinstance(raw, str) else raw
        if not isinstance(items, list) or not items or len(items) > 32 \
                or not all(isinstance(p, str) and p.strip() for p in items):
            raise Refusal("invalid_write_scope", f"{where}: write_scope must be a path or a list of paths.")
        scopes = []
        for item in items:
            path = scope_path(item, str(base))
            if path is None or path == os.sep:
                raise Refusal("invalid_write_scope", f"{where}: write_scope {item!r} is not a usable path.")
            scopes.append(os.path.realpath(path))
        return tuple(dict.fromkeys(scopes))

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

    def _endpoint_up(self, provider: str | None) -> bool:
        """False only for a named custom endpoint (a ``providers:`` base_url) that does not answer.

        Hosted providers and provider plugins are never probed: Hermes resolves those itself.
        """
        if not provider:
            return True
        url = self.host.endpoint_url(provider)
        if not url:
            return True
        now = self.clock()
        with self._lock:
            cached = self._probes.get(url)
        if cached is not None:
            up, at = cached
            if now - at < (PROBE_TTL_UP_SECONDS if up else PROBE_TTL_DOWN_SECONDS):
                return up
        up = bool(self.host.endpoint_up(url))
        with self._lock:
            self._probes[url] = (up, now)
        return up

    def _pick_route(self, spec: AgentSpec, only_provider: str | None = None) -> AgentSpec:
        """The agent on the first route whose endpoint answers.

        A tier with several routes falls through them in order; the chosen route and the skipped ones
        go into the run's note. With no route up, the spec keeps its first route and ``unreachable`` is
        set, so the run fails at once with a clear error instead of hanging on a dead endpoint.
        """
        routes = spec.routes or ((spec.provider, spec.model),)
        if spec.routes and only_provider is not None:
            # A build without per-launch provider routing can only run routes on the lead's provider.
            runnable = tuple(r for r in routes if not r[0] or r[0] == only_provider)
            if not runnable:
                return spec  # _launch refuses it with provider_unsupported, which explains why
            routes = runnable
        skipped = []
        for provider, model in routes:
            if self._endpoint_up(provider):
                if not skipped:
                    return replace(spec, provider=provider, model=model) if spec.routes else spec
                return replace(spec, provider=provider, model=model, skipped_routes=tuple(skipped))
            skipped.append(f"{provider}/{model}" if provider else model)
        return replace(spec, skipped_routes=tuple(skipped), unreachable=True)

    def _launch(self, spec: AgentSpec, task: dict, parent: Any, parent_provider: str, session_id: str,
                settings: dict, features: dict) -> Run:
        run = Run(run_id="ag_" + uuid.uuid4().hex[:10], agent=spec.name, owner_session_id=session_id,
                  goal=task["goal"], readonly=spec.readonly, write_scope=task.get("write_scope", ()),
                  owner_ref=_weak(parent), model=spec.model, started_at=self.wall())
        self._track(run)
        if spec.unreachable:
            return self._fail(run, "endpoint_unavailable",
                              f"No route for agent '{spec.name}' is reachable: "
                              f"{', '.join(spec.skipped_routes)} did not answer. Start the server, add a "
                              "fallback route to its tier, or use another agent.")
        if spec.skipped_routes:
            run.fallback = f"{', '.join(spec.skipped_routes)} unreachable"
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
        intent = _Intent(ChildPolicy.for_spec(spec, task.get("write_scope", ()), self._mcp_reader(spec, settings)))
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
                         "blocked: investigate and report instead."
                         + (" MCP tools that only read (marked read-only in this profile) work; the others are "
                            "blocked." if self._mcp_reader(spec, settings) is not None else ""))
        if task.get("write_scope"):
            rules.append("- Your write scope: " + ", ".join(task["write_scope"]) + ". Change files only there, "
                         "and always pass ABSOLUTE paths to write_file and patch (relative paths are blocked). "
                         "Edits outside the scope are blocked, and other agents may be editing the rest of the tree "
                         "right now. Do not use shell commands to write outside it either.")
        elif spec.tools is not None:
            rules.append("- Only your configured tools work; other tool calls are blocked.")
        rules.append("- Finish with ONE compact report for the lead: findings first (with file:line where it "
                     "applies), then what you checked, then open questions. No transcripts, no full file dumps.")
        rules.extend(task.get("rules") or ())  # plan stages: the items/verdict block comes last
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

    def _mcp_reader(self, spec: AgentSpec, settings: dict) -> Callable[[str], bool] | None:
        """For a read-only agent on the default tool list: which MCP tools it may call on top of it."""
        if not spec.mcp_reads or not settings.get("readonly_mcp"):
            return None
        marked = tuple(settings.get("readonly_mcp_tools") or ())
        host = self.host

        def read_only(name: str) -> bool:
            return any(fnmatch.fnmatchcase(name, g) for g in marked) or host.mcp_read_only(name) is True
        return read_only

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

    def _wait(self, runs: list, stop_at: float, plans: tuple = (), step: Callable | None = None) -> str | None:
        """Poll until every run (and plan) is finished or ``stop_at`` (clock time) passes; returns why it
        stopped early. ``step`` advances the plans: the only place a plan moves on."""
        pending = [run for run in runs if run.status == "running"]
        while pending or any(plan.status == "running" for plan in plans):
            pending = [run for run in pending if not self._poll(run)]
            for plan in plans:
                if plan.status == "running" and step is not None:
                    step(plan)
            if (not pending and not any(plan.status == "running" for plan in plans)) or self.clock() >= stop_at:
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
        if run.provider:
            item["provider"] = run.provider
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
        if run.fallback:
            item["fallback"] = run.fallback
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
            notes.append(f"{len(roster.problems)} agent file problem(s); /crew lists them.")
        if notes:
            out["config_problems"] = notes[:5]
        return out

    # ------------------------------------------------------------------ plans

    def _run_plan(self, args: dict, parent: Any, session_id: str, settings: dict, problems: list,
                  roster: Roster, started: float) -> dict:
        if args.get("tasks") is not None:
            raise Refusal("invalid_plan", "Pass either tasks or plan, not both.")
        try:
            parsed = agents_plan.parse_plan(args.get("plan"), settings["plan_max_items"])
        except PlanError as exc:
            raise Refusal(exc.code, str(exc)) from None
        for stage in parsed["stages"]:
            spec, task = self._parse_task(stage["task"], stage["where"], roster)
            if stage["retry"] and spec.readonly:
                raise Refusal("invalid_plan", f"{stage['where']}: retry is for writer stages, and '{spec.name}' "
                                              "is read-only.")
            if stage["verify"] and not spec.readonly:
                raise Refusal("invalid_plan", f"{stage['where']}: a verify stage needs a read-only agent, "
                                              f"and '{spec.name}' can change files.")
            for part in stage["split"] or ():
                if len(task["goal"]) + len(part) + 14 > MAX_GOAL_CHARS:
                    raise Refusal("invalid_goal", f"{stage['where']}: goal plus split part {part[:40]!r} is longer "
                                                  f"than {MAX_GOAL_CHARS} characters.")
            stage["spec"], stage["template"] = spec, task
        if settings["max_parallel_writers"] == 0 and any(not st["spec"].readonly for st in parsed["stages"]):
            raise Refusal("writers_disabled", "The operator turned off agents that change files "
                                              "(max_parallel_writers: 0); this plan has a writer stage.")
        features = self.features()
        with self._admit:
            with self._lock:
                busy = [p for p in self._plans.values() if p.status == "running" and self._owns(p, session_id, parent)]
            if busy:
                raise Refusal("plan_running", f"Plan {busy[0].plan_id} is still running in this conversation. Collect "
                                              "it with agents_status(plan_id=...) or cancel it first.")
            plan = Plan(plan_id="pl_" + uuid.uuid4().hex[:10], owner_session_id=session_id, owner_ref=_weak(parent),
                        stages=parsed["stages"], started_at=self.clock(), stage_started=self.clock())
            self._track_plan(plan)
        with plan.lock:
            self._start_stage(plan, parent, settings, features)
        early = self._wait([], started + self._wait_budget(args.get("wait_seconds"), settings), plans=(plan,),
                           step=lambda p: self._step(p, parent, settings, features))
        return self._plan_report(plan, problems, roster, early)

    def _status_plan(self, args: dict, parent: Any, session_id: str, settings: dict, problems: list,
                     roster: Roster, started: float) -> dict:
        plan_id = args.get("plan_id")
        with self._lock:
            plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
        if plan is None or not self._owns(plan, session_id, parent):
            raise Refusal("unknown_plan", f"No plan {plan_id!r} in this conversation.")
        plan.owner_session_id = session_id or plan.owner_session_id
        item_id = args.get("item")
        if item_id is not None:
            item = plan.items.get(item_id) if isinstance(item_id, str) else None
            if item is None:
                raise Refusal("unknown_item", f"Plan {plan.plan_id} has no item {item_id!r}.")
            return {"ok": True, "plan_id": plan.plan_id, "item": self._item_trail(item)}
        if args.get("cancel") and plan.status == "running":
            reason = clip(args.get("reason") or "plan cancelled by the lead agent", 500)
            with plan.lock:  # waits for an advancer in another call: no wave can launch after this
                for run, _ in plan.current:
                    self._cancel(run, reason)
                plan.cancelled_runs.extend(run for run, _ in plan.current)
                plan.queue.clear()
                self._end_plan(plan, "cancelled", note=reason)
        features = self.features()
        budget = started + self._wait_budget(args.get("wait_seconds", 0), settings)
        early = self._wait(list(plan.cancelled_runs), budget, plans=(plan,),
                           step=lambda p: self._step(p, parent, settings, features))
        return self._plan_report(plan, problems, roster, early)

    def _track_plan(self, plan: Plan) -> None:
        with self._lock:
            self._plans[plan.plan_id] = plan
            while len(self._plans) > MAX_TRACKED_PLANS:
                victim = next((pid for pid, p in self._plans.items() if p.status != "running" and p.reported),
                              None) or next((pid for pid, p in self._plans.items() if p.status != "running"), None)
                if victim is None:
                    break
                self._plans.pop(victim)

    def _start_stage(self, plan: Plan, parent: Any, settings: dict, features: dict) -> None:
        """Build the current stage's runs (queued) and launch the first wave."""
        stage = plan.stages[plan.stage_index]
        plan.stage_started = self.clock()
        plan.current, plan.done = [], []
        spec = self._pick_route(stage["spec"], None if features["provider"] else
                                str(getattr(parent, "provider", "") or ""))
        template = dict(stage["template"])
        rules = [agents_plan.VERIFY_RULE] if stage["verify"] else \
            [agents_plan.ITEMS_RULE] if stage["produces"] else []
        if stage["retry"]:
            rules.append(agents_plan.RESULT_RULE)
        template["rules"] = rules
        plan.stage_spec, plan.stage_template, plan.retried = spec, template, 0
        if stage["split"]:
            for part in stage["split"]:
                plan.queue.append((spec, {**template, "goal": f"{template['goal']}\n\nYour part: {part}"}, []))
        elif stage["for_each"]:
            ids = [i for i in plan.outputs.get(stage["for_each"], []) if plan.items[i]["status"] != "refused_scope"]
            workdir = str(template.get("workdir") or self.host.cwd())
            eligible = []
            for item_id in ids:
                item = plan.items[item_id]
                if not spec.readonly and item["files"] and agents_plan.plan_scope(item["files"], workdir) is None:
                    item["status"], item["reason"] = "refused_scope", "files outside the workdir or too broad"
                    continue
                eligible.append(item_id)
            # Writers: items whose files overlap go into ONE run, so fixes in the same file never wait for
            # each other or conflict; disjoint groups still run in parallel. Groups are never split.
            groups = self._group_by_files(plan, eligible, workdir) if not spec.readonly else [[i] for i in eligible]
            batch: list = []
            for group in groups:
                if batch and len(batch) + len(group) > stage["per_run"]:
                    plan.queue.append(self._item_task(plan, spec, template, batch, workdir))
                    batch = []
                batch.extend(group)
                if len(batch) >= stage["per_run"]:
                    plan.queue.append(self._item_task(plan, spec, template, batch, workdir))
                    batch = []
            if batch:
                plan.queue.append(self._item_task(plan, spec, template, batch, workdir))
        else:
            plan.queue.append((spec, template, []))
        if not plan.queue:  # every item was refused: nothing to run, and nothing to pass on
            for later in plan.stages[plan.stage_index:]:
                plan.stats.append({"name": later["name"], "skipped": True})
            self._end_plan(plan, "succeeded", note=f"stage {stage['name']}: no item passed the scope rule; the "
                                                    "remaining stages were skipped")
            return
        self._launch_wave(plan, parent, settings, features)

    @staticmethod
    def _group_by_files(plan: Plan, ids: list, workdir: str) -> list:
        """Item ids grouped so that items with overlapping file scopes share a group (union-find, stable order).
        Items without files stay on their own: their workdir-wide scope would otherwise swallow every group."""
        scopes = {i: agents_plan.plan_scope(plan.items[i]["files"], workdir) for i in ids}
        parent = {i: i for i in ids}

        def root(i: str) -> str:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        scoped = [i for i in ids if scopes[i]]
        for n, a in enumerate(scoped):
            for b in scoped[n + 1:]:
                if root(a) != root(b) and scopes_overlap(scopes[a], scopes[b]):
                    parent[root(b)] = root(a)
        groups: dict[str, list] = {}
        for i in ids:
            groups.setdefault(root(i), []).append(i)
        return list(groups.values())

    def _item_task(self, plan: Plan, spec: AgentSpec, template: dict, ids: list, workdir: str,
                   previous: str | None = None) -> tuple:
        detail_budget = max(300, 12_000 // max(1, len(ids)))
        items = [{**agents_plan.item_view(plan.items[i]), "detail": clip(plan.items[i]["detail"], detail_budget)}
                 for i in ids]
        # Items first: _compose_context clips the caller context from the end, never the items.
        context = "## Items\n" + json.dumps(items, ensure_ascii=False, indent=1) + \
            ("\n\n## Previous attempt (it failed; fix what it left)\n" + previous if previous else "") + \
            ("\n\n" + template["context"] if template["context"] else "")
        task = {**template, "context": context}
        if not spec.readonly:
            # Item files were checked in _start_stage. An item without files gets the whole workdir: still
            # inside the repository, and overlapping every other scope, so such writers run one at a time.
            root = os.path.realpath(workdir)
            scopes = [agents_plan.plan_scope(plan.items[i]["files"], workdir) or (root,) for i in ids]
            task["write_scope"] = tuple(dict.fromkeys(p for scope in scopes for p in scope))
        return spec, task, list(ids)

    def _launch_wave(self, plan: Plan, parent: Any, settings: dict, features: dict) -> None:
        if not plan.queue or plan.status != "running":
            return
        if self.host.spawn_paused():
            plan.note = "delegation paused by the operator; the next runs wait"
            return
        plan.note = None if plan.note and plan.note.startswith("delegation paused") else plan.note
        parent_provider = str(getattr(parent, "provider", "") or "")
        with self._admit:
            mine = self._live_runs(plan.owner_session_id, parent)
            live_writers = [r for r in mine if not r.readonly]
            room = min(settings["max_live_runs"] - len(mine),
                       settings["max_parallel"] - sum(1 for run, _ in plan.current if run.status == "running"))
            waiting = []
            for spec, task, ids in plan.queue:
                if room <= 0:
                    waiting.append((spec, task, ids))
                    continue
                if not spec.readonly:
                    try:
                        self._check_writers([(spec, task)], live_writers, settings)
                    except Refusal as refusal:
                        if refusal.code == "writers_disabled":
                            plan.queue = []
                            self._end_plan(plan, "failed", error=str(refusal.payload.get("message")))
                            return
                        waiting.append((spec, task, ids))  # does not fit next to the live writers: next wave
                        continue
                run = self._launch(spec, task, parent, parent_provider, plan.owner_session_id, settings, features)
                run.plan_id = plan.plan_id
                plan.current.append((run, ids))
                room -= 1
                if not spec.readonly and run.status == "running":
                    live_writers.append(run)
            plan.queue = waiting
            if waiting and not any(run.status == "running" for run, _ in plan.current):
                blockers = [r.run_id for r in mine if r.plan_id != plan.plan_id]
                plan.note = f"waiting: {len(waiting)} run(s) need a free slot" + (
                    f"; held by runs outside this plan: {', '.join(blockers[:5])}" if blockers else "")
            elif (plan.note or "").startswith("waiting:"):
                plan.note = None

    def _step(self, plan: Plan, parent: Any, settings: dict, features: dict) -> None:
        """Advance unless another call is advancing the same plan right now (then that call does it)."""
        if not plan.lock.acquire(blocking=False):
            return
        try:
            self._advance(plan, parent, settings, features)
        finally:
            plan.lock.release()

    def _advance(self, plan: Plan, parent: Any, settings: dict, features: dict) -> None:
        """One step: poll the stage's runs, launch the next wave, and close the stage when all are done."""
        if plan.status != "running":
            return
        still = []
        for run, ids in plan.current:
            if run.status == "running" and not self._poll(run):
                still.append((run, ids))
            else:
                run.reported = True
                if not self._retry(plan, run, ids):
                    plan.done.append((run, ids))
        plan.current = still
        stage = plan.stages[plan.stage_index]
        if any(self._run_failed(stage, run) for run, _ in plan.done):
            # A failed run fails the stage: launch nothing more (queued retries included; their runs go
            # back to done so their items get a final status), and let the running ones finish.
            for retry_run_ids in [ids for _, _, ids in plan.queue if ids]:
                for item_id in retry_run_ids:
                    plan.items[item_id]["status"] = f"{stage['name']}: not run"
            plan.queue = []
        if plan.queue:
            self._launch_wave(plan, parent, settings, features)
        if plan.current or plan.queue or plan.status != "running":
            return
        self._close_stage(plan, parent, settings, features)

    @staticmethod
    def _run_failed(stage: dict, run: Run) -> bool:
        """A stage run counts as failed: it did not succeed, or (retry stages) it reported failed."""
        return run.status != "succeeded" or bool(stage["retry"]) and \
            agents_plan.parse_result(run.summary)[0] == "failed"

    def _retry(self, plan: Plan, run: Run, ids: list) -> bool:
        """Queue one more attempt for a failed writer run of a retry stage; True when it was queued.

        Retried: a run that succeeded but reported failed (tests still red), or a run that failed while
        working. Never retried: interrupted, cancelled or lost runs (the user stopped it, or its edits may
        have landed), launch refusals (they fail the same way again), or a stage that already has a hard
        failure. Stops early when the reason repeats (no progress). Attempts count per stage.
        """
        stage = plan.stages[plan.stage_index]
        if not stage["retry"] or not ids or plan.stage_spec is None or plan.stage_spec.readonly:
            return False
        if any(self._run_failed(stage, r) for r, _ in plan.done):
            return False  # the stage is failing anyway: do not spend another run
        if run.status == "succeeded":
            result, reason = agents_plan.parse_result(run.summary)
            if result != "failed":
                return False
        elif run.status == "failed" and run.note not in LAUNCH_FAILURES:
            reason = clip(run.error or "failed", 200)
        else:
            return False
        reason = reason or "failed"
        key = stage["name"]
        items = [plan.items[i] for i in ids]
        for item in items:
            item.setdefault("retries", {}).setdefault(key, {"attempts": 1, "last": None})
        if any(item["retries"][key]["attempts"] > stage["retry"] for item in items):
            return False
        if any(item["retries"][key]["last"] == reason for item in items):
            for item in items:
                item["no_progress"] = True
            return False
        for item in items:
            item["retries"][key]["attempts"] += 1
            item["retries"][key]["last"] = reason
            item["attempts"] = item["retries"][key]["attempts"]
            item["trail"].append({"stage": stage["name"], "run_id": run.run_id, "status": "retry",
                                  "summary": clip(run.summary or run.error or "", PLAN_TRAIL_CHARS)})
        workdir = str(plan.stage_template.get("workdir") or self.host.cwd())
        previous = clip(run.summary or run.error or reason, 3_000)
        plan.queue.append(self._item_task(plan, plan.stage_spec, plan.stage_template, ids, workdir, previous))
        plan.retried += 1
        return True

    def _close_stage(self, plan: Plan, parent: Any, settings: dict, features: dict) -> None:
        stage = plan.stages[plan.stage_index]
        name, done = stage["name"], plan.done
        stat: dict[str, Any] = {"name": name, "runs": len(done) + plan.retried,
                                "seconds": round(self.clock() - plan.stage_started, 1)}
        if plan.retried:
            stat["retried"] = plan.retried
        for run, ids in done:
            for item_id in ids:
                plan.items[item_id]["trail"].append(
                    {"stage": name, "run_id": run.run_id, "status": run.status,
                     "summary": clip(run.summary or run.error or "", PLAN_TRAIL_CHARS)})
        failed = [(run, ids) for run, ids in done if self._run_failed(stage, run)]
        if failed:
            bad = {r.run_id for r, _ in failed}
            for r, ids in done:
                for item_id in ids:
                    if plan.items[item_id]["status"] != f"{name}: not run":
                        plan.items[item_id]["status"] = f"{name}: failed" if r.run_id in bad else f"{name}: done"
            run = failed[0][0]
            why = agents_plan.parse_result(run.summary)[1] if run.status == "succeeded" else clip(run.error or "", 400)
            if any(plan.items[i].get("no_progress") for _, ids in failed for i in ids):
                why = (why + "; " if why else "") + "stopped: the retry failed the same way"
            stat["failed"] = len(failed)
            plan.stats.append(stat)
            verb = "reported failed" if run.status == "succeeded" else run.status
            self._end_plan(plan, "failed", error=f"stage {name}: run {run.run_id} ({run.agent}) {verb}: "
                                                 f"{clip(why or run.error or 'no error message', 600)}")
            return
        if stage["produces"]:
            found, seen = [], set()
            for run, _ in done:
                items = agents_plan.parse_items(run.summary)
                if items is None:
                    plan.stats.append(stat)
                    tail = (run.summary or "")[-600:]
                    self._end_plan(plan, "failed", error=f"stage {name}: run {run.run_id} ({run.agent}) ended without "
                                                         f"the items block (items_missing). End of its report: {tail}")
                    return
                for item in items:
                    key = (item["title"].lower(), tuple(item["files"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    item_id = f"{name}-{len(found) + 1}"
                    found.append(item_id)
                    plan.items[item_id] = {**item, "id": item_id, "status": "found", "reason": None,
                                           "trail": [{"stage": name, "run_id": run.run_id, "status": run.status,
                                                      "summary": clip(run.summary or "", PLAN_TRAIL_CHARS)}]}
            if len(found) > settings["plan_max_items"]:
                plan.stats.append({**stat, "items": len(found)})
                titles = "; ".join(plan.items[i]["title"][:80] for i in found[:30])
                self._end_plan(plan, "failed", error=f"stage {name} found {len(found)} items, more than plan_max_items "
                                                     f"({settings['plan_max_items']}). Split the job. Items: {titles}")
                return
            plan.outputs[name] = found
            stat["items"] = len(found)
        elif stage["verify"]:
            confirmed: list = []
            rejected = 0
            for run, ids in done:
                ok, refused = agents_plan.parse_verdicts(run.summary, [plan.items[i] for i in ids])
                for item_id in ids:
                    item = plan.items[item_id]
                    if item_id in ok:
                        item["status"] = "confirmed"
                        confirmed.append(item_id)
                    else:
                        item["status"], item["reason"] = "rejected", refused.get(item_id, "verdict_missing")
                        rejected += 1
            plan.outputs[name] = confirmed
            stat.update(confirmed=len(confirmed), rejected=rejected)
        elif stage["for_each"]:
            passed = []
            for _, ids in done:
                for item_id in ids:
                    plan.items[item_id]["status"] = f"{name}: done"
                    passed.append(item_id)
            plan.outputs[name] = passed
            stat["succeeded"] = len(done)
        else:
            stat["succeeded"] = len(done)
        plan.stats.append(stat)
        if done:
            plan.final = self._stage_text(done)
        plan.stage_index += 1
        if plan.stage_index >= len(plan.stages):
            self._end_plan(plan, "succeeded")
            return
        nxt = plan.stages[plan.stage_index]
        if nxt["for_each"] and not plan.outputs.get(nxt["for_each"]):
            for later in plan.stages[plan.stage_index:]:
                plan.stats.append({"name": later["name"], "skipped": True})
            self._end_plan(plan, "succeeded", note=f"stage {nxt['for_each']} passed no items on; the remaining "
                                                    "stages were skipped")
            return
        self._start_stage(plan, parent, settings, features)

    @staticmethod
    def _stage_text(done: list) -> str | None:
        if len(done) == 1:
            return done[0][0].summary
        parts = [f"### {run.agent} {run.run_id}\n{run.summary or ''}" for run, _ in done]
        return "\n\n".join(parts) or None

    def _end_plan(self, plan: Plan, status: str, error: str | None = None, note: str | None = None) -> None:
        plan.status = status
        plan.error = error or plan.error
        plan.note = note or plan.note
        plan.finished_at = self.clock()

    def _item_trail(self, item: dict) -> dict:
        out = {k: item[k] for k in ("id", "title", "files", "detail", "status") if item.get(k) not in (None, "", [])}
        if item.get("reason"):
            out["reason"] = item["reason"]
        budget = PLAN_REPORT_CHARS * 2
        trail = []
        for step in item["trail"]:
            entry = dict(step)
            entry["summary"] = clip(entry["summary"], max(500, budget // max(1, len(item["trail"]))))
            trail.append(entry)
        out["trail"] = trail
        return out

    def _plan_report(self, plan: Plan, problems: list, roster: Roster, early: str | None = None) -> dict:
        body: dict[str, Any] = {"plan_id": plan.plan_id, "status": plan.status}
        end = plan.finished_at if plan.finished_at is not None else self.clock()
        body["duration_seconds"] = round(end - plan.started_at, 1)
        stages = list(plan.stats)
        if plan.status == "cancelled":
            stages += [{"name": st["name"], "cancelled": True} for st in plan.stages[len(plan.stats):]]
        if plan.status == "running":
            stage = plan.stages[plan.stage_index]
            total = len(plan.current) + len(plan.done) + len(plan.queue)
            stages.append({"name": stage["name"], "runs": total, "finished": len(plan.done), "running": True})
            stages += [{"name": s["name"], "pending": True} for s in plan.stages[plan.stage_index + 1:]]
            body["progress"] = (f"stage {plan.stage_index + 1}/{len(plan.stages)} '{stage['name']}': "
                                f"{len(plan.done)}/{total} runs finished, {len(plan.items)} items so far")
        body["stages"] = stages
        items = []
        for item in plan.items.values():
            line = {"id": item["id"], "title": clip(item["title"], 200), "status": item["status"]}
            if item.get("attempts", 1) > 1:
                line["attempts"] = item["attempts"]
            if item.get("reason"):
                line["reason"] = clip(item["reason"], 200)
            items.append(line)
        if plan.error:
            body["error"] = clip(plan.error, 1_500)
        if plan.note:
            body["note"] = clip(plan.note, 500)
        while items and len(json.dumps(items, ensure_ascii=False)) > PLAN_REPORT_CHARS - 2_500:
            items.pop()
        if items:
            body["items"] = items
        if len(items) < len(plan.items):
            body["more_items"] = len(plan.items) - len(items)
        if plan.status == "succeeded" and plan.final:
            while body.get("items") and PLAN_REPORT_CHARS - len(json.dumps(body, ensure_ascii=False)) < 2_000:
                body["items"].pop()
                body["more_items"] = body.get("more_items", 0) + 1
            room = PLAN_REPORT_CHARS - len(json.dumps(body, ensure_ascii=False)) - 40
            if room > 200:
                body["final"] = clip(plan.final, room)
        out: dict[str, Any] = {"ok": True, "plan": body}
        if plan.status == "running" and early == "interrupted":
            out["next"] = "Stopped waiting after an interrupt; the plan is paused until agents_status(plan_id=...)."
        elif plan.status == "running":
            out["next"] = ("The plan keeps going only while you wait: call agents_status with plan_id and "
                           "wait_seconds (a few minutes) to continue it.")
            if early == "new_message":
                out["next"] = "Stopped waiting because a new user message arrived. " + out["next"]
        else:
            plan.reported = True
            out["next"] = "agents_status with plan_id and item gives one item's full trail."
        notes = list(problems or [])
        if roster is not None and roster.problems:
            notes.append(f"{len(roster.problems)} agent file problem(s); /crew lists them.")
        if notes:
            out["config_problems"] = notes[:5]
        return out

    # ------------------------------------------------------------------ text surfaces

    def roster_text(self, raw_args: str = "") -> str:
        """The /crew command: a status table for the operator, or one agent's details with /crew <name>."""
        settings, problems, roster = self.config()
        name = (raw_args or "").strip().lower()
        if name:
            spec = roster.agents.get(name)
            if spec is None:
                return f"No agent named {name!r}. Agents: {', '.join(sorted(roster.agents)) or 'none'}."
            return self._agent_detail(spec)
        features = self.features()
        if not roster.agents:
            return "Crew: no agents configured.\n" + self._crew_footer(settings, problems, roster, features)
        rows = []
        for spec in sorted(roster.agents.values(), key=lambda s: s.name):
            routes = spec.routes or ((spec.provider, spec.model),)
            access = "read-only" if spec.readonly else "writes"
            for n, (provider, model) in enumerate(routes):
                route = f"{model or 'lead model'} @ {provider or 'lead provider'}"
                state = "" if self._endpoint_up(provider) else "  (down)"
                if n == 0:
                    rows.append((spec.name, spec.tier or "-", route + state, access))
                else:
                    rows.append(("", "", f"└ then {route}{state}", ""))
        head = ("AGENT", "TIER", "MODEL @ PROVIDER", "ACCESS")
        widths = [max(len(r[i]) for r in rows + [head]) for i in range(3)]

        def line(cells: tuple) -> str:
            return "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells[:3])) + "  " + cells[3]

        out = [f"Crew: {len(roster.agents)} agents", "", line(head)]
        out += [line(r).rstrip() for r in rows]
        out.append("")
        out.append(self._crew_footer(settings, problems, roster, features))
        return "\n".join(out)

    def _crew_footer(self, settings: dict, problems: list, roster: Roster, features: dict) -> str:
        with self._lock:
            running = sum(1 for r in self._runs.values() if r.status == "running")
            plans = sum(1 for p in self._plans.values() if p.status == "running")
        yes = {True: "yes", False: "no"}
        out = ["Status",
               f"  Runs in progress  {running}" + (f" (+{plans} plan{'s' if plans != 1 else ''})" if plans else ""),
               f"  Hermes build      per-agent provider: {yes[features['provider']]}, "
               f"per-agent effort: {yes[features['effort']]}",
               "",
               "Agent folders (a later one overrides an earlier one)"]
        for n, folder in enumerate(roster.folders, 1):
            out.append(f"  {n}. {folder}" + ("  (built-in)" if n == 1 else ""))
        issues = [f"  problem: {p}" for p in (problems + roster.problems)[:20]] + \
            [f"  warning: {w}" for w in roster.warnings[:10]]
        out += ["", "Problems" if issues else "Problems: none"] + issues
        out += ["", "Details of one agent: /crew <agent>"]
        return "\n".join(out)

    def _agent_detail(self, spec: AgentSpec) -> str:
        out = [f"{spec.name}  ({'read-only' if spec.readonly else 'can change files'})", ""]
        if spec.tier:
            out.append(f"  Tier         {spec.tier}")
        routes = spec.routes or ((spec.provider, spec.model),)
        for n, (provider, model) in enumerate(routes):
            label = "Route" if len(routes) == 1 else f"Route {n + 1}"
            state = "up" if self._endpoint_up(provider) else "down"
            out.append(f"  {label:<12} {model or 'lead model'} @ {provider or 'lead provider'}  ({state})")
        if spec.effort:
            out.append(f"  Effort       {spec.effort}")
        if spec.tools is not None:
            out.append("  Tools        " + ", ".join(spec.tools))
        out.append(f"  Source       {spec.source}")
        if spec.use_when:
            out += ["", "Use when", "  " + spec.use_when]
        if spec.description:
            out += ["", "Description", *("  " + row for row in textwrap.wrap(spec.description, 96))]
        return "\n".join(out)

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


def _lookup_key(tool_name: str, args: Any) -> str:
    """What one scouting call looked at, so repeated reads of the same target count once per turn."""
    if not isinstance(args, dict):
        return tool_name
    if tool_name == "read_file":
        target = args.get("path")  # offset and limit page through the same file
    elif tool_name == "search_files":
        target = f"{args.get('pattern')}\0{args.get('path')}\0{args.get('target')}"
    elif tool_name == "web_extract":
        urls = args.get("urls")
        target = "\0".join(sorted(map(str, urls))) if isinstance(urls, list) else urls
    elif tool_name == "web_search":
        target = args.get("query")
    elif tool_name == "terminal":
        target = " ".join(str(args.get("command") or "").split())
    else:
        target = None
    return f"{tool_name}\0{str(target)[:500]}"


def _is_scouting(tool_name: str, args: Any) -> bool:
    if tool_name in SCOUT_TOOLS:
        return True
    if tool_name == "terminal" and isinstance(args, dict):
        return is_read_command(str(args.get("command") or ""))
    return False


def is_read_command(command: str) -> bool:
    """True when every segment of a shell command only reads (cd prefixes allowed, at least one read)."""
    if not command.strip() or len(command) > 4_000 or "\n" in command.strip() or "`" in command or "$(" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEGMENT_BREAKS:
            segments.append([])
        else:
            segments[-1].append(token)
    reads = 0
    for segment in segments:
        if not segment:
            continue
        words = _strip_redirects(segment)
        if words is None:
            return False
        if not words:
            continue
        if words[0] == "cd":
            continue
        if not _reads_only(words):
            return False
        reads += words[0].rsplit("/", 1)[-1] not in PRINT_ONLY
    return reads > 0


def _strip_redirects(segment: list[str]) -> list[str] | None:
    """The words of one segment without harmless redirects; None when it writes or reads stdin from a file."""
    words: list[str] = []
    i = 0
    while i < len(segment):
        token = segment[i]
        if token in (">", ">>", ">|", "&>", "&>>"):
            target = segment[i + 1] if i + 1 < len(segment) else ""
            if target not in _HARMLESS_TARGETS:
                return None
            if words and words[-1].isdigit():
                words.pop()  # the fd number of 2>/dev/null
            i += 2
            continue
        if token in (">&", "<&"):
            if words and words[-1].isdigit():
                words.pop()  # 2>&1
            i += 2
            continue
        if token in ("<", "<<", "<<<"):
            i += 2  # input redirection reads; it never writes
            continue
        words.append(token)
        i += 1
    return words


def _reads_only(words: list[str]) -> bool:
    program = words[0].rsplit("/", 1)[-1]
    if program not in READ_PROGRAMS:
        return False
    rest = words[1:]
    if program == "sed":
        return "-n" in rest and not any(w == "-i" or w.startswith(("-i", "--in-place")) for w in rest)
    if program == "find":
        return not FIND_WRITE_ACTIONS & set(rest)
    if program == "git":
        at = 0
        while at < len(rest) and rest[at].startswith("-"):
            if rest[at] == "-c":
                return False  # -c can set any config, including programs git runs
            at += 2 if rest[at] in ("-C", "--git-dir", "--work-tree") else 1  # global options with a value
        sub, after = (rest[at], rest[at + 1:]) if at < len(rest) else ("", [])
        if sub not in GIT_READ_COMMANDS or any(w.startswith("--output") for w in rest):
            return False
        # `git branch NAME` creates a branch; only listing flags count as reading.
        return sub != "branch" or all(w in ("-a", "-r", "-v", "-vv", "--list", "--all") for w in after)
    if program == "sort":
        return not any(w == "-o" or w.startswith("--output") for w in rest)
    return True


def is_error_result(result: str, status: Any = "") -> bool:
    """True for a failed tool call: Hermes' own status, or a JSON result that reports an error."""
    if str(status or "").lower() in ("error", "failed", "blocked"):
        return True
    text = result.lstrip()
    if not text.startswith("{") or len(text) > MAX_ERROR_PARSE_CHARS:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("error")) or data.get("success") is False or data.get("ok") is False


PLAN_HINT = ("[hermes-agent-crew] Second fan-out of agents in this turn. If the next step again repeats per "
             "finding, pass agents_run one plan for the remaining stages (for_each over the findings, verify, "
             "fix with retry, check): it runs them and returns one compact result instead of every report.")


def _report_count(result: Any) -> int:
    """How many finished reports an agents_run result carried (0 when it is not one)."""
    if not isinstance(result, str) or not result.startswith("{") or len(result) > 400_000:
        return 0
    try:
        data = json.loads(result)
    except ValueError:
        return 0
    results = data.get("results") if isinstance(data, dict) else None
    return sum(1 for r in results if isinstance(r, dict) and r.get("summary")) if isinstance(results, list) else 0


def nudge_text(count: int, scouts: list) -> str:
    names = ", ".join(scouts[:4])
    return (f"[hermes-agent-crew] {count} separate reads/searches by hand in this turn. If more lookups follow, "
            f"send them to read-only agents ({names}) in ONE agents_run call, one task per question or area, "
            "and keep your own context for decisions. Keep going yourself if you are nearly done.")


def model_label(spec: AgentSpec) -> str:
    """What runs the agent, short: ``fast: haiku`` for a tier agent, the bare model otherwise."""
    model = " or ".join(dict.fromkeys(m for _, m in spec.routes)) if spec.routes else (spec.model or "lead's model")
    return f"{spec.tier}: {model}" if spec.tier else model


def agent_label(spec: AgentSpec) -> str:
    # The provider stays out of this label: it is in every turn's prompt, and /crew shows it.
    flags = [model_label(spec)] + (["read-only"] if spec.readonly else [])
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
    "Agents: named subagents for token-efficient delegation. You are the lead: you plan, decide and verify; "
    "agents do bounded work in their own context and return compact reports, so your context stays small.",
    "Roster:",
]
_PROMPT_TAIL = [
    'Call agents_run with {"tasks": [{"agent": "<name>", "goal": "...", "context": "...", "workdir": '
    '"/abs/path/to/repo"}]}. If it is a deferred tool, call it through tool_call with name "agents_run". '
    "Several tasks in one call run in parallel. Set workdir to the folder the work is in; it defaults to this "
    "session's working directory.",
    "When to use it:",
    "- Swarm early: when a task needs more than a handful of reads or searches (unfamiliar code, several "
    "areas, docs), send read-only scouts in ONE agents_run call, one task per question or area, before you "
    "read it all yourself.",
    "- Background work: a builder or a long review can run with wait_seconds 0 while you do your own part; "
    "collect it later with agents_status.",
    "- Parallel builders: give each writer a write_scope (the files or folders it may change, disjoint per "
    "writer) and they run side by side; without one, writers run one at a time.",
    "- Senior review on call: ask the reviewer (1) before you lock a multi-file plan, (2) when the same test or "
    "build error fails twice, (3) before you call non-trivial work done; pass attach_diff: true so it reads "
    "the diff itself.",
    "- Mechanical fan-out: whole-document checks and extractions (language or lint MCP checks over a file, "
    "page extraction) go to one worker per file or URL, in parallel, over the COMPLETE input. Do not sample "
    "excerpts inline to save calls; a short text (under ~1,000 characters) you may check inline.",
    "- Plans for big jobs: when a task splits into many independent items (roughly 5 or more files, findings "
    "or pages) that each go through the same steps, pass agents_run a plan (stages such as scan, verify, fix, "
    "check) instead of chaining calls; you read only the final result. Small jobs stay tasks or your own work.",
    "- Skip agents for small lookups: every agent starts with a fresh context.",
    "- Read-only agents have no shell: they cannot run scripts, curl, probes or tests, and cannot save notes. "
    "They can call the profile's read-only MCP tools (reports, docs, search APIs), so send big MCP reads to "
    "them. Work that must execute goes to a writer with a write_scope or stays with you.",
    "Agents cannot see this conversation, so give each one a complete goal and context (paths, constraints, "
    "what you already know, and the paths of notes or knowledge-base pages you know are relevant, so they read "
    "those instead of searching). Treat their reports as evidence to check, not as facts. Runs that outlive the "
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
            block.append(f"- … {more} more (/crew lists them)")
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
    return ("Run named subagents from the operator's roster (each has a fixed model and tool set; read-only "
            "agents cannot change anything or run commands) on self-contained tasks, in parallel, and get their "
            "compact final reports. Agents cannot see this conversation: give each task a full goal and context. "
            "Results that take longer than wait_seconds come back as run_ids for agents_status. Agents at startup: "
            f"{names}.")
