# Changelog

## 0.3.0 (2026-10-14)

- Agent files take an optional `use_when` (or `when_to_use`): a short trigger shown whole before
  the description in the lead's roster. The built-in agents have one. A live session
  skipped a configured language-check worker because its 160-character roster line cut off exactly
  the "when" part, and then checked a five-page CV by sampling excerpts inline.
- The prompt guide gains a "mechanical fan-out" rule: whole-document checks and extractions go to
  one worker per file or URL over the complete input, instead of inline sampling.
- `/claude-agents` lists each agent's trigger.
- The prompt section is built to budget: the call instructions and usage guide always survive;
  over budget, descriptions shrink first, then agents move to a "… N more" line. Before, the
  end-clip could cut the guide when many agents were configured.
- Roster lines end in a plain ellipsis instead of a line-breaking "[… N characters omitted]" marker.

## 0.2.1 (2026-10-14)

- Higher defaults for read-only swarms: `max_parallel` 6 -> 12 (cap 8 -> 16) and `max_live_runs`
  8 -> 24 (cap 16 -> 32). A six-task Haiku swarm finished in about 22 s, so the old limit, not
  the agents, was the bottleneck. Writers stay at one at a time.

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
