"""Per-child tool policy, enforced through Hermes' ``pre_tool_call`` hook.

Every tool call a subagent makes carries ``task_id == subagent_id``: the id Hermes reports in the
``subagent_start`` hook and on the lifecycle handle. The guard therefore tells this plugin's
children apart from the parent and from other subagents with one dictionary lookup, and never
touches tool calls it does not own.
"""
from __future__ import annotations

import fnmatch
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# Agents never start other agents: the X-shaped "lead plans, agents execute" split stays flat, and a
# child that could fan out would bypass the operator's limits.
OWN_TOOLS = frozenset({"agents_run", "agents_status", "delegate_task"})
# Tool search catalog reads. With tool search on, a child's MCP tools are deferred, so even an agent
# with an explicit tool list needs these to reach them. They only list and describe tools the child's
# own session may already use; the real call (through tool_call) is checked under its own name.
CATALOG_TOOLS = frozenset({"tool_search", "tool_describe"})
_GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class ChildPolicy:
    agent: str
    readonly: bool
    allowed: frozenset | None = None  # exact tool names; None = no per-tool limit
    globs: tuple = ()

    @classmethod
    def for_spec(cls, spec: Any) -> ChildPolicy:
        if spec.tools is None:
            return cls(spec.name, spec.readonly)
        exact = frozenset(t for t in spec.tools if not _GLOB_CHARS & set(t))
        globs = tuple(t for t in spec.tools if _GLOB_CHARS & set(t))
        return cls(spec.name, spec.readonly, exact, globs)

    def decide(self, tool_name: str) -> str | None:
        """Block message for a tool call, or None to let it through."""
        if tool_name in OWN_TOOLS:
            return (f"heremes-codex-agents: agent '{self.agent}' cannot start other agents. "
                    "Finish the task yourself and report back.")
        if self.allowed is None or tool_name in CATALOG_TOOLS:
            return None
        if tool_name in self.allowed or any(fnmatch.fnmatchcase(tool_name, g) for g in self.globs):
            return None
        usable = ", ".join(sorted(self.allowed)[:14] + list(self.globs)) or "none"
        if tool_name == "tool_call":
            # Hermes unwraps a valid single tool_call before this check and checks the real tool; what lands
            # here is a connector batch, an out-of-scope tool or arguments that failed the schema probe.
            return (f"heremes-codex-agents: this tool_call was blocked for agent '{self.agent}': it is a batch, or "
                    "it did not resolve to a tool this agent may use. Send one entry per tool_call, with the exact "
                    f"name and arguments from tool_describe. Usable tools: {usable}.")
        if self.readonly:
            return (f"heremes-codex-agents: agent '{self.agent}' is read-only, so '{tool_name}' is blocked. "
                    f"Usable tools: {usable}. Investigate and report instead of changing anything.")
        return (f"heremes-codex-agents: agent '{self.agent}' may only use its configured tools, so "
                f"'{tool_name}' is blocked. Usable tools: {usable}.")


class PolicyRegistry:
    """Thread-safe, bounded map of subagent id -> policy.

    Entries are never dropped when a run ends, only when the map is full (oldest first), so a
    late tool call from a finished child cannot slip through.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, ChildPolicy] = OrderedDict()
        self._max = max_entries

    def register(self, subagent_id: str, policy: ChildPolicy) -> None:
        with self._lock:
            self._items[subagent_id] = policy
            self._items.move_to_end(subagent_id)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def get(self, subagent_id: str) -> ChildPolicy | None:
        with self._lock:
            return self._items.get(subagent_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
