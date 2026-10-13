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
