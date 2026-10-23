"""Durable run ledger: what every agent run was, where it worked and how it ended.

Runs live in memory, so a crash, a restart or a new session used to lose them, and with them the
worktree, branch and report a lead needs to pick the work up again. The ledger is one append-only
JSON Lines file in the profile's plugin data folder: one full snapshot of a run per line, the last
line of a run_id wins. It is best-effort by design: every write or read error is swallowed and
every lock is taken without blocking, so the ledger can never fail or stall a launch, a wait or a hook.

A run the ledger still lists as running, written by a process that is gone, is reported as
``lost``: its child died with that process, but its folder and branch say where its work is.
"Gone" is decided on the process's start time where the OS exposes it, so a reused pid (common in
containers, where Hermes is often pid 1) does not keep a dead run alive.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

try:  # POSIX: serialize appends and compaction across Hermes processes that share one profile
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

FILE_NAME = "runs.jsonl"
MAX_RECORDS = 200           # newest runs kept; older ones drop out at the next compaction
COMPACT_FACTOR = 3          # rewrite the file once it holds this many lines per kept record
GOAL_CHARS = 600
SUMMARY_CHARS = 4_000       # enough for a follow_up (it clips the earlier report to the same size)
ERROR_CHARS = 1_000
MAX_LINE_CHARS = 16_000
LOCK_TRIES = 20             # non-blocking lock attempts ...
LOCK_PAUSE_SECONDS = 0.01   # ... this far apart: at most about 0.2 s, then the write or read is skipped
FIELDS = ("run_id", "agent", "status", "goal", "readonly", "workdir", "write_scope", "branch", "head_sha",
          "model", "provider", "owner_session_id", "child_session_id", "plan_id", "started_at", "finished_at",
          "summary", "error", "api_calls", "duration_seconds")
LOST_ERROR = ("The Hermes process that ran this agent stopped (crash, restart or exit) before its result was "
              "collected, so the outcome is unknown. Its edits, if any, are still in its workdir/branch.")
PROCESS_TOKEN = uuid.uuid4().hex[:12]  # tells this process apart from an earlier one with the same pid


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n[… {len(value) - limit} more characters]"
    return value


def _start_time(pid: int) -> str | None:
    """Start time of ``pid`` in clock ticks since boot (Linux /proc), else None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        return stat[stat.rindex(")") + 2:].split()[19]  # field 22; the name in (...) may hold spaces
    except Exception:
        return None


_OWN_START = _start_time(os.getpid())


def _alive(item: dict) -> bool:
    """True while the process that wrote ``item`` may still be running it."""
    pid = item.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return item.get("proc") == PROCESS_TOKEN  # same pid, earlier process: gone
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # exists, owned by someone else: decided by its start time below
    recorded, current = item.get("pid_start"), _start_time(pid)
    if recorded and current:
        return recorded == current  # a reused pid has another start time
    return True


def _try_lock(handle: Any, mode: int) -> bool:
    if fcntl is None:
        return True
    for attempt in range(LOCK_TRIES):
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if attempt + 1 < LOCK_TRIES:
                time.sleep(LOCK_PAUSE_SECONDS)
        except OSError:
            return False
    return False


class RunLedger:
    def __init__(self, folder_getter, *, wall=time.time, enabled=lambda: True) -> None:
        self._folder_getter = folder_getter  # resolved lazily: HERMES_HOME may change before first use
        self._enabled = enabled
        self._wall = wall
        self._lock = threading.Lock()
        self._appends = None  # appends since the last compaction check; None = check at the first write

    # ------------------------------------------------------------------ paths

    def path(self) -> Path | None:
        try:
            if not self._enabled():
                return None
            folder = self._folder_getter()
            return Path(folder) / FILE_NAME if folder else None
        except Exception:
            return None

    # ------------------------------------------------------------------ writing

    def record(self, run: Any) -> None:
        """Append one snapshot of ``run``. Never raises, never blocks for long."""
        try:
            path = self.path()
            if path is None:
                return
            snap = {name: getattr(run, name, None) for name in FIELDS}
            snap["write_scope"] = list(snap.get("write_scope") or ())
            snap["goal"] = _clip(" ".join(str(snap.get("goal") or "").split()), GOAL_CHARS)
            snap["summary"] = _clip(snap.get("summary"), SUMMARY_CHARS)
            snap["error"] = _clip(snap.get("error"), ERROR_CHARS)
            if snap.get("status") != "running" and not snap.get("finished_at"):
                snap["finished_at"] = self._wall()
            snap.update(pid=os.getpid(), proc=PROCESS_TOKEN, pid_start=_OWN_START)
            snap = {k: v for k, v in snap.items() if v not in (None, [], "")}
            line = json.dumps(snap, ensure_ascii=False, default=str)
            if len(line) > MAX_LINE_CHARS:
                snap.pop("summary", None)
                snap["note"] = "report too long for the ledger"
                line = json.dumps(snap, ensure_ascii=False, default=str)
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._appends is None or self._appends >= MAX_RECORDS * COMPACT_FACTOR:
                    self._appends = 0
                    self._compact(path)
                with open(path, "a", encoding="utf-8") as handle:
                    if _try_lock(handle, fcntl.LOCK_EX if fcntl else 0):
                        handle.write(line + "\n")
                        self._appends += 1
        except Exception:
            return

    def _compact(self, path: Path) -> None:
        """Drop superseded snapshots and the oldest runs when the file has grown (first write of a process,
        then every MAX_RECORDS * COMPACT_FACTOR appends)."""
        try:
            with open(path, "r+", encoding="utf-8") as handle:
                if not _try_lock(handle, fcntl.LOCK_EX if fcntl else 0):
                    return
                lines = handle.read().splitlines()
                records = self._parse(lines)
                if len(lines) <= max(len(records), 1) * COMPACT_FACTOR and len(records) <= MAX_RECORDS:
                    return
                kept = list(records.values())[-MAX_RECORDS:]
                handle.seek(0)
                handle.truncate()
                handle.write("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in kept))
        except FileNotFoundError:
            return
        except Exception:
            return

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _parse(lines: list) -> OrderedDict:
        """run_id -> newest snapshot, ordered by the run's latest write (so compaction keeps recent activity)."""
        records: OrderedDict[str, dict] = OrderedDict()
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash
            run_id = item.get("run_id") if isinstance(item, dict) else None
            if isinstance(run_id, str):
                records.pop(run_id, None)
                records[run_id] = item
        return records

    def load(self) -> OrderedDict:
        """Every known run, with dead ``running`` runs marked ``lost``. Never raises."""
        path = self.path()
        if path is None:
            return OrderedDict()
        try:
            with open(path, encoding="utf-8") as handle:
                if not _try_lock(handle, fcntl.LOCK_SH if fcntl else 0):
                    return OrderedDict()  # another process is compacting right now
                records = self._parse(handle.read().splitlines())
        except Exception:
            return OrderedDict()
        for item in records.values():
            if item.get("status") == "running" and not _alive(item):
                item["status"] = "lost"
                item["error"] = LOST_ERROR
        return records

    def get(self, run_id: str) -> dict | None:
        return self.load().get(run_id)

    def recent(self, limit: int) -> list:
        """The ``limit`` most recently started runs, newest first."""
        def started(item: dict) -> float:
            value = item.get("started_at")
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        return sorted(self.load().values(), key=started, reverse=True)[:max(0, limit)]
