"""Base records: which deployed version of each package you started from.

Written by `pull` and after every successful deploy; deploy compares the server's current
package with the base to tell "I am replacing what I started from" from "I am about to
overwrite someone else's work".

Kept per user, outside the repository: `$SSISBLISS_STATE_DIR` (default ~/.ssisbliss/state),
one file per repository. A repository is identified by its `origin` URL, or by git's common
directory when it has no remote, so every worktree, branch and clone of the same repository
shares one record of your own deploys. Folders outside git are keyed by their path. A
`.ssis-base.json` next to the packages (written by earlier versions) is still read.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

FILE = ".ssis-base.json"   # legacy location, read only


def target_key(server: str, folder: str, project: str) -> str:
    return f"{server}/{folder}/{project}"


def state_dir(state: Path | None = None) -> Path:
    return state or Path(os.environ.get("SSISBLISS_STATE_DIR", "~/.ssisbliss/state")).expanduser()


def _git(directory: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def identities(directory: Path) -> list[str]:
    """Ways to identify the repository a folder belongs to, strongest first: its origin URL (shared by
    every clone), git's common directory (shared by every worktree), the folder itself."""
    directory = directory.resolve()
    origin = _git(directory, "config", "--get", "remote.origin.url")
    common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return [i for i in (origin and f"remote:{origin}", common and f"git:{common}", f"dir:{directory}") if i]


def repository_id(directory: Path) -> str:
    return identities(directory)[0]


def _path_for(identity: str, state: Path | None) -> Path:
    return state_dir(state) / "bases" / f"{hashlib.sha256(identity.encode()).hexdigest()[:20]}.json"


def _file(directory: Path, state: Path | None) -> Path:
    return _path_for(repository_id(directory), state)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        return {}


def _packages(directory: Path, key: str, state: Path | None) -> dict:
    """Records under any identity the folder has had (a remote added later, a folder made a repository),
    stronger identities winning; the legacy per-folder file first."""
    found = dict(_read(directory / FILE).get(key, {}).get("packages", {}))
    for identity in reversed(identities(directory)):
        found.update(_read(_path_for(identity, state)).get("targets", {}).get(key, {}).get("packages", {}))
    return found


def entries(paths: list[Path], key: str, state: Path | None = None) -> dict[str, dict]:
    """Base record ({"sha256", "lsn"}) per package name for the given package files.

    Package names are case-insensitive in the catalog, so `stage_customer.dtsx` finds
    the record for `Stage_Customer.dtsx` when that is the only match.
    """
    out: dict[str, dict] = {}
    for p in paths:
        packages = _packages(p.parent, key, state)
        entry = packages.get(p.name)
        if entry is None:
            same = [v for n, v in packages.items() if n.lower() == p.name.lower()]
            entry = same[0] if len(same) == 1 else None
        if entry:
            out[p.name] = entry
    return out


def lookup(paths: list[Path], key: str, state: Path | None = None) -> dict[str, str]:
    """Base sha256 per package name for the given package files (see entries)."""
    return {n: e["sha256"] for n, e in entries(paths, key, state).items()}


def lsns(paths: list[Path], key: str, state: Path | None = None) -> dict[str, int]:
    """Project version each package's base record was taken at."""
    return {n: e["lsn"] for n, e in entries(paths, key, state).items() if e.get("lsn") is not None}


def record(directory: Path, key: str, lsn: int, packages: dict[str, str], state: Path | None = None) -> Path:
    path = _file(directory, state)
    data = _read(path)
    data["repository"] = repository_id(directory)
    target = data.setdefault("targets", {}).setdefault(key, {"packages": {}})
    target["lsn"] = lsn
    for name, digest in packages.items():
        target["packages"][name] = {"sha256": digest, "lsn": lsn}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def forget(directory: Path, key: str, names: list[str], state: Path | None = None) -> None:
    """Drop base records for packages that are no longer deployed (remove-packages)."""
    path = _file(directory, state)
    data = _read(path)
    packages = data.get("targets", {}).get(key, {}).get("packages", {})
    for n in names:
        packages.pop(n, None)
    if data:
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

