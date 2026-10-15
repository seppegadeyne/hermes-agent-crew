# Heremes-Codex-Agents

Named Claude **and** GPT subagents for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
This extends the original Claude plugin: its four agent names, model aliases,
provider default, tool permissions and instructions remain intact. Four additional
`codex-*` agents use Hermes' native ChatGPT OAuth provider.

## Built-in agents

| Agent | Model | Provider | Effort | Access |
|---|---|---|---|---|
| `reviewer` | opus | Claude subscription* | unchanged | read-only |
| `builder` | sonnet | Claude subscription* | unchanged | writer |
| `explorer` | haiku | Claude subscription* | unchanged | read-only |
| `researcher` | haiku | Claude subscription* | unchanged | read-only |
| `codex-reviewer` | gpt-6.1-sol-900k | openai-codex | high | read-only |
| `codex-builder` | gpt-6.1-sol-900k | openai-codex | medium | writer |
| `codex-explorer` | gpt-6-luna-900k | openai-codex | low | read-only |
| `codex-researcher` | gpt-6-luna-900k | openai-codex | low | read-only |

*The original default is `claude-subscription-directsdk-experimental`, provided by
[the original Claude subscription provider plugin](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk).
Keep that provider installed and authenticated, or retain your existing provider/model
overrides. This extension does not replace Claude authentication.

Claude roles continue to use the operator's global provider default unless overridden.
Codex roles explicitly select `openai-codex`, so adding them does not reroute the original
roles. Both families share the same run limits, cancellation, read-only enforcement,
and one-writer default. The lead chooses an agent name, not a model or provider.

## Install and migrate

1. Use Hermes 0.21.6+ with `plugins.isolation: in_process`. To use both providers in
   the same session, use a current Hermes build whose lifecycle API supports a
   per-launch `provider` field. `/claude-agents` reports this capability.
2. Keep your existing Claude provider setup. To add Codex authentication, run
   `hermes auth add openai-codex` (optionally `--browser`). The lead can remain Claude.
   Choose account-visible GPT models; catalog entries do not guarantee entitlement.
3. Extract this project into `~/.hermes/plugins/heremes-codex-agents` (or your custom
   `HERMES_HOME/plugins`). Copy the original settings block from
   `plugins.entries.hermes-claude-agents.settings` to
   `plugins.entries.heremes-codex-agents.settings`. Settings are not migrated automatically.
4. Disable the original orchestration plugin with
   `hermes plugins disable hermes-claude-agents`, then run
   `hermes plugins enable heremes-codex-agents`. Keep its separate Claude provider enabled.
5. Restart the session/gateway. Both `/claude-agents` and `/codex-agents` show the full roster.

The original custom directory `{hermes_home}/claude-agents` loads first, followed by
`{hermes_home}/codex-agents`; later files with the same name win. If you have a custom
`agent_dirs` setting, retain it and optionally append the Codex folder. Optional
`~/.claude/agents` imports and Claude tool-name mapping are unchanged.

The toolset remains `claude_agents` so existing enabled-toolset configurations continue
to work. Do not enable both orchestration plugins: their tool and command names overlap.
This adaptation has not been published to GitHub; install from the supplied source.

Hermes handles Codex OAuth and refresh; no Codex CLI or API key is required by the
Codex roles. Account limits still apply. This plugin does not alter provider fallback,
auxiliary model or external tool billing settings. Existing Claude billing behavior
belongs to the configured Claude provider. Review those settings separately.

Older Hermes builds refuse cross-provider launches rather than silently rerouting them.
Missing per-agent reasoning-effort support is reported in run results.

## Tools

`agents_run` starts one or more agents in parallel and waits for their final reports:

```json
{
  "tasks": [
    {"agent": "codex-explorer", "goal": "Find where sessions are persisted and who calls it.",
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
description: Read-only security review of a diff (Sol).
provider: openai-codex
model: gpt-6.1-sol-900k
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
(agents never start agents) and `color` is ignored, so Claude Code tool names remain compatible. Imported Claude model aliases are not translated: override them with GPT model IDs before using Codex. An
agent limited to read tools is read-only automatically.

Folders, read in this order (a later file with the same name replaces an earlier one):

1. the built-in agents in this plugin;
2. `~/.claude/agents`, when `import_claude_code_agents` is on;
3. every folder in `agent_dirs`, by default `{hermes_home}/claude-agents` then `{hermes_home}/codex-agents`.

## Configuration

Settings live under `plugins.entries.heremes-codex-agents.settings` in Hermes' `config.yaml`:

```yaml
plugins:
  entries:
    heremes-codex-agents:
      settings:
        overrides:
          reviewer: {model: sonnet}
          codex-reviewer: {model: gpt-6.1-sol-900k, effort: high}
          builder: {enabled: false}
        max_parallel_writers: 1
```

| Setting | Default | Meaning |
|---|---|---|
| `provider` | `claude-subscription-directsdk-experimental` | Provider for agents without their own; `inherit` passes none. |
| `agent_dirs` | `["{hermes_home}/claude-agents", "{hermes_home}/codex-agents"]` | Extra agent folders. |
| `import_claude_code_agents` | `false` | Also load `~/.claude/agents`. |
| `overrides` | `{}` | Per agent: `model`, `provider`, `effort`, `enabled`. |
| `max_parallel` | `12` | Tasks per `agents_run` call (1-16). |
| `max_live_runs` | `24` | Runs in progress per conversation (1-32). |
| `max_parallel_writers` | `1` | Agents that can change files running at once (0-8). |
| `wait_seconds` | `600` | Default wait, always capped below the tool deadline. |
| `diff_max_chars` | `20000` | Largest diff attached with `attach_diff`. |
| `summary_max_chars` | `12000` | Longest report returned per run. |
| `prompt_section` | `true` | Add the roster and a short usage guide to the lead's system prompt. |
| `nudge_after` | `8` | After this many read or search calls by the lead in one turn without agents, append a one-time hint to use read-only agents. `0` turns it off. |

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
python3 tests/smoke_check.py  # dependency-free fake-host smoke checks
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

## Provenance

Adapted from [seppegadeyne/hermes-claude-agents](https://github.com/seppegadeyne/hermes-claude-agents),
commit `0135e9f5098c66d663558c1ff2d2176860b66f37` (v0.4.1).
Original copyright and MIT license retained.

Authentication and defaults were checked against Hermes' `hermes_cli/codex_models.py`
and [provider docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md).
Automated tests use fake model runs; a live subscription smoke test remains necessary.

## License

MIT
