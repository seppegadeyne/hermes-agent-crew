# Measuring the plugin against real sessions

AGENTS.md asks every change to name its expected effect and, where practical, to check it in live
sessions. This page lists the measurements that have a script, and how to act on them.

## Scouting nudge

```sh
python scripts/nudge_report.py --days 14
python scripts/nudge_report.py --days 7 --skip <id of the session running this>
```

Read-only: it opens every profile's `state.db` under `HERMES_HOME` (or `--home`) and writes
nothing. Run it with a Python that can import the plugin (Hermes' own venv works).

What it prints:

- **Shown**: the nudges the plugin really added and what the lead did after them in the same turn.
  The key line is *followed by an explorer/researcher*: the follow rate.
- **Replay**: the same turns under other trigger rules. *fires* is how often the hint would appear,
  *useful* how often at least 20,000 more characters were read before the next edit, and
  *break-even* the follow rate a rule needs before it saves more context than the hint costs.

How to act on it:

- Follow rate at or above the current rule's break-even: keep the nudge; consider a rule with more
  useful fires.
- Follow rate below break-even but above zero: move to the rule with the lowest break-even that
  still catches most useful turns (change `nudge_after` / `nudge_min_chars` defaults).
- Follow rate zero over two weeks with 10+ nudges shown: the hint does not change behavior. Set the
  default `nudge_after` to 0 (off) and say so in the CHANGELOG.
- Record the numbers you based a change on in its CHANGELOG entry, like 0.12.0 does.

Caveats: an edit is detected from tool names and a few shell patterns, so some terminal edits are
missed; "useful" and the 3,000-character report size are assumptions, the same ones 0.12.0 used.
