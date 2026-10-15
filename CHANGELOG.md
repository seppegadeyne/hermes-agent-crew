# Heremes-Codex-Agents changes

## 0.2.0

- Restore original Claude reviewer/builder/explorer/researcher names, models, instructions and provider default.
- Add four explicit codex-* roles; Claude and Codex can run together on capable Hermes builds.
- Restore claude_agents toolset, add /claude-agents command alias, and load the original custom folder.
- Share writer/run limits and guard enforcement across both provider families.
- Document migration of settings to the new plugin identity.

## 0.1.0

- Default to Hermes native openai-codex ChatGPT subscription authentication.
- Use Sol for review/build and Luna for exploration/research, with role-specific effort.
- Rename plugin, toolset, prompt section, command and custom agent directory.
- Accept none reasoning effort for compatible models.
- Preserve explicit per-agent provider inheritance instead of reapplying the global default.
- Preserve upstream lifecycle, read-only guard, no-nesting policy and hardened git diff.
- Retain optional Claude Code file imports; model IDs require manual migration.

## Upstream history

# Changelog

## 0.4.1 (2026-10-14)

- The scouting nudge skips every failed result: Hermes' own `status`, and JSON results with a
  non-empty `error`, `"success": false` or `"ok": false`. Before, only results starting with
  `{"error"` were skipped.
- Read-only shell commands are recognized by a shell tokenizer per segment (`&&`, `;`, `|`)
  instead of a regex: quoted text such as `grep 'a>b'` or `grep -n "rm -rf"` counts as reading,
  while `git branch NAME`, `git log --output=…`, `git -c …`, `find -fprint`, `sort -o`, `tee`,
  file redirects and command substitution do not. `git -C <dir>`, `2>/dev/null`, `2>&1` and
  input redirects are fine. Only the count changes; nothing is ever blocked by it.

## 0.4.0 (2026-10-14)

Findings from a day of live sessions: leads delegated well once asked to, but rarely on their own.
In twelve turns the lead made 8 to 26 read and search calls by hand, and only four of those turns
used agents, usually after a dozen reads. Half of the agent runs with a tool list wasted calls on
blocked tool search.

- A scouting nudge: after `nudge_after` (default 8) read or search calls by the lead in one turn
  (`read_file`, `search_files`, web search and extract, read-only shell commands such as `grep`,
  `cat`, `sed -n`, `git log`), the plugin appends a one-line hint to that tool result to send further
  lookups to read-only agents. Once per turn, never after the turn used agents, never for subagents,
  never on error results, and off with `nudge_after: 0`. Calls without a session and turn id (from
  `execute_code` scripts, which parse every result as JSON) are never touched. Shell commands that
  write (`>`, `xargs`, `-delete`, `sed -i`, `rm`) do not count. Uses the documented
  `transform_tool_result` hook; fail-open.
- Agents with a `tools:` list may now use `tool_search` and `tool_describe`. With tool search on,
  their MCP tools are deferred, and 17 of 38 runs failed calls or wasted turns on blocked catalog
  reads before they found their tools. Only the catalog reads are allowed: the real call through
  `tool_call` is still checked under the underlying tool's name.
- A `tool_call` that Hermes could not resolve now gets a message that says how to fix it,
  instead of "read-only, so 'tool_call' is blocked".
- The `tasks` schema allowed 8 entries while `max_parallel` allows up to 16. It now follows the
  setting's cap. The `too_many_tasks` refusal says that nothing started and to split the call.
- The prompt guide says to swarm early (before reading everything yourself) and to run builders
  and long reviews in the background with `wait_seconds: 0`.

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
