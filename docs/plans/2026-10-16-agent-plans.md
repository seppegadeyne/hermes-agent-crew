# Open agent pool, new name, and agent plans

Status: Part A built and rolled out (0.6.0); Part B built (0.7.0, see CHANGELOG), measurement pending. Draft v4. Both parts were checked against the code by the Opus reviewer on 2026-10-16
(see "Review outcome" at the end of each part). Nothing is built yet.

Two releases, in this order, so the rename happens once and plans ship under the new name:

- Part A (0.6.0): provider-neutral agent pool and the new plugin name.
- Part B (0.7.0): agent plans.

# Part A: open agent pool and new name (0.6.0)

## Goal

Any model Hermes can reach can be an agent: Claude through the subscription provider, models of
other providers (OpenRouter, xAI, Google, OpenAI, ...) and local endpoints (llama.cpp, TabbyAPI,
Ollama, vLLM) configured under `providers:` in Hermes' config. The orchestrator is always the
model the Hermes session runs on; the plugin never picks or changes it.

Both plugin goals: cheap or local models take the high-volume work (scans, verification batches),
so the same job costs fewer paid tokens. The lead's context does not change.

## What already works, and what does not

Most of this is already in the code; Part A builds on it.

- Agents already have `provider` and `model` per file and per override, and a plugin-wide
  `provider` setting with `inherit` (agents_config.py: `_check_provider`, `INHERIT`,
  `_apply_overrides`).
- Named entries under `providers:` resolve through the launch request's `provider` field in the
  installed Hermes (verified by the reviewer): subagent_lifecycle.py passes `request.provider`
  to `_resolve_delegation_credentials`, which goes through `resolve_runtime_provider`; the
  named-custom rung reads the entry's `base_url` and `api_mode`, and a keyless local entry gets
  the `no-key-required` placeholder. A local agent needs no URL or key in its file.
- Hermes builds without the `provider` field (older than the lifecycle provider change) refuse a
  mismatched agent with `provider_unsupported`: fail closed, already in the code.
- The lead is already the session's agent: `host.parent()` returns whatever agent runs the turn,
  on whatever provider. Nothing to build, only to state and test.
- Not neutral today: the plugin name, the `/claude-agents` command, the prompt section
  ("Claude agents: ..."), the tool description ("Run named Claude subagents ..."), the
  `plugin.yaml` description, built-in agent descriptions ("Senior read-only reviewer (Opus)"),
  and these strings: `PLUGIN_ID` and launch metadata (agents_service.py:32, 473),
  `ISOLATION_MESSAGE` (:64), the contextvar name (:147), guard message prefixes
  (agents_policy.py:81-121, agents_service.py:198), the nudge text (:913), `/claude-agents`
  references (:399, :758, :987), `PROMPT_SECTION_ID` (__init__.py:14) and module docstrings.

## Model tiers

Built-in agents should not care which vendor runs them. Today they name `model: opus`,
`sonnet` and `haiku`, which only resolve on the Claude subscription provider.

- New optional agent-file key `tier`: `strong`, `balanced` or `fast`. The built-in agents use it
  (reviewer: strong, builder: balanced, explorer and researcher: fast) instead of `model`.
- New setting `tiers`, a map from tier to `{model, provider?}`. Defaults carry only a model:
  strong = opus, balanced = sonnet, fast = haiku. Without a `provider` in the tier, the provider
  comes from the plugin-wide `provider` setting exactly as today, so an operator on
  `provider: inherit` keeps running the built-ins on the lead's provider. An operator moves a
  tier elsewhere with one line, for example `fast: {provider: strata-qwen, model: strata}`.
