"""Current read-only Git facts for the selected Modkit source directory."""

from datetime import datetime, timezone
from pathlib import Path
import subprocess


def inspect(root: Path, configured_revision: str | None) -> dict:
    """Read Git sequentially without traversing sources or checking the game."""
    git = {
        "available": True,
        "repository_root": None,
        "repository_matches_source_root": None,
        "head": None,
        "working_tree_changed": None,
        "configured_revision_matches_head": None,
        "read_status": {"repository": "not_inspected", "head": "not_inspected", "working_tree": "not_inspected"},
        "exit_codes": {"repository": None, "head": None, "working_tree": None},
    }
    result = {
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_kind": "sequential_git_reads",
        "configured_revision": configured_revision,
        "revision_source": "caller_label",
        "source_only": True,
        "game_compatibility_checked": False,
        "git": git,
    }

    def read(stage: str, *arguments: str):
        try:
            completed = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(root), *arguments],
                capture_output=True,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            git["available"] = False
            git["read_status"][stage] = "unavailable"
            return None
        except OSError:
            git["read_status"][stage] = "failed"
            return None
        git["exit_codes"][stage] = completed.returncode
        git["read_status"][stage] = "ok" if completed.returncode == 0 else "failed"
        return completed.stdout if completed.returncode == 0 else None

    repository = read("repository", "rev-parse", "--show-toplevel")
    if repository is None:
        if not git["available"]:
            git["read_status"].update(head="unavailable", working_tree="unavailable")
        return result
    try:
        repository_root = Path(repository.decode("utf-8").strip()).resolve()
    except (UnicodeDecodeError, OSError, ValueError):
        git["read_status"]["repository"] = "failed"
        return result
    git["repository_root"] = str(repository_root)
    git["repository_matches_source_root"] = repository_root == root.resolve()
    if not git["repository_matches_source_root"]:
        git["read_status"]["repository"] = "foreign_repository"
        return result

    head = read("head", "rev-parse", "HEAD")
    if head is not None:
        try:
            git["head"] = head.decode("ascii").strip()
        except UnicodeDecodeError:
            git["read_status"]["head"] = "failed"
        else:
            if configured_revision is not None:
                git["configured_revision_matches_head"] = configured_revision.casefold() == git["head"].casefold()
    working_tree = read("working_tree", "status", "--porcelain=v1", "--untracked-files=all", "-z")
    if working_tree is not None:
        git["working_tree_changed"] = bool(working_tree)
    return result
