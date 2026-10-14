# hermes-claude-agents

Named Claude subagents for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
**Opus reviews, Sonnet builds, Haiku swarms.**

The lead agent picks an agent by name. The operator's Markdown files decide each agent's model,
its tools and whether it is read-only, so the lead never chooses a model, a provider or a tool
set itself. Read-only agents are enforced by a `pre_tool_call` guard, not by instructions.
Children run through Hermes' public subagent lifecycle API: no core patch, no fork of
`delegate_task`.

## Why

- **Token efficiency.** Scouts read the files and run the searches in their own context; the lead
  gets one compact report per agent, never a transcript.
- **Senior review on call.** An Opus reviewer checks a plan before it is locked, looks for the root
  cause when the same test fails twice, and reviews the diff before work is called done. With
  `attach_diff` the diff goes into the reviewer's context, not the lead's.
- **Operator control.** Models, tools and limits live in files and settings that the operator owns.

## Built-in agents

| Agent | Model | Access | Use it for |
|---|---|---|---|
| `reviewer` | opus | read-only | plan checks, repeated failures, done checks (`attach_diff: true`) |
| `explorer` | haiku | read-only | finding files, symbols, call sites and config; several in parallel |
| `researcher` | haiku | read-only | library, API and spec documentation, quoted with URLs |
| `builder` | sonnet | can change files | one scoped, well-specified change plus its tests; one at a time |

The model names are the `opus`, `sonnet` and `haiku` aliases of the
[Claude subscription provider](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk)
(`claude-subscription-directsdk-experimental`), the default provider for every agent. Other setups
change the provider and models in the settings (see [Configuration](#configuration)).

## Install

```bash
hermes plugins install seppegadeyne/hermes-claude-agents --enable
```

Start a new session (or restart the gateway): plugins load at startup. `/claude-agents` lists the
roster, the agent folders and any problems in agent files.

Requirements:

- Hermes 0.21.6 or newer (the subagent lifecycle API).
- In-process plugins, the default `plugins.isolation: in_process`. A launch has to bind to the
  running agent turn, which the plugin host process cannot see; under host isolation both tools
  refuse with a clear message.
- A provider that knows the model names in your agent files.

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

## Agent files

An agent is a Markdown file with a small frontmatter block, the format Claude Code uses for
`~/.claude/agents/*.md`:

```markdown
---
name: security-reviewer
description: Read-only security review of a diff (Opus).
model: opus
readonly: true
---
You review changes for injection, authentication and secret-handling bugs. Cite file:line.
```

| Key | Meaning |
|---|---|
| `name` | Lowercase letters, digits, `-` or `_` (max 32). Defaults to the file name. |
| `description` | Shown to the lead in the roster (clipped there to fit the prompt budget). |
| `use_when` | Short trigger shown whole before the description (over the prompt budget, descriptions shrink first), for example `a whole text file needs the language MCP checks`. This is what makes the lead pick the agent; `when_to_use` works too. Max 200 characters. |
| `model` | Model name for the agent's provider; `inherit` uses the lead's model. |
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

Settings live under `plugins.entries.hermes-claude-agents.settings` in Hermes' `config.yaml`:

```yaml
plugins:
  entries:
    hermes-claude-agents:
      settings:
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
| `max_parallel` | `12` | Tasks per `agents_run` call (1-16). |
| `max_live_runs` | `24` | Runs in progress per conversation (1-32). |
| `max_parallel_writers` | `1` | Agents that can change files running at once (0-8). |
| `wait_seconds` | `600` | Default wait, always capped below the tool deadline. |
| `diff_max_chars` | `20000` | Largest diff attached with `attach_diff`. |
| `summary_max_chars` | `12000` | Longest report returned per run. |
| `prompt_section` | `true` | Add the roster and a short usage guide to the lead's system prompt. |

Provider routing depends on the Hermes build:

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
  `tool_describe`, plus MCP tools its file lists explicitly. Everything else is blocked: file
  writes, `terminal`, `execute_code`, browser actions, memory, scheduling and unlisted MCP tools.
- No agent can start agents: `agents_run`, `agents_status` and `delegate_task` are blocked for
  every child of this plugin.
- The guard only answers for children this plugin launched. Other tool calls pass untouched; an
  internal error for one of its own children blocks the call.
- `attach_diff` runs git without a shell and with every program a repository can configure
  disabled: clean and smudge filters, textconv, external diff drivers, fsmonitor, hooks and the
  pager. Submodules are skipped.

## Limits and differences from `delegate_task`

- Lifecycle launches skip some `delegate_task` gates (`delegation.max_concurrent_children`,
  `max_spawn_depth`), so the plugin enforces its own: tasks per call, live runs per conversation,
  parallel writers, no nesting, and Hermes' spawn pause.
- Children get Hermes' default iteration cap (250), not `delegation.max_iterations`.
  `delegation.child_timeout_seconds` does apply.
- Hermes records each child as its own session (model, provider, usage) and folds its cost into
  the lead's.
- Run state lives in memory. After a restart, unreported runs come back as `unknown`.

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
