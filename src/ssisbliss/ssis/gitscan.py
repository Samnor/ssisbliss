"""What the team's git branches do to a project's packages.

Reads only: remote-tracking branches (refreshed by `git fetch` when the caller asks for it), the
default branch and the history between them. For each branch: which packages in the project
folder it changes compared with the default branch, who changed them and when, and a content
fingerprint (package_sha256) of every version of each changed package on the branch, so a
server copy can be traced back to the branch it came from.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .dtsx import package_sha256

MAX_VERSIONS = 30   # versions per package and branch to fingerprint


def _git_bytes(cwd: Path, *args: str) -> bytes | None:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=60)
    return p.stdout if p.returncode == 0 else None


def _git(cwd: Path, *args: str) -> str | None:
    out = _git_bytes(cwd, *args)
    return None if out is None else out.decode("utf-8", "replace").strip()


@dataclass
class PackageChange:
    name: str
    path: str                       # repository-relative path at the branch tip ("" when the branch deletes it)
    author: str
    email: str
    date: str                       # ISO time of the branch's last commit touching it
    commits: int
    deleted: bool = False
    versions: set[str] = field(default_factory=set)   # package_sha256 of every version on the branch
    tip: str | None = None                            # package_sha256 at the branch tip


@dataclass
class Branch:
    name: str                       # e.g. origin/pr/42
    sha: str
    date: str
    yours: bool
    changes: dict[str, PackageChange] = field(default_factory=dict)   # by package name (lower case)


@dataclass
class GitScan:
    root: Path
    prefix: str                     # the project folder, relative to the repository root ("" for the root)
    default: str | None             # e.g. origin/main
    me: str                         # your git user.email
    current: str | None             # your branch
    branches: list[Branch] = field(default_factory=list)
    main_packages: dict[str, str] = field(default_factory=dict)   # name (lower) -> package_sha256 on the default branch
    main_names: dict[str, str] = field(default_factory=dict)      # name (lower) -> name as spelled on the default branch
    fetched: bool = False
    note: str = ""

    def changing(self, name: str, include_yours: bool = False) -> list[tuple[Branch, PackageChange]]:
        key = name.lower()
        return [(b, b.changes[key]) for b in self.branches if key in b.changes and (include_yours or not b.yours)]

    def branch_with_version(self, sha256: str) -> list[tuple[Branch, PackageChange]]:
        """Branches on which some version of a package has exactly this content."""
        return [(b, c) for b in self.branches for c in b.changes.values() if sha256 in c.versions]

    def deleted_on_default(self, name: str) -> dict | None:
        """The commit on the default branch that deleted (or renamed away) this package, if any."""
        if not self.default:
            return None
        path = f"{self.prefix}/{name}" if self.prefix else name
        out = _git(self.root, "log", self.default, "-1", "--diff-filter=DR", "--format=%h%x00%an%x00%ae%x00%cI%x00%s",
                   "--", path)
        if not out:
            return None
        sha, author, email, date, subject = out.split("\x00", 4)
        return {"commit": sha, "author": author, "email": email, "date": date, "subject": subject}


def _default_branch(root: Path) -> str | None:
    head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head
    for ref in ("origin/main", "origin/master", "main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", ref):
            return ref
    return None


def _packages_at(root: Path, ref: str, prefix: str) -> dict[str, tuple[str, str]]:
    """name (lower) -> (name, repository path) of every .dtsx directly in the project folder at ref."""
    out = _git(root, "ls-tree", "--name-only", ref, f"{prefix}/" if prefix else ".") or ""
    found = {}
    for path in out.splitlines():
        name = path.rsplit("/", 1)[-1]
        if name.lower().endswith(".dtsx") and not name.lower().endswith(".server.dtsx"):
            found[name.lower()] = (name, path)
    return found


def _sha_at(root: Path, ref: str, path: str) -> str | None:
    data = _git_bytes(root, "show", f"{ref}:{path}")
    if data is None:
        return None
    try:
        return package_sha256(data)
    except Exception:   # noqa: BLE001 - a broken file still counts as a change, just without a fingerprint
        return None


def scan(directory: Path, fetch: bool = False) -> GitScan | None:
    """None when the folder is not in a git repository."""
    directory = directory.resolve()
    top = _git(directory, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top)
    prefix = _git(directory, "rev-parse", "--show-prefix") or ""
    prefix = prefix.rstrip("/")
    fetched, note = False, ""
    if fetch:
        fetched = _git(root, "fetch", "--quiet", "--prune") is not None
        note = "" if fetched else "git fetch failed; using the remote branches as of the last fetch"
    s = GitScan(root=root, prefix=prefix, default=_default_branch(root), me=(_git(root, "config", "user.email") or "").lower(),
                current=_git(root, "rev-parse", "--abbrev-ref", "HEAD"), fetched=fetched, note=note)
    if not s.default:
        s.note = (s.note + "; " if s.note else "") + "no default branch found (origin/HEAD, main or master)"
        return s
    main = _packages_at(root, s.default, prefix)
    s.main_names = {k: v[0] for k, v in main.items()}
    s.main_packages = {k: sha for k, (_, path) in main.items() if (sha := _sha_at(root, s.default, path))}
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    default_sha = _git(root, "rev-parse", s.default)
    refs = _git(root, "for-each-ref", "refs/remotes", "--format=%(refname:short)%00%(objectname)%00%(committerdate:iso-strict)") or ""
    pathspec = [f":(glob){prefix}/*.dtsx" if prefix else ":(glob)*.dtsx"]
    for line in refs.splitlines():
        name, sha, date = line.split("\x00")
        if name.endswith("/HEAD") or name == s.default or sha == default_sha or "/" not in name:
            continue
        base = _git(root, "merge-base", s.default, sha)
        if not base:
            continue
        changed = _git(root, "diff", "--name-status", "--no-renames", base, sha, "--", *pathspec) or ""
        b = Branch(name=name, sha=sha, date=date, yours=name == upstream)
        for row in changed.splitlines():
            status, path = row.split("\t", 1)
            pkg = path.rsplit("/", 1)[-1]
            log = _git(root, "log", f"{base}..{sha}", "--format=%H%x00%an%x00%ae%x00%cI", "--", path) or ""
            commits = [c.split("\x00") for c in log.splitlines() if c]
            if not commits:
                continue
            last = commits[0]
            ch = PackageChange(name=pkg, path="" if status == "D" else path, author=last[1], email=last[2].lower(),
                               date=last[3], commits=len(commits), deleted=status == "D")
            if not ch.deleted:
                ch.tip = _sha_at(root, sha, path)
                for c in commits[:MAX_VERSIONS]:
                    v = _sha_at(root, c[0], path)
                    if v:
                        ch.versions.add(v)
            b.changes[pkg.lower()] = ch
        if b.changes:
            # A branch is yours when it is your upstream, or every change on it is yours.
            b.yours = b.yours or bool(s.me) and all(c.email == s.me for c in b.changes.values())
            s.branches.append(b)
    s.branches.sort(key=lambda b: b.date, reverse=True)
    return s
