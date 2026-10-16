# hermes-agent-crew

Named subagents for [Hermes Agent](https://github.com/NousResearch/hermes-agent), on any provider
Hermes knows, local models included: **strong models review, balanced models build, fast models
swarm.**

Formerly `hermes-claude-agents`; see [Upgrading from hermes-claude-agents](#upgrading-from-hermes-claude-agents).

The lead is the model your Hermes session runs on; the plugin never picks or changes it. The lead
picks an agent by name. The operator's Markdown files and settings decide each agent's model,
provider, tools and whether it is read-only, so the lead never chooses a model, a provider or a tool
set itself. Read-only agents are enforced by a `pre_tool_call` guard, not by instructions.
Children run through Hermes' public subagent lifecycle API: no core patch, no fork of
`delegate_task`.

## Why

- **Token efficiency.** Scouts read the files and run the searches in their own context; the lead
  gets one compact report per agent, never a transcript.
- **Senior review on call.** A strong-tier reviewer checks a plan before it is locked, looks for the root
  cause when the same test fails twice, and reviews the diff before work is called done. With
  `attach_diff` the diff goes into the reviewer's context, not the lead's.
- **Cheaper volume work.** Point the fast tier at a local or low-cost model and the scouting runs
  there, while the reviewer stays on a strong model.
- **Operator control.** Models, providers, tools and limits live in files and settings that the
  operator owns.

## Built-in agents

| Agent | Tier (default model) | Access | Use it for |
|---|---|---|---|
| `reviewer` | strong (opus) | read-only | plan checks, repeated failures, done checks (`attach_diff: true`) |
| `explorer` | fast (haiku) | read-only | finding files, symbols, call sites and config; several in parallel |
| `researcher` | fast (haiku) | read-only | library, API and spec documentation, quoted with URLs |
| `builder` | balanced (sonnet) | can change files | one scoped, well-specified change plus its tests; parallel with disjoint `write_scope` |

Built-in agents name a tier, not a model. By default the tiers map to the `opus`, `sonnet` and
`haiku` aliases of the
[Claude subscription provider](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk)
(`claude-subscription-directsdk-experimental`), the default provider. The `tiers` setting moves a
tier to any other model and provider (see [Model tiers](#model-tiers)).

## Install

```bash
hermes plugins install seppegadeyne/hermes-agent-crew --enable
```

Start a new session (or restart the gateway): plugins load at startup. `/crew` lists the
roster (`/claude-agents` still works for one release), the agent folders and any problems in
agent files.

Requirements:

- Hermes 0.21.6 or newer (the subagent lifecycle API).
- In-process plugins, the default `plugins.isolation: in_process`. A launch has to bind to the
  running agent turn, which the plugin host process cannot see; under host isolation both tools
  refuse with a clear message.
- A provider that knows the model names in your agent files and tiers.

## Tools

`agents_run` starts one or more agents in parallel and waits for their final reports:

```json
{
  "tasks": [
    {"agent": "explorer", "goal": "Find where sessions are persisted and who calls it.",
     "context": "Python repo; storage code lives under src/store/.", "workdir": "/srv/app"},
    {"agent": "reviewer", "goal": "Done check of the session-storage change.", "workdir": "/srv/app",
     "attach_diff": true}
  ],
  "wait_seconds": 300
}
```

- `goal` is the complete task; `context` carries the facts. Agents cannot see the conversation.
- `workdir` is the absolute path of the folder the agent works in, usually a repository root. It
  defaults to the session's working directory. Pass it whenever the work is somewhere else: a
  subagent has no other reliable notion of "the current directory".
- `attach_diff` adds `git status`, `--stat` and the diff of the working tree in `workdir` (against
  `diff_base`, default `HEAD`) to the agent's context.
- The wait is capped 25 seconds below Hermes' tool deadline (`timeouts.tools.sequential_call`,
  default 420 seconds). Agents that are still working come back as `run_ids`.

`agents_status` collects, waits for or cancels those runs. Without `run_ids` it returns every run
whose result the lead has not seen yet.

With tool search enabled, Hermes may defer both tools behind `tool_search`; the plugin's prompt
section tells the model to call them through `tool_call` then.

Scouting nudge: when the lead makes `nudge_after` (default 8) read or search calls in one turn
without using agents, the plugin appends a one-line hint to that tool result that names the
read-only agents. It fires once per turn, never for subagents, and never after the turn used
agents.

## Agent files

An agent is a Markdown file with a small frontmatter block, the format Claude Code uses for
`~/.claude/agents/*.md`:

```markdown
---
name: security-reviewer
description: Read-only security review of a diff.
tier: strong
readonly: true
---
You review changes for injection, authentication and secret-handling bugs. Cite file:line.
```

| Key | Meaning |
|---|---|
| `name` | Lowercase letters, digits, `-` or `_` (max 32). Defaults to the file name. |
| `description` | Shown to the lead in the roster (clipped there to fit the prompt budget). |
| `use_when` | Short trigger shown whole before the description (over the prompt budget, descriptions shrink first), for example `a whole text file needs the language MCP checks`. This is what makes the lead pick the agent; `when_to_use` works too. Max 200 characters. |
| `model` | Model name for the agent's provider; `inherit` uses the lead's model. Wins over `tier`; `inherit` cannot be combined with `tier`. |
| `tier` | `strong`, `balanced` or `fast`: the `tiers` setting decides the model (and optionally the provider). |
| `provider` | Provider for this agent; `inherit` passes none (see below). |
| `readonly` | `true` limits the agent to read tools (see [Read-only](#how-read-only-is-enforced)). |
| `tools` | Allowed tools: Hermes names, MCP names with `*` globs, or Claude Code names. |
| `toolsets` | Hermes toolsets the child's tool schema is narrowed to. |
| `effort` | Reasoning effort, on Hermes builds whose lifecycle API accepts it. |

Claude Code tool names map to Hermes tools: `Read` to `read_file`; `Grep`, `Glob` and `LS` to
`search_files`; `WebFetch` and `WebSearch` to `web_extract` and `web_search`; `Edit`, `MultiEdit`
and `NotebookEdit` to `patch`; `Write` to `write_file`; `Bash` to `terminal`. `Task` is dropped
(agents never start agents) and `color` is ignored, so Claude Code agent files load unchanged. An
agent limited to read tools is read-only automatically.

Folders, read in this order (a later file with the same name replaces an earlier one):

1. the built-in agents in this plugin;
2. `~/.claude/agents`, when `import_claude_code_agents` is on;
3. every folder in `agent_dirs`, by default `{hermes_home}/claude-agents`.

## Configuration

Settings live under `plugins.entries.hermes-agent-crew.settings` in Hermes' `config.yaml`:

```yaml
plugins:
  entries:
    hermes-agent-crew:
      settings:
        tiers:
          fast:  # first reachable route runs
            - {provider: my-local-server, model: qwen}
            - {model: haiku}
        overrides:
          reviewer: {model: sonnet}
          builder: {enabled: false}
        max_parallel_writers: 1
```

| Setting | Default | Meaning |
|---|---|---|
| `provider` | `claude-subscription-directsdk-experimental` | Provider for agents without their own; `inherit` passes none. |
| `agent_dirs` | `["{hermes_home}/claude-agents"]` | Extra agent folders. |
| `import_claude_code_agents` | `false` | Also load `~/.claude/agents`. |
| `overrides` | `{}` | Per agent: `model`, `provider`, `effort`, `enabled`. |
| `tiers` | strong: opus, balanced: sonnet, fast: haiku | Per tier: `{model, provider?}`, or a list of up to 4 of those tried in order. |
| `max_parallel` | `12` | Tasks per `agents_run` call (1-16). |
| `max_live_runs` | `24` | Runs in progress per conversation (1-32). |
| `max_parallel_writers` | `1` | Agents that can change files running at once while one of them has no `write_scope` (0-8). Writers that all have disjoint scopes run side by side; `0` turns writers off. |
| `wait_seconds` | `600` | Default wait, always capped below the tool deadline. |
| `diff_max_chars` | `20000` | Largest diff attached with `attach_diff`. |
| `summary_max_chars` | `12000` | Longest report returned per run. |
| `prompt_section` | `true` | Add the roster and a short usage guide to the lead's system prompt. |
| `nudge_after` | `8` | After this many read or search calls by the lead in one turn without agents, append a one-time hint to use read-only agents. `0` turns it off. |

### Model tiers

Model and provider are resolved one field at a time, most specific first:

- model: `overrides.<agent>.model`, then the agent file's `model`, then its tier's `model`, then the
  lead's model;
- provider: `overrides.<agent>.provider`, then the file's `provider`, then the tier's `provider`, then
  the `provider` setting, then none.

A tier without a `provider` keeps using the `provider` setting, so `provider: inherit` still runs the
built-in agents on the lead's provider. Agent files with a `model` and no `tier`, including Claude
Code files, behave as before tiers existed. An unknown tier name is an agent file problem that
`/crew` lists, and an invalid `tiers` entry leaves that tier's agents out instead of falling back to
the default route.

`inherit` means "pass nothing": Hermes then routes the agent through its own `delegation:` settings,
and only when those name no provider or model does the agent run on the lead's route. An override
`provider: inherit` on a tier agent therefore lands on the `provider` setting, not on the lead.

### Local and other providers

A tier or agent can name any provider Hermes resolves, including a named entry under `providers:` in
Hermes' `config.yaml`:

```yaml
providers:
  my-local-server:
    base_url: http://127.0.0.1:8081/v1
    api_mode: chat_completions
    default_model: qwen
```

Hermes' own provider resolution supplies the endpoint and key, so agent files hold no URLs or
secrets. Notes:

- Hermes refuses models with a context window under 64K tokens. Start a local server with at least
  `-c 65536` (llama.cpp) or the equivalent, or the run ends as `launch_failed` or `failed`.
- A `providers:` entry named like a built-in provider is shadowed by the built-in; pick a distinct
  name.
- Start with local models on read-only agents (scouting, verification). Weaker models may skip the
  report format; the guard still holds whatever the model does.
- Local servers are not always running. Give the tier a list of routes and the first one that
  answers runs the agent, for example the local model first and `{model: haiku}` behind it. Before a
  launch the plugin sends `GET <base_url>/models` (2-second timeout) to named `providers:` endpoints
  only; hosted providers are never probed. A refused connection, a timeout or a 5xx (llama.cpp while
  it loads a model) counts as down. Results are cached 60 seconds when up and 15 seconds when down,
  so a server that just started is picked up quickly. The run's `fallback` field says which routes
  were skipped. Named endpoints are recognized the way Hermes itself matches them (normalized names,
  `api`/`url`/`base_url`, `enabled: false`, the legacy `custom_providers` list), and probes never go
  through `HTTP(S)_PROXY`.
- Pinning one field of a multi-route tier (an override or a file with only `model`, or only
  `provider`) picks one route: the other field comes from the first route that agrees with it, so a
  hosted model name is never sent to a local server or the reverse.
- On Hermes builds without per-launch provider routing, routes on another provider than the lead's
  are skipped without a probe.
- With a single route, or when every route is down, the run fails at once with
  `endpoint_unavailable` instead of hanging, and the other agents in the same call still run. A
  fallback to a paid model only happens when you list one.
- `/crew` shows each route with `[down]` when its endpoint does not answer.
- Each run's result lists its `provider` and `model`, so you can see what ran where.

### Provider routing by Hermes build

- Builds whose `SubagentLaunchRequest` has a `provider` field (Hermes `main` since
  [#137167](https://github.com/NousResearch/hermes-agent/pull/137167)) run every agent on its
  configured provider, whatever the lead uses. `inherit` passes no provider; Hermes then follows its
  `delegation` settings, or the lead's provider when those name none.
- Older builds run every agent on the lead's provider. An agent configured for another provider is
  refused before anything starts, with a message that says why.

## How read-only is enforced

- Every tool call a child makes carries `task_id` equal to its subagent id. The plugin records the
  agent's policy in the `subagent_start` hook, which Hermes fires inside the launch, before the
  child can make its first tool call, and checks every call in `pre_tool_call`.
- A read-only agent may use `read_file`, `search_files`, `web_search`, `web_extract`, `x_search`,
  `vision_analyze`, `session_search`, `skill_view`, `skills_list`, `todo_list`, `tool_search` and
  `tool_describe`, plus MCP tools its file lists explicitly. An agent with a `tools:` list may
  always use `tool_search` and `tool_describe`, so it can reach its deferred MCP tools; a call
  through `tool_call` is checked under the real tool's name. Everything else is blocked: file
  writes, `terminal`, `execute_code`, browser actions, memory, scheduling and unlisted MCP tools.
- No agent can start agents: `agents_run`, `agents_status` and `delegate_task` are blocked for
  every child of this plugin.
- The guard only answers for children this plugin launched. Other tool calls pass untouched; an
  internal error for one of its own children blocks the call.
- `attach_diff` runs git without a shell and with every program a repository can configure
  disabled: clean and smudge filters, textconv, external diff drivers, fsmonitor, hooks and the
  pager. Submodules are skipped.

## Parallel writers

Give each writer a `write_scope`: the files or folders it may change, relative to its `workdir` or
absolute. Writers whose scopes do not overlap run in parallel; an unscoped writer still runs alone.

```json
{"tasks": [
  {"agent": "builder", "goal": "...", "workdir": "/abs/site", "write_scope": ["content/a.json"]},
  {"agent": "builder", "goal": "...", "workdir": "/abs/site", "write_scope": ["content/b.json"]}
]}
```

The guard checks the target of every `write_file` and `patch` call (including every file in a V4A
patch, after resolving symlinks) against the scope and blocks the call when it falls outside. A
scoped writer must use absolute paths: Hermes resolves relative and `~` paths against the child's
own cwd and the profile home, which the guard cannot see, so those are blocked with a retry hint.
Overlapping scopes are refused before anything starts.

Limits: shell commands are not path-checked, so a scope keeps the file tools of parallel writers
apart but is not a sandbox. Scopes are host paths and only meaningful with the local terminal
backend (with docker or SSH the check fails closed). Another plugin that rewrites tool arguments
in its own `pre_tool_call` could change a path after this check.

## Limits and differences from `delegate_task`

- Lifecycle launches skip some `delegate_task` gates (`delegation.max_concurrent_children`,
  `max_spawn_depth`), so the plugin enforces its own: tasks per call, live runs per conversation,
  parallel writers, no nesting, and Hermes' spawn pause.
- Children get Hermes' default iteration cap (250), not `delegation.max_iterations`.
  `delegation.child_timeout_seconds` does apply.
- Hermes records each child as its own session (model, provider, usage) and folds its cost into
  the lead's.
- Run state lives in memory. After a restart, unreported runs come back as `unknown`.

## Upgrading from hermes-claude-agents

0.6.0 renamed the plugin from `hermes-claude-agents` to `hermes-agent-crew`. Hermes keys enablement
and settings by plugin id, so move them once, in this order:

1. `hermes plugins disable hermes-claude-agents` (while it is still installed, so Hermes cleans up
   its own records).
2. Install the new id: `hermes plugins install seppegadeyne/hermes-agent-crew`, and remove the old
   folder. If you link the plugin folder into several profiles, rename the links to
   `hermes-agent-crew` too.
3. `hermes plugins enable hermes-agent-crew`.
4. Move `plugins.entries.hermes-claude-agents.settings` to `plugins.entries.hermes-agent-crew.settings`.
5. Restart the gateway, or start a new session.

Never enable both ids at once: both register `agents_run` and `agents_status`. Unchanged on purpose:
the toolset id `claude_agents` (saved `platform_toolsets` lists keep working), the tool names, and the
default agent folder `{hermes_home}/claude-agents`.

## Development

```bash
python -m pytest -q        # with the Python of a Hermes checkout, or HERMES_SOURCE=/path/to/hermes-agent
ruff check .
hermes plugins doctor . --ci
hermes plugins validate .
```

The unit tests use a fake host. The integration tests load the plugin through Hermes' real plugin
loader, hook dispatch and lifecycle API in a temporary `HERMES_HOME`; only the child's model run
is stubbed. A git test proves that a malicious repository filter never runs. CI runs everything
against the oldest supported Hermes release and against Hermes `main`.

See also [hermes-teammates](https://github.com/100yenadmin/hermes-teammates), asynchronous
teammates built on the same lifecycle API.

## License

MIT
