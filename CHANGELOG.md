# Changelog

## 0.2.0 (2026-10-13)

- Tasks take an optional `workdir`: the absolute path of the folder the agent works in, and the
  folder `attach_diff` reads git from. Without it agents use the session's working directory.
  The first live run showed why: a session whose working directory pointed elsewhere sent both
  agents (and the diff snapshot) to the wrong folder.

## 0.1.0 (2026-10-13)

First release.

- `agents_run` starts named agents in parallel and returns their final reports; `agents_status`
  collects, waits for or cancels runs that outlive the wait.
- Built-in agents: `reviewer` (opus, read-only), `explorer` and `researcher` (haiku, read-only),
  `builder` (sonnet).
- Read-only agents are enforced by a `pre_tool_call` guard bound in `subagent_start`; agents never
  start agents.
- `attach_diff` gives a review agent the working tree's git status, stat and diff, with every
  repository-configured git program disabled.
- Agent files use the Claude Code format; `~/.claude/agents` can be imported.
- Settings for the provider, per-agent overrides and limits; `/claude-agents` lists the roster.
