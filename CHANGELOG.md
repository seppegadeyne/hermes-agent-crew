# Changelog

## 0.9.1 (2026-10-17)

- The lead's prompt asks it to pass the paths of notes or knowledge-base pages it knows are relevant
  in a task's context. Expected effect: agents read the right page instead of searching a whole
  knowledge base (in the sessions reviewed, scouts spent 3-6 calls each reading an index that held
  nothing for their task). About 110 characters more in the lead's prompt; the roster budget
  absorbs it.

## 0.9.0 (2026-10-17)

Read-only agents can use the profile's read-only MCP tools. Expected effect: big MCP reads (ad
reports, analytics, docs, search APIs) move out of the lead's context into a scout's, which returns
a compact report. In the sessions reviewed, one Google Ads analysis put about 515k characters of
MCP output into the lead because no agent could call those tools.

- A read-only agent without its own `tools:` list may call any MCP tool the server marked
  `readOnlyHint: true` at discovery (the data Hermes keeps for its untrusted-server gate), plus
  tools matched by the new `readonly_mcp_tools` setting (names or `mcp__` globs) for servers that
  do not annotate. Every other MCP tool stays blocked, with a message that says why. An agent
  with an explicit tool list keeps exactly that list. Off with `readonly_mcp: false`.
- `readOnlyHint` is the server's own claim (Hermes only checks it for `trust: untrusted`
  servers); the README says so and how to turn the access off. A tool counts as read-only only
  when every recorded name that maps to it is marked, so a stale hint cannot open a write tool. A
  `readonly_mcp_tools` glob that covers a whole server or all servers is reported under problems.
- The child's brief and the lead's prompt say which MCP tools read-only agents can use.
- The child's tool schema is not widened: the MCP toolsets come from Hermes' own
  `inherit_mcp_toolsets`, and the guard decides per call.

## 0.8.2 (2026-10-17)

From a review of a day of real sessions. Expected effect: fewer false nudges in turns where the lead
pages through the file it is editing (no added tokens; the nudge only fires less often), and fewer
wasted read-only runs whose goals need a shell, which the lead then has to redo by hand.

- The scouting nudge counts distinct lookups per turn (file path, search, query, URL set or
  command), not calls. Paging one file with offset/limit is one lookup. In the sessions reviewed,
  both nudges fired while the lead read one 1,300-line script in chunks just before editing it.
- The lead's prompt and the `agents_run` description now say that read-only agents cannot run
  commands or save notes. Four researchers were asked to "test with plain urllib"; all four
  reported they could not, and the lead redid the probes itself.

## 0.8.1 (2026-10-16)

- `/crew` prints an aligned table (agent, tier, model @ provider, access, fallback routes marked
  `(down)`), a status block, numbered agent folders and problems; `/crew <agent>` shows one agent in
  full (routes with up/down, tools, source, trigger, description). The lead's prompt is unchanged.

## 0.8.0 (2026-10-16)

Follow-ups to plans. Expected effect: fewer failed plans that the lead has to rerun by hand (fix
loop), no waiting writers for fixes in the same file (grouping), and more jobs moved to plans
(hint). None of these adds tokens to the lead's context; the hint is one line, once per turn.

- Fix loop: `retry: 0-2` on a writer `for_each` stage. Its runs end with a result block; a run that
  reports `failed` or ends in error gets another attempt with its report in context, and the loop
  stops early when an attempt fails for the same reason as the last (no progress). Items show
  `attempts` (counted per stage); stages show `retried`. Only reported failures and runs that failed
  while working are retried; interrupted, cancelled or lost runs, launch refusals and stages that
  already have a hard failure are not. `retry` is refused on read-only and non-for_each stages.
- Items whose files overlap go to one writer run, so same-file fixes neither wait nor conflict.
  Chosen instead of git worktrees per builder: worktrees need a merge step whose conflicts would
  cost another agent or lead round trip, which works against both plugin goals.
