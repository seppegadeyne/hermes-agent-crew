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
PROBE_TIMEOUT_SECONDS = 2.0
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
    def mcp_read_only(name: str) -> bool:
        """True when the MCP server that registered ``name`` marked that tool readOnlyHint=True.

        Reads the discovery data Hermes keeps for its untrusted-server gate. Unknown, missing or moved
        internals mean False: the tool is treated as one that changes things.
        """
        try:
            from tools import mcp_tool as core
            from tools.mcp_tool_schema import mcp_prefixed_tool_name
            from tools.mcp_tool_scope import _resolve_server_key
            server = core._mcp_tool_server_names.get(name)
            if not server:
                return False
            hints = core._tool_read_only_hints.get(_resolve_server_key(server), {})
            # Every recorded name that maps to this registry name must be read-only: the hints are never
            # pruned, and `get-x` and `get_x` sanitize alike, so one stale True must not let a writer through.
            flags = [flag for raw, flag in list(hints.items()) if mcp_prefixed_tool_name(server, raw) == name]
            return bool(flags) and all(flag is True for flag in flags)
        except Exception:
            return False

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
    def endpoint_url(provider: str) -> str | None:
        """base_url of a named custom endpoint (``providers:`` map or ``custom_providers`` list), else None.

        None means "a provider Hermes resolves itself" (hosted APIs, provider plugins): not probed.
        Hermes' own resolver comes first, so name normalization, aliases, ``enabled: false`` and names
        shadowed by a built-in provider match what the launch will do; a plain lookup is the fallback
        for builds where that helper moved.
        """
        try:
            from hermes_cli.runtime_provider_custom import _get_named_custom_provider
            entry = _get_named_custom_provider(provider)
            url = entry.get("base_url") if isinstance(entry, dict) else None
            return url.strip() if isinstance(url, str) and url.strip() else None
        except Exception:
            pass
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly() or {}
        except Exception:
            return None
        entry = None
        providers = config.get("providers")
        if isinstance(providers, dict):
            entry = providers.get(provider)
        if entry is None and isinstance(config.get("custom_providers"), list):
            entry = next((e for e in config["custom_providers"]
                          if isinstance(e, dict) and str(e.get("name") or "") == provider), None)
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            return None
        url = entry.get("api") or entry.get("url") or entry.get("base_url")  # Hermes' own key order
        return url.strip() if isinstance(url, str) and url.strip() else None

    @staticmethod
    def endpoint_up(base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
        """True when the endpoint answers HTTP below 500 (401/404 still mean the server is there).

        Connection refused, a timeout or a 5xx (llama.cpp answers 503 while it loads a model) mean down.
        """
        import urllib.error
        import urllib.request
        url = base_url.rstrip("/") + "/models"
        # No proxy: a local endpoint must be reached directly, never through HTTP(S)_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout):
                return True
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except Exception:
            return False

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
