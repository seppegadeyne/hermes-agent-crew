# Purpose

The plugin exists for two goals, and every change has to serve them:

1. Finish tasks faster: parallel agents, fewer round trips through the lead.
2. Use fewer tokens, above all in the lead's context, which is the most expensive one: agents
   read and search in their own context and hand back compact reports, never transcripts.

- A change must improve at least one goal and must not make the other worse. Name the expected
  effect in the `CHANGELOG.md` entry, and where practical check it with a live run (lead tokens,
  wall-clock time, child `api_calls`) before you call it done. `docs/measuring.md` lists the
  measurement scripts (for the scouting nudge: `scripts/nudge_report.py`) and how to act on them.
- Build on what exists: extend the current tools, guard and service. Do not rewrite. A new
  feature reuses the existing launch, policy and writer checks rather than adding a parallel path.
- Opt-in, never automatic: the lead decides when to delegate. Small tasks stay with the lead,
  because every agent starts with an empty context.
- Provider-neutral: the lead (orchestrator) is whatever model the Hermes session runs on, and an
  agent can run on any provider Hermes knows, including local endpoints. Never hard-code a
  vendor or model in code, prompts or tool descriptions; vendors appear only in agent files and
  settings.
- The safety rules below are not trade-offs against speed or tokens.

# Development conventions

- Use documented Hermes plugin surfaces only: `ctx.register_tool`, `register_hook`,
  `register_command`, `register_system_prompt_section`, `get_config`, `subagent_lifecycle` and the
  public `agent.subagent_lifecycle` module. Every other Hermes import lives in `agents_host.py`,
  wrapped so that a moved symbol degrades to "unknown" instead of breaking the plugin.
- Never patch or monkeypatch Hermes core at runtime.
- `pre_tool_call` stays total and constant-time: Hermes blocks every tool call, the lead's
  included, when that callback raises or hangs. Return None for calls the plugin does not own;
  fail closed only for its own children.
- Agents never start agents. Read-only means the allowlist in `agents_config.READONLY_TOOLS`;
  widen it only together with a test.
- Git runs only in `agents_git.py`: no shell, every repository-configured program disabled. Keep
  the malicious-filter test green.
- Tests never touch a real Hermes home. Unit tests use the fake host in `tests/conftest.py`;
  integration tests use Hermes' real loader in a temporary `HERMES_HOME`.
- Before a push: `ruff check .`, `python -m pytest -q` against the oldest supported Hermes and
  against `main` (`HERMES_SOURCE=/path/to/checkout`), `hermes plugins doctor . --ci` and
  `hermes plugins validate .`.
- Bump `version` in `plugin.yaml` and add a `CHANGELOG.md` entry for every behavior change. Raise
  `requires_hermes` when the plugin starts to need a newer surface, and move the CI pin with it.
- US English for docs, comments and messages. No private paths, hostnames, credentials or
  telemetry.
