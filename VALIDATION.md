# Validation

This adaptation retains the upstream unit tests, real-loader integration tests,
and CI matrix for Hermes v0.21.6 and main. Tests were updated for Codex routing,
preserved Claude defaults, additive GPT roles, reasoning effort, and explicit provider inheritance.

Local checks:

- Python syntax parsing: passed.
- `python3 tests/smoke_check.py`: 10 tests passed; dependency-free fake-host tests exercise
  roster defaults, registration, cross-provider routing, read-only/no-nesting
  enforcement before launch returns, old-host refusal, collection and cancellation, mixed-provider runs and shared writer limits.
- Full pytest, ruff, Hermes plugin doctor and validate: not run here. This
  environment lacks the dependencies and cannot resolve package download hosts.
- Live Claude/Codex subscription inference: not run. No credentials were read or changed.

Before release, run the CI matrix and a live smoke test in a temporary workspace:

1. Keep the existing Claude provider authenticated; add `hermes auth add openai-codex`.
2. Install/enable the plugin and restart the session.
3. Check `/codex-agents` for available roster and provider/effort support.
4. Ask codex-explorer to summarize a small local file, then collect with `agents_status`.
5. Confirm the child session records `openai-codex` and the selected account-visible
   GPT model. Confirm a write attempt by explorer is blocked.
6. Ask codex-builder to make one small change in the temporary workspace, review the
   resulting diff, and check cancellation of a separate pending run.
7. Repeat with the original explorer/builder roles and confirm their Claude route.
   Run reviewer and codex-reviewer together to verify both providers in one session.

The plugin does not configure global fallback providers or tool-service billing.
Review those settings if all inference must stay on the subscription route.
