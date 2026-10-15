"""hermes-claude-agents: named Claude subagents for Hermes.

Opus reviews, Sonnet builds, Haiku swarms. Agents are Markdown files (the Claude Code agent format);
the lead model picks an agent by name, the operator's files decide model, tools and read-only status.
Children run through Hermes' public subagent lifecycle API; no core patch.
"""
from __future__ import annotations

from . import agents_tools
from .agents_config import Roster
from .agents_host import HermesHost
from .agents_service import AgentsService, tool_description

PROMPT_SECTION_ID = "claude-agents.guide"


def register(ctx):
    host = HermesHost(ctx)
    service = AgentsService(host)
    try:
        _, _, roster = service.config()
    except Exception:
        roster = Roster()
    description = tool_description(roster)
    # On a Hermes build that takes it, agents_run renders as the delegation card (one row per agent,
    # like delegate_task) and keeps its lifecycle visible with display.tool_progress off.
    card = {"ui_card": "delegate"} if host.supports_ui_card() else {}
    for name in agents_tools.TOOL_NAMES:
        schema = agents_tools.schema(name, description)
        ctx.register_tool(name=name, toolset=agents_tools.TOOLSET, schema=schema,
                          handler=agents_tools.handler(name, lambda: service),
                          description=schema["description"], emoji="🤖", **(card if name == "agents_run" else {}))
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
            return f"hermes-claude-agents: {str(exc)[:500]}"

    ctx.register_command("claude-agents", handler=command, description="List the Claude agents and their models")
    return service
