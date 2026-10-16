"""Agent plans: declarative stages that the service runs one after another.

Pure functions only (validation, item parsing, the scope rule); the service owns launching and
waiting. A plan is JSON the lead writes; nothing in it is executed.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

MAX_STAGES = 8
MAX_PER_RUN = 8
MAX_SPLIT_CHARS = 2_000
MAX_SUMMARY_PARSE_CHARS = 64_000
MAX_ITEM_FILES = 8
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 2_000
MAX_SCOPE_DEPTH = 2  # a folder in an item's files may be at most this many levels below the workdir
STAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TASK_KEYS = ("agent", "goal", "context", "workdir", "attach_diff", "diff_base", "write_scope")
STAGE_KEYS = frozenset(TASK_KEYS + ("name", "split", "for_each", "per_run", "verify"))
_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n?```", re.S | re.I)

ITEMS_RULE = (
    "- The next stage of this plan works from your findings. End your report with ONE fenced ```json block: "
    '{"items": [{"title": "...", "files": ["/abs/path", ...], "detail": "..."}]}. One item per finding; files '
    'lists the files a fix would change (absolute paths); detail says what to change. Use {"items": []} when '
    "there is nothing.")
VERIFY_RULE = (
    "- You verify the findings in the Items section. Check each one against the code yourself; do not trust "
    "its reasoning. End your report with ONE fenced ```json block: "
    '{"items": [{"id": "...", "title": "..."}], "rejected": [{"id": "...", "reason": "..."}]}. items repeats '
    "only the findings you confirmed with file:line evidence (keep their id); rejected lists the rest with a "
    "short reason.")


class PlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_plan(raw: Any, max_items: int) -> dict:
    """Validate the plan's shape. Returns {"workdir": str|None, "stages": [stage dict, ...]}.

    Each stage dict keeps the task fields for the service's task parser plus name, split,
    for_each, per_run and verify. Raises PlanError.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise PlanError("invalid_plan", "plan must be an object with a stages list.") from None
    if not isinstance(raw, dict):
        raise PlanError("invalid_plan", "plan must be an object with a stages list.")
    unknown = sorted(set(raw) - {"stages", "workdir"})
    if unknown:
        raise PlanError("invalid_plan", f"plan: unknown key(s) {', '.join(unknown)} (use stages, workdir).")
    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise PlanError("invalid_plan", "plan.stages must be a non-empty list.")
    if len(stages_raw) > MAX_STAGES:
        raise PlanError("invalid_plan", f"a plan has at most {MAX_STAGES} stages.")
    workdir = raw.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise PlanError("invalid_plan", "plan.workdir must be a string.")
    seen: list[str] = []
    stages = []
    for index, item in enumerate(stages_raw):
        where = f"plan.stages[{index}]"
        if not isinstance(item, dict):
            raise PlanError("invalid_plan", f"{where} must be an object.")
        unknown = sorted(set(item) - STAGE_KEYS)
        if unknown:
            raise PlanError("invalid_plan", f"{where}: unknown key(s) {', '.join(unknown)}.")
        name = str(item.get("name") or "").strip().lower()
        if not STAGE_NAME_RE.match(name):
            raise PlanError("invalid_plan", f"{where}: name must be lowercase letters, digits, - or _ (max 32).")
        if name in seen:
            raise PlanError("invalid_plan", f"{where}: stage name {name!r} is used twice.")
        for_each = item.get("for_each")
        if for_each is not None:
            for_each = str(for_each).strip().lower()
            if for_each not in seen:
                raise PlanError("invalid_plan", f"{where}: for_each {for_each!r} must name an EARLIER stage.")
        split = item.get("split")
        if split is not None:
            if for_each is not None:
                raise PlanError("invalid_plan", f"{where}: use split or for_each, not both.")
            if not isinstance(split, list) or not split or not all(isinstance(s, str) and s.strip() for s in split):
                raise PlanError("invalid_plan", f"{where}: split must be a non-empty list of strings.")
            if len(split) > max_items:
                raise PlanError("plan_too_large", f"{where}: split has {len(split)} parts; at most {max_items}.")
            if any(len(s) > MAX_SPLIT_CHARS for s in split):
                raise PlanError("invalid_plan", f"{where}: a split part is longer than {MAX_SPLIT_CHARS} characters.")
        per_run = item.get("per_run", 1)
        if isinstance(per_run, bool) or not isinstance(per_run, int) or not 1 <= per_run <= MAX_PER_RUN:
            raise PlanError("invalid_plan", f"{where}: per_run must be a whole number in 1-{MAX_PER_RUN}.")
        if per_run != 1 and for_each is None:
            raise PlanError("invalid_plan", f"{where}: per_run only applies to a for_each stage.")
        verify = item.get("verify", False)
        if not isinstance(verify, bool):
            raise PlanError("invalid_plan", f"{where}: verify must be true or false.")
        if verify and for_each is None:
            raise PlanError("invalid_plan", f"{where}: a verify stage needs for_each (the findings it checks).")
        if for_each is not None and item.get("write_scope") not in (None, "", []):
            raise PlanError("invalid_plan", f"{where}: a for_each stage takes its write scope from the items' files.")
        task = {key: item.get(key) for key in TASK_KEYS}
        if task.get("workdir") in (None, "") and workdir:
            task["workdir"] = workdir
        stages.append({"name": name, "task": task, "split": [s.strip() for s in split] if split else None,
                       "for_each": for_each, "per_run": per_run, "verify": verify, "where": where})
        seen.append(name)
    producers = {s["for_each"] for s in stages if s["for_each"]}
    for stage in stages:
        stage["produces"] = stage["name"] in producers and stage["for_each"] is None
    return {"workdir": workdir, "stages": stages}