- Plan hint: the second `agents_run` tasks fan-out with 3+ reports in one turn gets a one-line hint
  to use a plan. Off with `nudge_after: 0`.

## 0.7.0 (2026-10-16)

Agent plans: the lead writes a big job once as stages, and the plugin runs them one after another
inside the lead's own `agents_run` / `agents_status` waits. Expected effect on the plugin goals: a
scan -> verify -> fix -> check job no longer passes every intermediate report through the lead's
context (up to 60,000 characters per stage before); the lead reads one result of at most about
8,000 characters and asks for one item's trail when it needs more. Stages also start without a
lead reasoning step in between. Measured once (8-file rename in a test repo, sonnet lead, haiku
scout, 8 sonnet builders, opus reviewer; both runs changed all 8 files and were approved): the lead
processed about 64k tokens with a plan against 115k chained (-45%), with 762 against 1,921 output
tokens and 3 against 5 API calls; wall clock 73 s against 69 s (one sample each, within noise).
All sessions together used about 15% more tokens with the plan, mostly builders that re-read files
(3-5 API calls each against 3). Script: research/hermes-update/20261016-agent-crew-070/measure.sh.

- `agents_run` takes `plan` as an alternative to `tasks`: up to 8 stages with the task fields plus
  `name`, `split`, `for_each`, `per_run` and `verify`. Validation happens before anything starts.
- Stages hand over items through a fenced json block the plugin asks for; ids are assigned by the
  plugin and duplicates dropped. A missing block fails the plan with the end of the report quoted.
- Verify stages keep only confirmed items; silence counts as rejected. New built-in read-only
  `verifier` agent (balanced tier).
- Writer stages take `write_scope` from item files, which must stay inside the workdir
  (`refused_scope` otherwise). Runs launch in waves under the existing admission lock, through the
  existing `_launch` and `_check_writers`; a writer that does not fit waits for the next wave.
