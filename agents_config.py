"""Agent roster and settings for hermes-claude-agents.

Agents are Markdown files with a small YAML-style frontmatter block, the same shape Claude Code
uses for ``~/.claude/agents/*.md``::

    ---
    name: reviewer
    description: Senior read-only reviewer for plans and diffs.
    model: opus
    readonly: true
    ---
    Instructions for the agent (the Markdown body).

Supported keys: ``name``, ``description``, ``model`` (``inherit`` = the parent's model),
``provider``, ``readonly``, ``tools`` (Hermes tool names, or Claude Code names such as Read,
Grep, Glob, WebFetch, Bash, Edit), ``toolsets`` (Hermes toolsets to narrow the child to) and
``effort``. Unknown keys are ignored with a warning, so Claude Code files load unchanged.

The parser is deliberately small (flat keys, scalars, inline or block lists, ``|``/``>``
blocks), so the plugin needs no YAML dependency.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
BUILTIN_AGENTS_DIR = PLUGIN_DIR / "agents"
CLAUDE_CODE_AGENTS_DIR = "~/.claude/agents"

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,127}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOOL_RE = re.compile(r"^[A-Za-z0-9_*?-]{1,96}$")
TOOLSET_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
MAX_DESCRIPTION_CHARS = 300
MAX_INSTRUCTIONS_CHARS = 12_000

# Tools a read-only agent may use. Everything else (file writes, shell, code execution, browser
# actions, memory, scheduling, messaging, other agents) is refused by the pre_tool_call guard.
READONLY_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract", "x_search", "vision_analyze",
    "session_search", "skill_view", "skills_list", "todo_list", "tool_search", "tool_describe",
})
# Claude Code tool names -> Hermes tool names, so Claude Code agent files can be reused.
CLAUDE_CODE_TOOLS: dict[str, tuple[str, ...]] = {
    "read": ("read_file",),
    "grep": ("search_files",),
    "glob": ("search_files",),
    "ls": ("search_files",),
    "webfetch": ("web_extract",),
    "websearch": ("web_search",),
    "todowrite": ("todo_list",),
    "bash": ("terminal", "process_manage"),
    "bashoutput": ("process_manage",),
    "killshell": ("process_manage",),
    "edit": ("patch",),
    "multiedit": ("patch",),
    "write": ("write_file",),
    "notebookedit": ("patch",),
    "task": (),  # Claude Code's own subagent launcher: agents never start agents here
}
KNOWN_KEYS = frozenset({
    "name", "description", "model", "provider", "tools", "toolsets", "readonly", "read_only",
    "read-only", "effort", "reasoning_effort",
    "color",  # Claude Code UI colour: cosmetic, ignored without a warning
})

# Provider the agents run on unless an agent file names its own. "inherit" (or empty) = the lead's
# provider. On Hermes builds without per-launch provider routing an agent can only run on the lead's
# provider; a mismatch is refused before anything starts.
DEFAULT_PROVIDER = "claude-subscription-directsdk-experimental"
INHERIT = "inherit"

DEFAULT_SETTINGS: dict[str, Any] = {
    "agent_dirs": ["{hermes_home}/claude-agents"],
    "import_claude_code_agents": False,
    "provider": DEFAULT_PROVIDER,
    "overrides": {},
    "max_parallel": 6,
    "max_live_runs": 8,
    "max_parallel_writers": 1,
    "wait_seconds": 600,
    "diff_max_chars": 20_000,
    "summary_max_chars": 12_000,
    "prompt_section": True,
}
INT_RANGES = {
    "max_parallel": (1, 8),
    "max_live_runs": (1, 16),
    "max_parallel_writers": (0, 8),
    "wait_seconds": (0, 1800),
    "diff_max_chars": (1000, 30_000),
    "summary_max_chars": (1000, 32_000),
}


class NotAnAgent(ValueError):
    """A Markdown file without frontmatter (a README, notes): skipped silently."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str = ""
    model: str | None = None
    provider: str | None = None
    readonly: bool = False
    tools: tuple[str, ...] | None = None
    toolsets: tuple[str, ...] | None = None
    effort: str | None = None
    instructions: str = ""
    source: str = "builtin"


@dataclass
class Roster:
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- frontmatter parsing

def _scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    hash_at = text.find(" #")
    if hash_at >= 0:
        text = text[:hash_at].rstrip()
    return text