- Resolution per field, most specific first, each field on its own:
  - model: `overrides.<agent>.model` > the file's `model` > the tier's `model` > none (the
    lead's model, as `inherit` today).
  - provider: `overrides.<agent>.provider` > the file's `provider` > the tier's `provider` > the
    plugin-wide `provider` setting > none (the lead's provider).
  An agent file with `model` and no `tier` (every existing user file, and Claude Code imports)
  behaves exactly as before. Existing overrides such as `{reviewer: {model: sonnet}}` keep
  working.
- An unknown tier name, or a tier the operator mapped to an invalid value, is a roster problem
  (shown by the command, like other agent file problems) and that agent is left out of the
  roster. No silent fallback: Hermes would otherwise route a provider-less launch through the
  `delegation:` config, not to the lead (subagent_lifecycle.py:296-297).
- `tiers` joins `RosterCache._KEYS`, gets validation in `read_settings` and a `dict` entry in
  `plugin.yaml` `config_schema`.
- The roster label shows what actually runs: `explorer (fast: strata-qwen/strata, read-only)`.

## Local and third-party models: what changes in behavior

- Tool use: weaker models may call tools badly or skip the report format. The guard does not
  depend on the model, so safety is unchanged; quality is the operator's choice. The README
  advises local models for read-only scouting and verification first, not for writers.
- Context size: Hermes refuses a model with a window under 64K tokens
  (`MINIMUM_CONTEXT_LENGTH`). The README tells operators to start local servers with at least
  `-c 65536`, and names the symptom (`launch_failed` or a failed child).
- A `providers:` entry whose name equals a built-in provider name is shadowed by the built-in;
  the README says so.
- Endpoint down (added after the build, at Seppe's request: the local server is not always up):
  a tier can list fallback routes; named endpoints are probed before launch and the first route
  that answers runs. A single dead route fails fast with `endpoint_unavailable`. A paid fallback
  only happens when the operator lists one.
- Parallelism: local servers queue requests above their slot count themselves, and Hermes'
  lifecycle pool caps live children at 8. No per-endpoint limit in 0.6.0 (see "Later").
- `provider` is added to each run's public result, next to `model` and `api_calls`, so the lead
  and the measurement see what ran where.

## New name

`claude` goes out of every visible surface. Proposal (to confirm with Seppe):

- Plugin id (`name` in `plugin.yaml`), repo and install folder: `hermes-agent-crew`. GitHub keeps
  a redirect after a repo rename.
- Command: `/crew`. `/agents` and `/plan` are Hermes built-ins; `/crew` is free and plugin
  registration refuses built-in collisions anyway. `/claude-agents` stays as a second
  `register_command` alias for one release, with a deprecation line.
- Kept on purpose:
  - Toolset id `claude_agents`. Renaming it would switch the new toolset on in every saved
    `platform_toolsets` list, reversing operators' opt-outs, and leave stale entries that only
    `hermes doctor` reports. It is barely visible; a later release may rename it with its own
    migration.
  - Tool names `agents_run` and `agents_status` (already neutral, used in SOUL texts).
  - Agent folder `{hermes_home}/claude-agents` stays the default for 0.6.0; the setting
    `agent_dirs` already lets operators choose. A neutral default can come with the toolset
    rename.
- Prompt section id `crew.guide`; prompt head "Agents: named subagents for token-efficient
  delegation. You are the lead: you plan, decide and verify ...".

Migration (settings live under `plugins.entries.<plugin id>`, and plugins are off until enabled):

1. While the old plugin is still installed: `hermes plugins disable hermes-claude-agents`.
2. Install the new id; on Aorus rename the shared install folder and every profile's symlink to
   `hermes-agent-crew` (Hermes looks a plugin up by a folder named after its id when it is not
   loaded).
3. `hermes plugins enable hermes-agent-crew`.
4. Move `plugins.entries.hermes-claude-agents.settings` to `plugins.entries.hermes-agent-crew`.
5. Restart gateways; one `agents_run` per profile as proof.

Never have both ids enabled at once: both register `agents_run` and `agents_status`. The plugin
cannot detect old settings itself (`get_config` only reads its own entry), so the CHANGELOG and
README carry these steps.

## Tasks (Part A)

A1. Integration test with the real loader in a temporary HERMES_HOME: a launch with `provider`
    set to a named `providers:` entry (base_url endpoint, keyless) resolves to that entry's
    base_url and the requested model (stub the HTTP call). Second case: no provider in the
    request with `delegation.provider` set, to pin down that Hermes then uses `delegation:`,
    not the lead.
A2. `tier` in `KNOWN_KEYS`, `AgentSpec.tier`, the `tiers` setting with defaults and validation,
    `tiers` in `RosterCache._KEYS`, the per-field resolution. Tests in `tests/test_config.py`:
    built-ins on default tiers resolve to today's models and providers; `provider: inherit`
    still leaves built-ins on the lead's provider; a tier with its own provider; an override with
    only `model` on a tier agent; a file with `provider` and `tier` but no `model`; an unknown
    tier is a problem; a user file with `model` ignores tiers; a tier change invalidates the
    cache.
A3. Built-in agent files: `tier` instead of `model`, descriptions without vendor or model names.
A4. `provider` in each run's public result; roster label with tier and route.
A5. Neutral text everywhere in the list above, plus README and `plugin.yaml` description. A test
    renders the prompt section, tool descriptions, nudge, refusals and roster with the default
    roster and asserts that no text outside agent-file and setting values contains `Claude`,
    `Opus`, `Sonnet` or `Haiku`.
A6. Rename: plugin id, `PLUGIN_ID`, package name in `tests/conftest.py`, `/crew` plus the
    deprecated `/claude-agents` alias, prompt section id. CHANGELOG 0.6.0 with a "Breaking:
    plugin id" block and the migration steps, version 0.6.0. README local-model section.
A7. Checks from AGENTS.md (ruff, pytest oldest and main, plugins doctor and validate), then a
    live run: one `fast` agent on a local endpoint (strata-qwen) and one on the Claude
    subscription in the same `agents_run` call, from a lead on a third provider.

Rollout (ops, after the release, not a code task): rename the GitHub repo, then the migration
steps on Aorus (shared install, all profiles) and on the VPS nodes.

## Review outcome, Part A (Opus reviewer, 2026-10-16)

- B1: default tiers carry no provider, so `provider: inherit` keeps working.
- B2: no "fall back to the lead" for a missing tier; it is a roster problem. A1 tests the
  `delegation:` routing for provider-less launches.
- B3: `tiers` in the roster cache key, settings validation and `config_schema`.
- B4: toolset id `claude_agents` kept (opt-out reversal and stale entries otherwise).
- S1: the 64K minimum context of Hermes, documented for local servers.
- S2: model and provider resolve per field; mixed cases tested.
- S3: rollout order (disable old, swap, enable new, move settings), install folder renamed with
  the id, never both ids enabled.
- S4: shadowed built-in provider names documented.
- S5: full list of `claude` strings in A5.
- Cut: `max_concurrent` (no waves in 0.6.0, so it could only refuse), agent folder rename, ops
  rollout out of the release.
- Done check (Opus reviewer, after the build): the vendor-neutrality test had a broken regex and
  checked nothing (fixed, plus a negative control); an invalid `tiers` entry now drops its agents
  instead of falling back to the default route; `tier` with an explicit `inherit` is a problem; the
  delegation-routing integration test was added. The prompt's roster label leaves out the provider
  on purpose (prompt tokens); `/crew` and each run result show it.
- Open: `parent.provider` of a lead on a named custom endpoint (entry name or `custom`); only
  matters if the plugin ever passes the lead's route explicitly, which this plan avoids.

# Part B: agent plans (0.7.0)

## Goal

The lead writes the plan for a large task once. The plugin runs it stage by stage, feeds the
output of one stage into the next, and returns one compact result. The lead pays for the plan and
the final result, not for every report in between.

Both plugin goals (AGENTS.md, Purpose):

- Faster: no lead reasoning step between stages, and every stage fans out in parallel.
- Fewer tokens: today a scan -> fix -> review job costs the lead three tool results full of
  reports (up to 60,000 characters each) plus its own reasoning over them. With a plan, the lead
  reads short progress lines while it waits and one final summary of about 8,000 characters at
  most. Details stay in the plugin; the lead reads them only on request.

Source of the idea: an X thread (@thedelost, 2026-10-14) on "Opus writes the workflow, Haiku
scans, Sonnet patches, a verifier checks, Opus reads only what survived". The "script" there is a
plan, not code. This design takes the pattern, not the thread's claims (`--effort ultracode`,
JEV), which are unverified.

## Non-goals

- No rewrite. `agents_run`, `agents_status`, the guard, `_launch`, `_check_writers`, `_wait`,
  `_poll` and the report code stay; plans call them. The only refactor is extracting the
  per-task body of `_parse_tasks` into `_parse_task` (no behavior change).
- No executable scripts written by the model. A plan is declarative JSON that the plugin
  validates. No shell, no eval.
- No automatic activation. The lead chooses a plan; the plugin never turns a call into one.
- No background thread. A plan advances only while the lead waits in `agents_run` or
  `agents_status` (see "Execution" for why).
- No loops, branches, retries, nudge or git worktrees in 0.7.0 (see "Later").

## Shape of a plan

`agents_run` gets an optional `plan` argument as an alternative to `tasks` (exactly one of the
two). Extending the existing tool keeps the lead's schema small and reuses parsing, admission and
launch.

```json
{
  "plan": {
    "workdir": "/srv/app",
    "stages": [
      {"name": "scan", "agent": "explorer",
       "goal": "Find every call of the deprecated parse_date() and say what each one needs.",
       "context": "Python repo; the replacement is dates.parse(value, tz=UTC).",
       "split": ["src/api/", "src/jobs/", "src/web/"]},
      {"name": "verify", "agent": "verifier", "for_each": "scan", "verify": true, "per_run": 4,
       "goal": "Check that these findings are real and the proposed fixes are right."},
      {"name": "fix", "agent": "builder", "for_each": "verify",
       "goal": "Apply the fix for this finding and run its tests.",
       "context": "Test command: python -m pytest -q tests/dates"},
      {"name": "check", "agent": "reviewer", "attach_diff": true,
       "goal": "Done check of every parse_date() replacement."}
    ]
  }
}
```

Stage fields:

- `name` (required, unique, `[a-z][a-z0-9_-]{0,31}`), `agent`, `goal`, `context`, `attach_diff`,
  `diff_base`: as in a task, parsed by the shared `_parse_task`. `workdir` per stage or once for
  the plan.
- `split` (optional, only without `for_each`): a list of up to `plan_max_items` strings. One run
  per entry; the entry is appended to the goal as "Your part: <entry>". Without `split` and
  `for_each` the stage is one run.
- `for_each` (optional): the name of an earlier stage. Runs over the items that stage produced
  (see "Items"), the items added to each run's context as JSON.
- `per_run` (optional, default 1, max 8): items per run in a `for_each` stage. Verify stages
  should batch (one verifier per 4 items costs a quarter of the cold starts).
- `verify` (optional bool): the stage filters items (see "Verification").
- A writer stage with `for_each` gets `write_scope` from its items' `files` (see "Scope rule").

Validation happens before anything starts, with the same refusals as `tasks` plus `invalid_plan`
(shape, names, `for_each` pointing forward or to an unknown stage, `split` with `for_each`, both
or neither of `plan` and `tasks`, more than 8 stages). The `max_parallel` cap of a `tasks` call
does not apply to a stage; stages launch in waves instead.

## Items: how stages talk

A stage that a later stage names in `for_each` must hand over items. The plugin adds one rule to
the context of exactly those runs:

    End your report with one fenced json block: {"items": [{"title": "...",
    "files": ["/abs/path", ...], "detail": "..."}]}. One item per finding. files lists the files
    a fix would change. Use {"items": []} when there is nothing.

- The plugin parses the LAST fenced `json` block of each run's summary (64 KB cap). `title` is
  required; `files` (at most 8) and `detail` (clipped to 2,000 characters) are optional; unknown
  keys are dropped. The plugin always assigns ids itself (`scan-1`, `scan-2`, ...), so ids never
  collide across runs.
- Items of all runs of a stage are concatenated in run order and deduplicated on
  (`title`, `files`).
- A run whose summary has no valid block fails the stage (`items_missing`). The plan stops there
  and the result names the run and quotes the end of its summary: the lead always has a way out.
- The plan keeps its own copy of every item and of each run's summary per item, because finished
  runs are evicted after 256 (`_track`) and lifecycle results expire after an hour.

## Scope rule for plan writers

Item `files` come from a model that read the repository, which may contain prompt injection, so
they never widen what a writer may touch:

- Every path must resolve (realpath) inside the stage's workdir, must not be the workdir itself,
  and must be a file path or a folder at most two levels below the workdir. An item that breaks
  this is not fixed; it appears in the result as `refused_scope`.
- The scope of a run is the union of its items' files. Runs with disjoint scopes run in
  parallel through the existing `_check_writers`; items without `files` become unscoped writers,
  one at a time, and the result says so (slow path).

## Verification

A stage with `verify: true` uses the same items block, with one difference in its rule: "Repeat
only the findings you confirmed with file:line evidence, and add `"rejected": [{"title": ...,
"reason": ...}]` for the rest." Only items in `items` go on to the next stage. Items that came in
but are neither confirmed nor listed as rejected count as rejected (`verdict_missing`), never as
confirmed.

New built-in agent `agents/verifier.md` (added in Part B): tier balanced, read-only (same tools as `reviewer`),
instructions: check each finding against the code yourself, do not trust its reasoning, confirm
only what you can point to with file:line. Balanced, not strong: verification runs per batch of
items, so it has to be cheap; the strong reviewer stays the single final check. No new frontmatter
key: the agent file stays in the Claude Code format.

## Execution

A plan advances only inside the lead's own `agents_run` and `agents_status` waits:

- `_advance(plan)` is called from the existing `_wait` loop next to `_poll`. When every run of the
  current stage is finished, it parses items, builds the next stage's tasks and launches them in
  waves under `self._admit`, through the existing `_launch` and `_check_writers`. A writer that
  does not fit (`Refusal`) waits for the next wave instead of failing the plan.
- Interrupt, new-message yield and the tool deadline come for free from `_wait`. A plan that
  outlives the wait returns a one-line progress note (stage, runs done, items so far) and its
  `plan_id`; the lead continues with `agents_status(plan_id=..., wait_seconds=...)`. Cost: one
  short round trip about every 395 seconds with the default 420-second deadline.
- Why no background thread: the Opus review showed that a thread started with
  `copy_context()` can still launch after the tool returned, but against a stale parent. After
  context compression `_record` (subagent_lifecycle.py, session-id check) no longer finds the
  handles, so status, result and cancel break; after `/new` the thread launches under a closed
  agent; interrupts are per thread and never reach it; and live children between turns make the
  gateway queue the user's interrupts. Advancing only inside a live wait avoids all of that.
- `spawn_paused` is checked before every wave, not only at admission.
- One live plan per conversation (`plan_running` refusal), owned through `_owns`, so it survives a
  session-id change.
- Runs launched by a plan are ordinary `Run` records with `plan_id` set, so the guard, the policy
  registry, cancellation and `/crew` treat them as usual. They are marked reported once
  the plan consumed them, so `agents_status` without arguments does not dump them on the lead.
- A stage with no items (nothing found, or everything rejected) ends the plan early with status
  `succeeded`; later stages show as `skipped`.
- A failed run (any status other than succeeded) fails its stage and the plan. Cancelling the plan
  cancels its running runs.
- Parallelism is bounded by the lifecycle pool (8 workers): runs past 8 queue as pending. Waves
  therefore launch at most `max_parallel` runs, and the result reports the wall-clock time per
  stage so the measurement in Task 10 shows whether the pool is the bottleneck.

## What the lead gets back

```json
{"ok": true, "plan": {"plan_id": "pl_...", "status": "succeeded", "duration_seconds": 412.0,
  "stages": [{"name": "scan", "runs": 3, "items": 9, "seconds": 61.0},
             {"name": "verify", "runs": 3, "confirmed": 6, "rejected": 3, "seconds": 80.2},
             {"name": "fix", "runs": 6, "succeeded": 6, "seconds": 190.4},
             {"name": "check", "runs": 1, "succeeded": 1, "seconds": 80.4}],
  "items": [{"id": "scan-1", "title": "...", "status": "fixed"},
            {"id": "scan-4", "title": "...", "status": "rejected", "reason": "..."}],
  "final": "<summary of the last stage's run, clipped>"}}
```

- Budget: a constant of 8,000 characters in total. Item lines are clipped to 200 characters; the
  last stage's summary gets what is left, at least 2,000. No new setting.
- `agents_status` gets `plan_id` and an optional `item` (an item id): it returns that one item's
  full trail (each run's summary for it). That is how the lead looks closer when something looks
  wrong, without paying for everything.
- `agents_status` with `plan_id` and `cancel: true` cancels the plan.

## Prompt guide

One guide line in `_PROMPT_TAIL` (the prompt budget is 3,800 characters; shorten another line if
the render test fails): "Plans for big jobs: when a task splits into many independent items
(roughly 5 or more files, findings or pages) that each go through the same steps, pass agents_run
a plan (scan, verify, fix, check) instead of chaining calls; you read only the final result. Small
jobs stay tasks or your own work."

No nudge in 0.7.0: measure first whether the guide line is enough.

## Settings

One new setting in `plugin.yaml`: `plan_max_items` (default 24, 1-64), the most items one stage
may fan out over. Over it the stage fails with `too_many_items` and the result lists the item
titles, so the lead can split the job. The total number of runs is already bounded by
`plan_max_items` x 8 stages and `max_live_runs`.

## Tasks

Each task: test first, then code, then `ruff check .` and `python -m pytest -q`.

0. Purpose section in AGENTS.md (done with this plan).
1. Extract `_parse_task(item, where, settings, roster)` from `_parse_tasks`; no behavior change,
   existing tests stay green. Files: `agents_service.py`.
2. `agents/verifier.md` and its roster test. Files: `agents/verifier.md`, `tests/test_config.py`.
3. New module `agents_plan.py` with pure functions: `parse_plan`, `parse_items`,
   `parse_verify`, `plan_scope` (the scope rule). Tests in `tests/test_plan.py`: several and
   nested fenced blocks, block not last, oversized JSON, missing `title`, renumbered ids,
   dedupe, malicious `files` (`/`, `..`, outside the workdir, the workdir itself, too deep, more
   than 8), goal plus "Your part:" over `MAX_GOAL_CHARS`, forward `for_each`.
4. `Plan` record, `plan_id` on `Run`, `_advance` with waves under `_admit`, called from `_wait`.
   Tests with the fake lifecycle: a full scan -> verify -> fix -> check plan; an empty stage ends
   early; a failed run stops the plan; a writer that does not fit waits a wave; a concurrent
   `agents_run` writer during a plan builder; `spawn_paused` between stages; a session-id change
   mid-plan (mutate `parent_agent.session_id`); a new parent object for the same session.
5. Result shape, progress note, `agents_status` `plan_id`, `item` and `cancel`. Files:
   `agents_service.py`, `agents_tools.py` (schema), `tests/test_service.py`.
6. Integration test with the real loader in a temporary HERMES_HOME: a two-stage plan through the
   real lifecycle API (child model run stubbed as in the existing integration tests).
7. Prompt guide line; the render test keeps the section under its budget.
8. `plan_max_items` setting, README section "Plans", CHANGELOG 0.7.0 with the expected effect,
   version 0.7.0.
9. Checks from AGENTS.md: ruff, pytest against oldest and main, `hermes plugins doctor . --ci`,
   `hermes plugins validate .`.
10. Measure (AGENTS.md, Purpose): the same job twice in a test repo, once as chained
    `agents_run` calls and once as a plan (for example: replace a helper in 8 files). Compare lead
    input and output tokens (session usage), wall-clock time and total child `api_calls`.
    Record the numbers in the CHANGELOG entry. Ship only if the plan wins on lead tokens and is
    not slower.

## Risks

- Models that skip the json block. Mitigation: the rule is in the context of exactly the runs
  that need it, failure is loud, and the lead gets the end of the summary to recover.
- Cost of a big fan-out. Mitigation: `plan_max_items`, `per_run` batching for verify stages, and
  the verifier on the balanced tier (or a local fast model) rather than the strong tier.
- Silent wrong results the lead never sees. Mitigation: rejected and refused items with reasons
  are always in the result, and `agents_status item` gives the full trail.
- Stalls: a plan only advances while the lead waits. The progress note and the guide line tell the
  lead to keep calling `agents_status`; the final measurement shows whether that costs time.

## Review outcome (Opus reviewer, 2026-10-16)

Taken over from the review:

- B1: the background driver thread is dropped; plans advance inside `_wait` only.
- B2: plan-derived write scopes must stay inside the workdir (scope rule plus malicious tests).
- B3: `_parse_tasks` is split into `_parse_task` rather than reused with its `max_parallel` cap.
- Waves run under `_admit`; a writer that does not fit is a caught `Refusal`, not a second check.
- No `verdict` frontmatter key; verify is a stage flag and uses the same items block.
- `plan_report_chars`, `plan_max_runs` and the plan nudge are cut; the report budget is a
  constant.
- Plans keep their own item trail (run eviction, one-hour lifecycle retention).
- Batching for verify stages (`per_run`), because the lifecycle pool has 8 workers.

Open from the review: whether the oldest supported Hermes compares session ids in `_record` the
same way. The plan no longer depends on it, but Task 4's session-change test runs against both.

## Later (not in 0.7.0)

- Fix loop: a builder item that fails its tests gets one more round with the failure in context;
  stop after two rounds without progress.
- Git worktree per builder for items that touch the same file, merged by the plugin in
  `agents_git.py`.
- A plan nudge, if the measurement shows the lead keeps chaining `agents_run` calls.
- Plans that span several repositories.
- Per-endpoint concurrency (`max_concurrent` per provider + model) as wave admission: wait, never
  refuse.
- Neutral toolset id and agent folder, with their own migration.
