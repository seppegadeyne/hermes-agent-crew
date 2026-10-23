"""Bounded, read-only git snapshot (status, stat, diff) for review agents.

The snapshot goes straight into the subagent's context, so the diff never passes through the lead
agent's context. Git runs without a shell and with every repository-configured program disabled:
no external diff drivers, no textconv, no fsmonitor, no pager, no hooks path, and every
clean/smudge/process filter the repository configures is overridden with an empty command. When
the filter configuration cannot be read, no diff is attached.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REF_RE = re.compile(r"^(?!-)[A-Za-z0-9._/@^~{}:+-]{1,120}$")
FILTER_KEY_RE = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$", re.IGNORECASE)
TIMEOUT_SECONDS = 20
STATUS_MAX_CHARS = 4000
STAT_MAX_CHARS = 4000
_BASE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null",
                "-c", "status.submoduleSummary=false", "--no-pager")
_DROP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR")


def valid_ref(value: object) -> bool:
    return isinstance(value, str) and bool(REF_RE.match(value.strip()))


def _head(text: str, limit: int, note: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[… {len(text) - limit} more characters; {note}]"


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    # GIT_NO_LAZY_FETCH: in a partial clone a missing object must not trigger a fetch (and a credential helper).
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", LC_ALL="C",
               GIT_NO_LAZY_FETCH="1")
    return env


SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def _plain(args: list, cwd: Path) -> tuple:
    """Run one git command that reads only refs and objects (no diff, so no filters); (code, stdout)."""
    git = shutil.which("git")
    if not git:
        return 127, ""
    try:
        proc = subprocess.run([git, *_BASE_CONFIG, *args], cwd=str(cwd), env=_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def head(cwd: Path) -> str | None:
    """Full sha of HEAD in the repository at ``cwd``, or None (no repository, no commit, git missing)."""
    code, out = _plain(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd)
    return out if code == 0 and SHA_RE.match(out) else None


BRANCH_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")


def branch(cwd: Path) -> str | None:
    """Short name of the checked-out branch at ``cwd``, or None (detached HEAD, no repository, git missing)."""
    code, out = _plain(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd)
    return out if code == 0 and BRANCH_RE.match(out) else None


def is_ancestor(cwd: Path, sha: str) -> bool:
    """True when ``sha`` is HEAD or an ancestor of it (False after a rebase, amend or branch switch)."""
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        return False
    code, _ = _plain(["merge-base", "--is-ancestor", sha, "HEAD"], cwd)
    return code == 0


def collect(cwd: Path, base: str | None, max_chars: int) -> str:
    """Return a text snapshot of the working tree changes; never raises."""
    git = shutil.which("git")
    if not git:
        return "(git is not installed; no diff attached)"
    if base is not None and not valid_ref(base):
        return f"(invalid diff_base {base!r}; no diff attached)"
    env = _env()

    def run(args: list, where: Path, extra: tuple = ()) -> tuple:
        proc = subprocess.run([git, *_BASE_CONFIG, *extra, *args], cwd=str(where), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
        output = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
        return proc.returncode, output.decode("utf-8", "replace")

    try:
        code, top = run(["rev-parse", "--show-toplevel"], cwd)
        if code != 0:
            return f"(no git repository at {cwd}; no diff attached)"
        root = Path(top.strip())
        code, names = run(["config", "--includes", "--name-only", "-z", "--get-regexp", r"^filter\."], root)
        if code not in (0, 1):
            return "(could not read the repository's filter configuration; no diff attached)"
        drivers = {m.group(1) for key in names.split("\0") if (m := FILTER_KEY_RE.match(key.strip()))}
        overrides: list = []
        for driver in sorted(drivers):
            for key in ("clean", "smudge", "process"):
                overrides += ["-c", f"filter.{driver}.{key}="]
            overrides += ["-c", f"filter.{driver}.required=false"]
        extra = tuple(overrides)
        ref = (base or "HEAD").strip()
        # Submodules are skipped: their own filter drivers are not part of the overrides above.
        _, status = run(["status", "--short", "--branch", "--untracked-files=normal", "--ignore-submodules=all"],
                        root, extra)
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        code, stat = run([*diff_args, "--stat", ref, "--"], root, extra)
        if code != 0:
            return f"(git diff against {ref} failed: {stat.strip()[:300]})"
        _, diff = run([*diff_args, "--no-color", ref, "--"], root, extra)
    except subprocess.TimeoutExpired:
        return f"(git did not answer within {TIMEOUT_SECONDS} seconds; no diff attached)"
    except OSError as exc:
        return f"(git failed: {exc}; no diff attached)"
    untracked = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
    parts = [f"Repository: {root}", f"Diff base: {ref}", "", "### git status --short",
             _head(status.strip() or "(clean)", STATUS_MAX_CHARS, "status clipped")]
    if untracked:
        parts.append("Untracked files are not in the diff below; read them with read_file if they matter.")
    parts += ["", "### git diff --stat", _head(stat.strip() or "(no changes)", STAT_MAX_CHARS, "stat clipped")]
    if diff.strip():
        parts += ["", "### git diff", diff.rstrip()]
    return _head("\n".join(parts), max_chars, "read the changed files with read_file for the rest")
