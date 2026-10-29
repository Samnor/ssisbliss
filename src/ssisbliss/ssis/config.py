"""Named deployment environments (dev / stage / prod ...).

`onprem-ssis.toml`, from --config, else the working directory or a parent, else the user's own
file ($ONPREM_SSIS_CONFIG, default ~/.config/ssisbliss/onprem-ssis.toml), so nothing has to
live in the SSIS repository:

    [environments.dev]
    server = "devsql01\\SQL01"         # ODBC server string
    server_name = "DEVSQL01\\SQL01"    # optional: expected @@SERVERNAME, catches wrong aliases

    [environments.prod]
    server = "prodsql01\\SQL01"
    server_name = "PRODSQL01\\SQL01"
    require_git_committed = true      # packages must be committed and unmodified in git
    require_git_pushed = true         # ... and their last commit must be on a remote branch (pushed)
    confirm = true                    # apply also needs --confirm-env prod
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

FILE = "onprem-ssis.toml"


@dataclass
class Environment:
    name: str
    server: str
    server_name: str | None = None
    require_git_committed: bool = False
    require_git_pushed: bool = False
    confirm: bool = False
    sql_user: str | None = None
    trust_server_certificate: bool = False


def user_config() -> Path:
    return Path(os.environ.get("ONPREM_SSIS_CONFIG", "~/.config/ssisbliss/onprem-ssis.toml")).expanduser()


def find_config(start: Path | None = None) -> Path | None:
    d = (start or Path.cwd()).resolve()
    for candidate in [d, *d.parents]:
        if (candidate / FILE).exists():
            return candidate / FILE
    return user_config() if user_config().exists() else None


def load_environment(name: str, config: Path | None = None) -> Environment:
    path = config or find_config()
    if path is None:
        raise ValueError(f"--env {name} given but no {FILE} found here, in a parent folder or at {user_config()}")
    envs = tomllib.loads(path.read_text()).get("environments", {})
    if name not in envs:
        raise ValueError(f"environment '{name}' not in {path} (have: {', '.join(envs) or 'none'})")
    e = envs[name]
    return Environment(name=name, server=e["server"], server_name=e.get("server_name"),
                       require_git_committed=e.get("require_git_committed", False) or e.get("require_git_pushed", False),
                       require_git_pushed=e.get("require_git_pushed", False),
                       confirm=e.get("confirm", False), sql_user=e.get("sql_user"),
                       trust_server_certificate=e.get("trust_server_certificate", False))


def git_uncommitted(paths: list[Path]) -> list[str]:
    """Files that are untracked, modified or not in a git repository."""
    bad = []
    for p in paths:
        cwd = p.resolve().parent
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", p.name], cwd=cwd,
                                 capture_output=True, text=True)
        if tracked.returncode != 0:
            bad.append(f"{p} (not tracked in git)")
            continue
        status = subprocess.run(["git", "status", "--porcelain", "--", p.name], cwd=cwd,
                                capture_output=True, text=True)
        if status.stdout.strip():
            bad.append(f"{p} (uncommitted changes)")
    return bad


def git_unpushed(paths: list[Path]) -> list[str]:
    """Committed files whose last commit is on no remote-tracking branch (not pushed). Uses the local
    remote-tracking refs, so a push from another clone shows after `git fetch`."""
    bad = []
    for p in paths:
        cwd = p.resolve().parent
        if not subprocess.run(["git", "remote"], cwd=cwd, capture_output=True, text=True).stdout.strip():
            bad.append(f"{p} (the repository has no remote)")
            continue
        commit = subprocess.run(["git", "log", "-1", "--format=%H", "--", p.name], cwd=cwd,
                                capture_output=True, text=True).stdout.strip()
        branches = subprocess.run(["git", "branch", "-r", "--contains", commit], cwd=cwd,
                                  capture_output=True, text=True).stdout.strip() if commit else ""
        if not branches:
            bad.append(f"{p} (its last commit {commit[:10]} is not pushed)")
    return bad

