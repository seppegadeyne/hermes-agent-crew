"""heremes-codex-agents: named Claude and GPT subagents for Hermes.

Claude roles stay intact; explicit Codex roles add Sol and Luna.
Agents are Markdown files (the Claude Code agent format);
the lead model picks an agent by name, the operator's files decide model, tools and read-only status.
Children run through Hermes' public subagent lifecycle API; no core patch.
"""
from __future__ import annotations

from . import agents_tools
from .agents_config import Roster
from .agents_host import HermesHost
from .agents_service import AgentsService, tool_description

PROMPT_SECTION_ID = "codex-agents.guide"


def register(ctx):
    service = AgentsService(HermesHost(ctx))
    try:
        _, _, roster = service.config()
    except Exception:
        roster = Roster()
    description = tool_description(roster)
    for name in agents_tools.TOOL_NAMES:
        schema = agents_tools.schema(name, description)
        ctx.register_tool(name=name, toolset=agents_tools.TOOLSET, schema=schema,
                          handler=agents_tools.handler(name, lambda: service),
                          description=schema["description"], emoji="🤖")
    ctx.register_hook("pre_tool_call", service.pre_tool_call)
    ctx.register_hook("subagent_start", service.on_subagent_start)
    ctx.register_hook("subagent_stop", service.on_subagent_stop)
    ctx.register_hook("transform_tool_result", service.on_transform_tool_result)
    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section(PROMPT_SECTION_ID, service.prompt_section, max_chars=4000)

    def command(raw_args: str = "", **_kwargs) -> str:
        try:
            return service.roster_text()
        except Exception as exc:
            return f"heremes-codex-agents: {str(exc)[:500]}"

    for name in ("claude-agents", "codex-agents"):
        ctx.register_command(name, handler=command, description="List Claude and Codex agents and their models")
    return service
