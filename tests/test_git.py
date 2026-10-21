"""The read-only git snapshot for review agents."""
import subprocess

import pytest

from hermes_agent_crew import agents_git


@pytest.fixture(autouse=True)
def no_outer_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("one\nchanged\n")
    (repo / "new.txt").write_text("untracked\n")
    return repo


def test_snapshot_has_status_stat_and_diff(tmp_path):
    text = agents_git.collect(make_repo(tmp_path), None, 20_000)
    assert "## main" in text
    assert "a.txt" in text and "+changed" in text
    assert "Untracked files" in text and "new.txt" in text


def test_repository_configured_programs_never_run(tmp_path):
    repo = make_repo(tmp_path)
    marker = tmp_path / "pwned"
    git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    git(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    git(repo, "config", "diff.evil.command", f"touch {marker}")
    git(repo, "config", "diff.evil.textconv", f"touch {marker}; cat")
    git(repo, "config", "core.fsmonitor", f"touch {marker}")
    git(repo, "config", "core.pager", f"touch {marker}; cat")
    (repo / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    text = agents_git.collect(repo, None, 20_000)
    assert not marker.exists()
    assert "+changed" in text


def test_budget_is_respected(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("one\n" + "x" * 50_000 + "\n")
    text = agents_git.collect(repo, None, 2_000)
    assert len(text) < 2_200 and "read the changed files" in text


def test_invalid_ref_and_missing_repository(tmp_path):
    assert "invalid diff_base" in agents_git.collect(tmp_path, "--output=/tmp/x", 5_000)
    assert "no git repository" in agents_git.collect(tmp_path, None, 5_000)
    assert "failed" in agents_git.collect(make_repo(tmp_path), "no-such-ref", 5_000)


def test_head_and_ancestry(tmp_path):
    repo = make_repo(tmp_path)
    first = agents_git.head(repo)
    assert first and len(first) == 40
    assert agents_git.is_ancestor(repo, first)
    git(repo, "commit", "-qam", "second")
    assert agents_git.head(repo) != first and agents_git.is_ancestor(repo, first)
    git(repo, "checkout", "-q", "--orphan", "other")
    git(repo, "commit", "-qm", "unrelated")
    assert not agents_git.is_ancestor(repo, first)
    assert not agents_git.is_ancestor(repo, "main; rm -rf /") and not agents_git.is_ancestor(repo, "HEAD")
    assert agents_git.head(tmp_path / "missing") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert agents_git.head(plain) is None
