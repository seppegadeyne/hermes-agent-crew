"""The only place this plugin touches Hermes.

Documented plugin surfaces only: ``ctx.get_config``, ``ctx.subagent_lifecycle``, the public
``agent.subagent_lifecycle`` module, ``hermes_constants.get_hermes_home``,
``agent.runtime_cwd.resolve_agent_cwd`` and ``tools.registry``. Three read-only probes
(spawn pause, the sequential tool deadline and the interrupt flag) degrade to "unknown" when a
Hermes build moves them. Tests replace this class with a fake.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
DEFAULT_TOOL_DEADLINE_SECONDS = 420.0
_INVALID = object()


def parse_seconds(raw: Any) -> Any:
    """A timeout value: float seconds, None for "no cap" (0 or negative), _INVALID when unusable."""
    if raw is None or isinstance(raw, bool):
        return _INVALID
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _INVALID
    if value != value:  # NaN
        return _INVALID
    return value if value > 0 else None


class HermesHost:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    @property
    def isolated(self) -> bool:
        """True inside a ``plugins.isolation: host`` child process, where launches cannot work."""
        return os.environ.get(HOST_PROCESS_ENV, "").strip().lower() not in ("", "0", "false")

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.ctx.get_config(key, default)

    @property
    def lifecycle(self) -> Any:
        return self.ctx.subagent_lifecycle

    @staticmethod
    def home() -> Path:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())

    @staticmethod
    def cwd() -> Path:
        try:
            from agent.runtime_cwd import resolve_agent_cwd
            return Path(resolve_agent_cwd())
        except Exception:
            return Path(os.getcwd())

    @staticmethod
    def parent() -> Any:
        """The agent whose turn is running (Hermes binds it for the turn), or None."""
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            return get_active_subagent_parent()
        except Exception:
            return None

    @staticmethod
    def request_fields() -> frozenset:
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest
            return frozenset(field.name for field in dataclasses.fields(SubagentLaunchRequest))
        except Exception:
            return frozenset()

    @staticmethod
    def make_request(**kwargs: Any) -> Any:
        from agent.subagent_lifecycle import SubagentLaunchRequest
        return SubagentLaunchRequest(**kwargs)

    @staticmethod
    def toolset_for_tool(name: str) -> str | None:
        try:
            from tools.registry import registry
            return registry.get_toolset_for_tool(name)
        except Exception:
            return None

    @staticmethod
    def spawn_paused() -> bool:
        try:
            from tools.delegate_tool_registry import is_spawn_paused
            return bool(is_spawn_paused())
        except Exception:
            return False

    @staticmethod
    def tool_deadline() -> float | None:
        """Seconds Hermes lets one sequential tool call run before it abandons the wait (None = no cap).

        Same precedence as Hermes: ``timeouts.tools.sequential_call``, then
        ``timeouts.tools.concurrent_batch``, then ``HERMES_CONCURRENT_TOOL_TIMEOUT_S``, then 420 s.
        """
        try:
            from hermes_cli.config import load_config_readonly
            timeouts = (load_config_readonly() or {}).get("timeouts")
            tools = timeouts.get("tools") if isinstance(timeouts, dict) else None
            tools = tools if isinstance(tools, dict) else {}
        except Exception:
            tools = {}
        for raw in (tools.get("sequential_call"), tools.get("concurrent_batch"),
                    os.environ.get("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "").strip() or None):
            seconds = parse_seconds(raw)
            if seconds is not _INVALID:
                return seconds
        return DEFAULT_TOOL_DEADLINE_SECONDS

    @staticmethod
    def interrupted() -> bool:
        """True after /stop asked the current tool call to stop."""
        try:
            from tools.interrupt import is_interrupted
            return bool(is_interrupted())
        except Exception:
            return False

    @staticmethod
    def in_child_context() -> bool:
        """True while code runs for a delegated child (second guard next to the parent's depth)."""
        try:
            from agent.delegation_context import is_delegated_child_context
            return bool(is_delegated_child_context())
        except Exception:
            return False

    @staticmethod
    def take_yield() -> bool:
        """True (once) when a new user message asked the running tool to hand back control."""
        try:
            import threading

            from tools.interrupt import consume_yield
            return bool(consume_yield(threading.current_thread().ident))
        except Exception:
            return False