def last_json_block(text: Any) -> dict | None:
    """The last fenced ```json block of a report, parsed; None when there is none or it is not an object."""
    if not isinstance(text, str) or not text:
        return None
    text = text[-MAX_SUMMARY_PARSE_CHARS:]
    blocks = _FENCE_RE.findall(text)
    for body in reversed(blocks):
        try:
            data = json.loads(body)
        except ValueError:
            return None  # the last block is the contract; an invalid one is not repaired from an older one
        return data if isinstance(data, dict) else None
    return None


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse_items(summary: Any) -> list[dict] | None:
    """Items from a producer run's report; None when the json block is missing or malformed."""
    data = last_json_block(summary)
    if data is None or not isinstance(data.get("items"), list):
        return None
    items = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            continue
        title = _clip(raw.get("title"), MAX_TITLE_CHARS)
        if not title:
            continue
        files = raw.get("files") or []
        if isinstance(files, str):
            files = [files]
        files = [f.strip() for f in files if isinstance(f, str) and f.strip()][:32] if isinstance(files, list) else []
        detail = raw.get("detail")
        items.append({"title": title, "files": files,
                      "detail": str(detail)[:MAX_DETAIL_CHARS] if isinstance(detail, str) else ""})
    return items


def parse_verdicts(summary: Any, incoming: list[dict]) -> tuple[list[str], dict[str, str]]:
    """(confirmed ids, {rejected id: reason}) for the items a verify run received.

    Matching is by id, then by exact title. An item that is neither confirmed nor rejected is
    rejected with ``verdict_missing``: silence never confirms.
    """
    data = last_json_block(summary)
    by_id = {item["id"]: item for item in incoming}
    by_title = {item["title"].lower(): item["id"] for item in incoming}

    def match(entry: Any) -> str | None:
        if not isinstance(entry, dict):
            return None
        key = entry.get("id")
        if isinstance(key, str) and key.strip() in by_id:
            return key.strip()
        title = entry.get("title")
        return by_title.get(_clip(title, MAX_TITLE_CHARS).lower()) if isinstance(title, str) else None

    confirmed: list[str] = []
    rejected: dict[str, str] = {}
    if data is not None:
        for entry in data.get("rejected") or []:
            found = match(entry)
            if found:
                rejected[found] = _clip(entry.get("reason") or "rejected by the verifier", MAX_TITLE_CHARS)
        for entry in data.get("items") or []:
            found = match(entry)
            if found and found not in rejected and found not in confirmed:
                confirmed.append(found)
    for item_id in by_id:
        if item_id not in confirmed and item_id not in rejected:
            rejected[item_id] = "verdict_missing"
    return [i for i in by_id if i in confirmed], rejected


def plan_scope(files: list, workdir: str) -> tuple[str, ...] | None:
    """Write scope from an item's files, or None when any path is unsafe.

    Item files come from a model that read the repository (possibly prompt-injected), so they never
    reach outside the workdir: each path must resolve inside it, must not be the workdir itself, may
    be a file at any depth or an existing folder at most MAX_SCOPE_DEPTH levels down, and there are at
    most MAX_ITEM_FILES of them.
    """
    if not files or len(files) > MAX_ITEM_FILES:
        return None
    root = os.path.realpath(workdir)
    scopes = []
    for raw in files:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            return None
        path = raw.strip()
        path = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
        if path == root or not path.startswith(root + os.sep):
            return None
        if ".git" in os.path.relpath(path, root).split(os.sep):  # hooks and config would run code
            return None
        if os.path.isdir(path) and len(os.path.relpath(path, root).split(os.sep)) > MAX_SCOPE_DEPTH:
            return None
        scopes.append(path)
    return tuple(dict.fromkeys(scopes))


def item_view(item: dict) -> dict:
    """What a run sees of an item (no trail, no status)."""
    return {"id": item["id"], "title": item["title"], "files": item["files"], "detail": item["detail"]}
