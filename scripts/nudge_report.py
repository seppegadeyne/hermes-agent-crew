#!/usr/bin/env python3
"""Measure the scouting nudge against real lead sessions (read-only; never writes to Hermes).

Two reports over the last --days of every profile's state.db under HERMES_HOME:

1. Shown: every nudge the plugin really added, and what the lead did afterwards in that turn
   (started an explorer/researcher, kept reading, had already edited). The follow rate is the
   number that decides whether the nudge earns its place.
2. Replay: the same turns replayed under other trigger rules (lookups x characters), with a cost
   (hint characters x later API calls, paid whether or not the lead follows) and a benefit if
   followed (reading a scout could have taken over before the next edit, minus one report, x later
   calls). break-even = cost / benefit = the follow rate the rule needs to pay off.

Usage (from the repository root, with a Python that can import the plugin's modules):
    python scripts/nudge_report.py --days 14
    python scripts/nudge_report.py --days 7 --skip <session_id>  # e.g. the session running this
See docs/measuring.md for how to read the output and what to do with it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUDGE_MARK = "separate reads/searches by hand"
SCOUTS = {"explorer", "researcher"}
REPORT_CHARS = 3_000   # a scout's compact report replaces the reading it took over
USEFUL_CHARS = 20_000  # a nudge is "useful" when at least this much reading followed before the next edit
EDIT_RE = re.compile(r"sed -i|cat >>? |tee |python3? - <<|git (commit|apply|checkout -b|switch -c)|"
                     r">\s*[\w./~$-]+\.(ts|js|svelte|py|md|json|css)\b")


def load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_agent_crew", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_agent_crew"] = module
    spec.loader.exec_module(module)
    from hermes_agent_crew import agents_service
    return agents_service


def state_dbs(home: Path) -> dict:
    dbs = {"default": home / "state.db"} if (home / "state.db").exists() else {}
    for path in sorted(home.glob("profiles/*/state.db")):
        dbs[path.parent.name] = path
    return dbs


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, (dict, list)) else {}


def calls_of(raw: str) -> list:
    """(call id, tool name, args) per tool call, with tool_call bridge calls unwrapped."""
    out = []
    for call in _json(raw) or []:
        fn = call.get("function", call)
        name, args = fn.get("name"), _json(fn.get("arguments"))
        args = args if isinstance(args, dict) else {}
        if name == "tool_call":
            inner = _json(args.get("calls")) if isinstance(args.get("calls"), str) else args.get("calls") or []
            for sub in inner if isinstance(inner, list) else []:
                if isinstance(sub, dict):
                    sub_args = _json(sub.get("arguments") or {})
                    out.append((call.get("id"), sub.get("name"), sub_args if isinstance(sub_args, dict) else {}))
        else:
            out.append((call.get("id"), name, args))
    return out


def is_edit(name: str, args: dict) -> bool:
    return name in ("write_file", "patch") or (name == "terminal" and bool(EDIT_RE.search(args.get("command") or "")))


def collect_turns(svc, dbs: dict, since: float, skip: set) -> list:
    """Per lead turn: a list of (kind, lookup key, result chars, API calls left in the session, nudged, agents)."""
    turns = []
    for db in dbs.values():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        sids = [r[0] for r in con.execute(
            "select id from sessions where parent_session_id is null and started_at > ?", (since,)) if r[0] not in skip]
        for sid in sids:
            rows = con.execute("select role, content, tool_calls, tool_call_id from messages where session_id = ? "
                               "order by id", (sid,)).fetchall()
            results = {r[3]: r[1] or "" for r in rows if r[0] == "tool"}
            total = sum(1 for r in rows if r[0] == "assistant")
            seen = 0
            turn = None
            for role, _content, tool_calls, _ in rows:
                if role == "assistant":
                    seen += 1
                if role == "user":
                    turn = []
                    turns.append(turn)
                elif role == "assistant" and tool_calls and turn is not None:
                    for cid, name, args in calls_of(tool_calls):
                        result = results.get(cid, "")
                        if is_edit(name, args):
                            kind = "edit"
                        elif name in ("agents_run", "agents_status"):
                            kind = "agents"
                        elif svc._is_scouting(name, args):
                            kind = "scout"
                        else:
                            kind = "other"
                        agents = [t.get("agent") for t in args.get("tasks") or [] if isinstance(t, dict)] \
                            if name == "agents_run" else []
                        turn.append({"kind": kind, "key": svc._lookup_key(name, args) if kind == "scout" else None,
                                     "chars": len(result), "left": total - seen, "nudged": NUDGE_MARK in result,
                                     "agents": agents})
        con.close()
    return turns


def shown_report(turns: list) -> None:
    rows = []
    for turn in turns:
        at = next((i for i, c in enumerate(turn) if c["nudged"]), None)
        if at is None:
            continue
        before, after = turn[:at + 1], turn[at + 1:]
        rows.append({
            "followed": any(SCOUTS & set(c["agents"]) for c in after),
            "any_agents": any(c["agents"] for c in after),
            "edited_before": any(c["kind"] == "edit" for c in before),
            "reads_after": sum(1 for c in after if c["kind"] == "scout"),
            "chars_after": sum(c["chars"] for c in after if c["kind"] == "scout"),
        })
    print(f"Shown: {len(rows)} nudges")
    if not rows:
        return
    followed = sum(r["followed"] for r in rows)
    print(f"  followed by an explorer/researcher in the same turn: {followed} ({followed / len(rows):.0%})")
    print(f"  followed by any agents_run (reviewer, builder too):  {sum(r['any_agents'] for r in rows)}")
    print(f"  lead had already edited in that turn:                {sum(r['edited_before'] for r in rows)}")
    print(f"  reads after the nudge, median:                       {statistics.median(r['reads_after'] for r in rows)}")
    print(f"  characters read after the nudge, median:             "
          f"{statistics.median(r['chars_after'] for r in rows):,.0f}")


def replay(turns: list, rule, hint_chars: int) -> tuple:
    fires = useful = cost = benefit = 0
    for turn in turns:
        keys, count, chars = set(), 0, 0
        for i, call in enumerate(turn):
            if call["kind"] == "agents":
                break
            if call["kind"] != "scout" or call["key"] in keys:
                continue
            keys.add(call["key"])
            count, chars = count + 1, chars + call["chars"]
            if not rule(count, chars):
                continue
            fires += 1
            cost += hint_chars * call["left"]
            read, gain = 0, 0
            for later in turn[i + 1:]:
                if later["kind"] == "edit":
                    break
                if later["kind"] == "scout":
                    read += later["chars"]
                    gain += later["chars"] * later["left"]
            benefit += max(0, gain - REPORT_CHARS * call["left"])
            useful += read >= USEFUL_CHARS
            break
    return fires, useful, cost, benefit


def replay_report(turns: list, current: tuple, hint_chars: int) -> None:
    after, min_chars = current
    rules = [
        (f"current: {after} lookups + {min_chars // 1000}k chars", lambda n, c: n >= after and c >= min_chars),
        (f"{after} lookups only", lambda n, c: n >= after),
        (f"{after} lookups + 60k chars", lambda n, c: n >= after and c >= 60_000),
        ("6 lookups + 40k chars", lambda n, c: n >= 6 and c >= 40_000),
        ("50k chars only", lambda n, c: c >= 50_000),
    ]
    print(f"\nReplay over {len(turns)} lead turns (hint of {hint_chars} characters)")
    print(f"  {'rule':34s} {'fires':>5s} {'useful':>6s} {'break-even':>10s}")
    for name, rule in rules:
        fires, useful, cost, benefit = replay(turns, rule, hint_chars)
        even = f"{cost / benefit:.1%}" if benefit else "-"
        print(f"  {name:34s} {fires:5d} {useful:6d} {even:>10s}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--days", type=float, default=14)
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    parser.add_argument("--skip", action="append", default=[], help="session id to leave out (repeatable)")
    parser.add_argument("--hint-chars", type=int, default=470, help="size of the hint the replay charges for")
    args = parser.parse_args()
    svc = load_plugin()
    from hermes_agent_crew.agents_config import DEFAULT_SETTINGS
    dbs = state_dbs(Path(args.home).expanduser())
    if not dbs:
        print(f"No state.db under {args.home}", file=sys.stderr)
        return 1
    turns = collect_turns(svc, dbs, time.time() - args.days * 86_400, set(args.skip))
    print(f"Profiles: {', '.join(dbs)}; last {args.days:g} days\n")
    shown_report(turns)
    replay_report(turns, (DEFAULT_SETTINGS["nudge_after"], DEFAULT_SETTINGS["nudge_min_chars"]), args.hint_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