def _split_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [item for item in (_scalar(part) for part in parts) if item]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown agent file into (metadata, body)."""
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        raise ValueError("frontmatter has no closing '---'")
    meta: dict[str, Any] = {}
    i = 1
    while i < end:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line[:1].isspace():
            raise ValueError(f"frontmatter line {i + 1}: unexpected indentation")
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or not KEY_RE.match(key):
            raise ValueError(f"frontmatter line {i + 1}: expected 'key: value'")
        if key in meta:
            raise ValueError(f"frontmatter key {key!r} appears twice")
        rest = rest.strip()
        i += 1
        if rest and rest[0] in "|>" and rest.rstrip("+-") in ("|", ">"):
            block: list[str] = []
            while i < end and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            meta[key] = ("\n".join(block) if rest[0] == "|" else " ".join(x for x in block if x)).strip()
        elif not rest:
            items: list[str] = []
            while i < end and lines[i].strip().startswith("- "):
                items.append(_scalar(lines[i].strip()[2:]))
                i += 1
            meta[key] = items
        elif rest.startswith("["):
            if not rest.endswith("]"):
                raise ValueError(f"frontmatter key {key!r}: unterminated list")
            meta[key] = _split_list(rest)
        else:
            meta[key] = _scalar(rest)
    return meta, "\n".join(lines[end + 1:]).strip()


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    text = "" if isinstance(value, list) and not value else str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0", ""):
        return False
    raise ValueError(f"{key} must be true or false, not {value!r}")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        items = _split_list(text) if ("," in text or text.startswith("[")) else text.split()
    return [str(item).strip() for item in items if str(item).strip()]


def _opt_str(value: Any, key: str) -> str | None:
    if value is None or value == []:
        return None
    if isinstance(value, (list, dict)):
        raise ValueError(f"{key} must be a single value")
    text = str(value).strip()
    return text or None


def _map_tools(names: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for name in names:
        base = name.split("(", 1)[0].strip()  # Claude Code permission rules: Bash(git diff:*)
        mapped = CLAUDE_CODE_TOOLS.get(base.lower())
        if mapped is not None:
            out.extend(mapped)
        elif TOOL_RE.match(base):
            out.append(base)
        else:
            raise ValueError(f"invalid tool name {name!r}")
    return tuple(dict.fromkeys(out))


def _check_model(value: str | None) -> str | None:
    if value is None or value.lower() == "inherit":
        return None
    if not MODEL_RE.match(value):
        raise ValueError(f"invalid model {value!r}")
    return value


def _check_provider(value: str | None) -> str | None:
    if value is None or value.lower() == INHERIT:
        return None
    if not PROVIDER_RE.match(value):
        raise ValueError(f"invalid provider {value!r}")
    return value


def _check_effort(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.lower()
    if value not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    return value


def spec_from_markdown(text: str, *, source: str = "builtin",
                       default_name: str | None = None) -> tuple[AgentSpec, list[str]]:
    """Build an AgentSpec from an agent file; raises ValueError (NotAnAgent without frontmatter)."""
    meta, body = parse_frontmatter(text)
    if not meta:
        raise NotAnAgent("no frontmatter")
    warnings: list[str] = []
    name = _opt_str(meta.get("name"), "name") or default_name or ""
    if not NAME_RE.match(name):
        raise ValueError(f"invalid agent name {name!r} (lowercase letters, digits, - or _, max 32)")
    description = " ".join((_opt_str(meta.get("description"), "description") or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
        warnings.append(f"description clipped to {MAX_DESCRIPTION_CHARS} characters")
    model = _check_model(_opt_str(meta.get("model"), "model"))
    provider = _check_provider(_opt_str(meta.get("provider"), "provider"))
    readonly_key = next((k for k in ("readonly", "read_only", "read-only") if k in meta), None)
    readonly = _as_bool(meta[readonly_key], readonly_key) if readonly_key else False
    tools: tuple[str, ...] | None = None
    if "tools" in meta:
        raw = _as_list(meta["tools"])
        if raw and not (len(raw) == 1 and raw[0].lower() in ("*", "all")):
            tools = _map_tools(raw)
    toolsets: tuple[str, ...] | None = None
    if "toolsets" in meta:
        raw = _as_list(meta["toolsets"])
        bad = [t for t in raw if not TOOLSET_RE.match(t)]
        if bad:
            raise ValueError(f"invalid toolset name(s): {', '.join(bad)}")
        toolsets = tuple(dict.fromkeys(raw)) or None
    effort = _check_effort(_opt_str(meta.get("effort", meta.get("reasoning_effort")), "effort"))
    if readonly:
        if tools is None:
            tools = tuple(sorted(READONLY_TOOLS))
        else:
            # MCP tools are the operator's call: list only read-type MCP tools for a read-only agent.
            unsafe = [t for t in tools if t not in READONLY_TOOLS and not t.startswith("mcp__")]
            if unsafe:
                raise ValueError("a read-only agent cannot list tools that change things: " + ", ".join(unsafe))
    elif tools and all(t in READONLY_TOOLS for t in tools):
        readonly = True  # a Claude Code agent limited to Read/Grep/Glob/WebFetch is read-only in practice
    if len(body) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(f"instructions are longer than {MAX_INSTRUCTIONS_CHARS} characters")
    warnings.extend(f"ignored key {key!r}" for key in meta if key not in KNOWN_KEYS)
    spec = AgentSpec(name=name, description=description, model=model, provider=provider, readonly=readonly,
                     tools=tools, toolsets=toolsets, effort=effort, instructions=body, source=source)
    return spec, warnings


# --------------------------------------------------------------------------- settings

def read_settings(get_config: Callable[[str, Any], Any]) -> tuple[dict[str, Any], list[str]]:
    """Plugin settings (plugins.entries.hermes-claude-agents.settings.*) with validation."""
    settings: dict[str, Any] = {}
    problems: list[str] = []
    for key, default in DEFAULT_SETTINGS.items():
        try:
            value = get_config(key, default)
        except Exception:
            value = default
        if value is None:
            value = default
        if key in INT_RANGES:
            lo, hi = INT_RANGES[key]
            ok = (not isinstance(value, bool) and isinstance(value, (int, float))
                  and float(value).is_integer() and lo <= value <= hi)
            if not ok:
                problems.append(f"settings.{key}={value!r} is not a whole number in {lo}-{hi}; using {default}")
                value = default
            value = int(value)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                problems.append(f"settings.{key} must be true or false; using {default}")
                value = default
        elif key == "agent_dirs":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                problems.append("settings.agent_dirs must be a list of folder paths; using the default")
                value = list(default)
        elif key == "overrides":
            if not isinstance(value, Mapping):
                problems.append("settings.overrides must be a mapping of agent name -> settings; ignored")
                value = {}
            value = {str(k): v for k, v in value.items()}
        elif key == "provider":
            if not isinstance(value, str):
                problems.append(f"settings.provider must be a provider name or '{INHERIT}'; using {default}")
                value = default
            value = value.strip()
            if value.lower() == INHERIT:
                value = ""
            elif value and not PROVIDER_RE.match(value):
                problems.append(f"settings.provider {value!r} is not a valid provider name; using the lead's")
                value = ""
        settings[key] = value
    return settings, problems


# --------------------------------------------------------------------------- roster loading

def agent_dirs(settings: Mapping[str, Any], home: Path) -> list[Path]:
    """Folders scanned in order; a later definition with the same name replaces an earlier one."""
    dirs = [BUILTIN_AGENTS_DIR]
    if settings.get("import_claude_code_agents"):
        dirs.append(Path(CLAUDE_CODE_AGENTS_DIR).expanduser())
    for raw in settings.get("agent_dirs") or ():
        dirs.append(Path(str(raw).replace("{hermes_home}", str(home))).expanduser())
    return list(dict.fromkeys(dirs))


def _apply_overrides(roster: Roster, settings: Mapping[str, Any]) -> None:
    for name, override in (settings.get("overrides") or {}).items():
        if name not in roster.agents:
            roster.problems.append(f"overrides.{name}: no agent with that name")
            continue
        if not isinstance(override, Mapping):
            roster.problems.append(f"overrides.{name} must be a mapping")
            continue
        try:
            if "enabled" in override and not _as_bool(override["enabled"], f"overrides.{name}.enabled"):
                del roster.agents[name]
                continue
            changes: dict[str, Any] = {}
            for key, value in override.items():
                text = None if value in (None, "") else str(value).strip()
                if key == "model":
                    changes["model"] = _check_model(text)
                elif key == "provider":
                    changes["provider"] = _check_provider(text)
                elif key == "effort":
                    changes["effort"] = _check_effort(text)
                elif key != "enabled":
                    raise ValueError(f"unknown key {key!r} (use model, provider, effort or enabled)")
        except ValueError as exc:
            roster.problems.append(f"overrides.{name}: {exc}")
            continue
        roster.agents[name] = replace(roster.agents[name], **changes)
    default_provider = settings.get("provider") or None
    if default_provider:
        for name, spec in list(roster.agents.items()):
            if spec.provider is None:
                roster.agents[name] = replace(spec, provider=default_provider)


def load_roster(settings: Mapping[str, Any], home: Path) -> Roster:
    roster = Roster()
    for directory in agent_dirs(settings, home):
        roster.folders.append(str(directory))
        builtin = directory == BUILTIN_AGENTS_DIR
        try:
            paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
        except OSError as exc:
            roster.problems.append(f"{directory}: {exc}")
            continue
        for path in paths:
            label = f"builtin:{path.name}" if builtin else str(path)
            try:
                spec, warnings = spec_from_markdown(path.read_text(encoding="utf-8"),
                                                    source="builtin" if builtin else str(path),
                                                    default_name=path.stem)
            except NotAnAgent:
                continue
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                roster.problems.append(f"{label}: {exc}")
                continue
            roster.warnings.extend(f"{label}: {warning}" for warning in warnings)
            roster.agents[spec.name] = spec
    _apply_overrides(roster, settings)
    return roster


class RosterCache:
    """Reload the roster only when an agent file or a roster-relevant setting changes."""

    _KEYS = ("agent_dirs", "import_claude_code_agents", "provider", "overrides")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Any = None
        self._roster: Roster | None = None

    def get(self, settings: Mapping[str, Any], home: Path) -> Roster:
        files: list[tuple[str, int, int]] = []
        for directory in agent_dirs(settings, home):
            try:
                paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
            except OSError:
                paths = []
            for path in paths:
                try:
                    stat = path.stat()
                    files.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    files.append((str(path), 0, -1))
        relevant = json.dumps({k: settings.get(k) for k in self._KEYS}, sort_keys=True, default=str)
        key = (relevant, str(home), tuple(files))
        with self._lock:
            if self._roster is None or key != self._key:
                self._roster = load_roster(settings, home)
                self._key = key
            return self._roster