- No background thread: plans advance only while the lead waits. `agents_status` gets `plan_id`
  (continue, report, cancel) and `item` (one item's full trail). Plan runs never show up as
  unreported runs.
- `plan_max_items` setting (default 24). A prompt guide line tells the lead when a plan pays off.
- `_parse_task` extracted from `_parse_tasks` (no behavior change).

## 0.6.0 (2026-10-16)

The plugin is now provider-neutral: any model Hermes can reach can be an agent, local endpoints
included, and the lead is whatever model the session runs on. Expected effect on the plugin goals:
the fast tier can move scans and verification to a local or cheap model, so the same job costs fewer
paid tokens; the lead's context is unchanged. Live check: a lead on a local llama.cpp model
(named `providers:` entry) ran one `agents_run` call with an explorer on the fast tier mapped to that
local endpoint (6 API calls, no paid tokens) and the reviewer on the Claude subscription (opus, 2 API
calls); the child sessions recorded exactly those providers and models. Token savings over a fully
paid run are not measured yet.

Breaking: plugin id `hermes-claude-agents` -> `hermes-agent-crew`. Move enablement and settings
once (README, "Upgrading from hermes-claude-agents"); never enable both ids.

- Model tiers. Agent files can name `tier: strong | balanced | fast` instead of a model; the new
  `tiers` setting maps each tier to a `model` and optionally a `provider`. The built-in agents use
  tiers, and the defaults keep today's routes (opus, sonnet, haiku on the `provider` setting), so
  nothing changes without configuration. Model and provider resolve one field at a time: override,
  then the agent file, then the tier, then the `provider` setting. A tier without a provider keeps
  `provider: inherit` working; agent files with a `model` (and Claude Code files) ignore tiers. An
  unknown tier, or `tier` together with `model: inherit` or `provider: inherit` in one file, is an
  agent file problem. An invalid `tiers` entry leaves that tier's agents out with a problem line,
  never falling back to the default (possibly paid) route. A tier change reloads the roster.
- Fallback routes for local models. A tier can list up to 4 routes; before each launch the plugin
  probes named `providers:` endpoints (`GET <base_url>/models`, 2 s, cached 60 s up / 15 s down;
  hosted providers are never probed) and runs the agent on the first route that answers, with the
  skipped routes in the run's `note`. Example: `fast: [{provider: strata-qwen, model: strata},
  {model: haiku}]` scouts locally while the server runs and on haiku when it does not. Skipped
  routes are listed in the run's new `fallback` field. Endpoint lookup uses Hermes' own matching of
  named providers, probes bypass HTTP proxies, pinning one field picks the route that agrees with it,
  and builds without per-launch provider routing skip routes on other providers. With no
  reachable route the run fails at once (`endpoint_unavailable`) instead of hanging on a dead
  endpoint; other runs in the call are unaffected. `/crew` marks down routes. Live check: a dead
  endpoint was skipped to strata-qwen, and with both local routes dead the explorer ran on haiku.
- Named `providers:` entries (for example a local llama.cpp or TabbyAPI server) work as an agent's or
  tier's provider through Hermes' own provider resolution; an integration test proves the child
  gets that endpoint and model and never the lead's key. A second test pins that a provider-less
  launch (`provider: inherit`) follows Hermes' `delegation:` config, not the lead.
- Each run's result now includes its `provider`. Roster labels show the tier: `explorer (fast:
  haiku, read-only)`.
- `/crew` replaces `/claude-agents`, which stays one release as a deprecated alias. The prompt
  section id is `crew.guide`, and the plugin's own text (prompt guide, tool descriptions, guard
  messages, built-in agent descriptions) names no vendor or model; a test keeps it that way.
- Unchanged on purpose: the toolset id `claude_agents` (renaming it would re-enable the toolset in
  saved `platform_toolsets` lists where operators turned it off), the tool names and the default
  agent folder `{hermes_home}/claude-agents`.
- AGENTS.md has a Purpose section: every change serves faster tasks and fewer lead tokens, builds
  on what exists, stays opt-in and provider-neutral.

## 0.5.0 (2026-10-15)

Findings from a day of live sessions (one Opus lead, about 15 conversations): the lead's only
agents_run call asked for three Sonnet builders, each writing its own JSON file. The one-writer
limit refused it, and the lead fell back to `delegate_task`, which ran the three on Opus (its
`delegation.model`) for about 6 USD and 10 minutes. The scouting nudge fired twice all day; most hand reads were
`cat a; echo ---; cat b` chains that did not count as reading.

- `write_scope` per task: the files or folders a writer may change. Writers with disjoint scopes
  run in parallel; `max_parallel_writers` applies while any writer has no scope. The guard checks
  the target of every `write_file` and `patch` call (every file of a V4A patch, symlinks
  resolved) against the scope; scoped writers must use absolute paths, because Hermes resolves
  relative and `~` paths against the child's own cwd and home. Admission and launch of an
  `agents_run` call are now one locked step, so two calls at the same time cannot both pass the
  writer checks. Overlapping scopes are refused before anything starts, and the
  refusal for unscoped writers says how to run them in parallel. `max_parallel_writers: 0` turns
  writers off with its own `writers_disabled` refusal.
- The `pre_tool_call` guard now receives the call's arguments (documented hook kwarg `args`).
- Read-only shell detection also accepts `echo`, `printf`, `pwd`, `which`, `realpath`, `readlink`,
  `basename`, `dirname`, `diff`, `cmp`, checksums and `git rev-parse/describe/ls-tree/cat-file/
  shortlog`. Print-only segments (`echo ---`, `pwd`) do not count as a lookup on their own, and a
  redirect into a file still disqualifies the command.
- The prompt guide has a "Parallel builders" line; the builder's description says it runs in
  parallel with disjoint scopes.

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
