#!/usr/bin/env python3
"""Stop private details from reaching the public repository.

Checks what is about to be committed or pushed (file contents, file names, commit messages,
author and committer identities) against:

- built-in patterns that never belong in this repository: e-mail addresses other than
  placeholder and noreply ones, AWS account ids, access keys and private keys, AWS-generated
  host names, home-directory paths;
- your own denylist, `private/denylist.txt` (or $ONPREM_PRIVATE_DIR/denylist.txt): names of
  clients, servers, databases, people, your own name and e-mail. One term per line, matched
  case-insensitively; `re:` starts a regular expression; `#` starts a comment. The denylist is
  gitignored and is never printed in full.

    python scripts/privacy_check.py --install-hooks   # pre-commit and pre-push hooks, this clone only
    python scripts/privacy_check.py --staged          # what the pre-commit hook runs
    python scripts/privacy_check.py --push A..B       # what the pre-push hook runs
    python scripts/privacy_check.py --files           # every tracked file as it is now
    python scripts/privacy_check.py --all             # files and the whole history (CI runs this)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ALLOWED_EMAILS = re.compile(r"(@example\.(com|org|net)|@users\.noreply\.github\.com|^noreply@anthropic\.com)$", re.I)
BUILTIN = [
    ("e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("AWS account id", re.compile(r"arn:aws:[a-z0-9-]*:[a-z0-9-]*:\d{12}|AdministratorAccess-\d{12}|\b\d{12}\.dkr\.ecr\.")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS host name", re.compile(r"\bEC2AMAZ-[A-Z0-9]{7}\b")),
    ("home directory path", re.compile(r"/Users/(?!Shared/)[A-Za-z][\w.-]+/|[Cc]:\\\\?Users\\\\?(?!Public|Default|labadmin|labdev)[A-Za-z][\w.-]+\\\\")),
]
SKIP_SUFFIXES = (".lock",)
CHECKER = "scripts/privacy_check.py"     # its own patterns would match themselves


def private_dir() -> Path:
    return Path(os.environ.get("ONPREM_PRIVATE_DIR") or REPO / "private")


def denylist() -> list[tuple[str, re.Pattern]]:
    path = private_dir() / "denylist.txt"
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        term = line.split("#", 1)[0].strip()
        if term:
            rx = re.compile(term[3:], re.I) if term.startswith("re:") else re.compile(re.escape(term), re.I)
            out.append((f"denylist line {n}", rx))
    return out


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=True).stdout


def git(*args: str) -> str:
    return git_bytes(*args).decode("utf-8", "replace")


def scan_text(where: str, text: str, rules, findings: list[str]) -> None:
    for line_no, line in enumerate(text.splitlines(), 1):
        for name, rx in rules:
            for m in rx.finditer(line):
                if name == "e-mail address" and ALLOWED_EMAILS.search(m.group(0)):
                    continue
                shown = m.group(0) if not name.startswith("denylist") else "<denylisted term>"
                findings.append(f"{where}:{line_no}: {name}: {shown}")


def scan_blob(where: str, data: bytes, rules, findings: list[str]) -> None:
    # Package files can be UTF-16; zipped .ispac parts are checked too.
    if data[:2] == b"PK":
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for n in z.namelist():
                    scan_blob(f"{where}!{n}", z.read(n), rules, findings)
            return
        except zipfile.BadZipFile:
            pass
    text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8", "replace")
    scan_text(where, text, rules, findings)


def identities(rev_range: str | None) -> list[tuple[str, str]]:
    args = ["log", "--format=%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x01"] + ([rev_range] if rev_range else ["--all"])
    out = []
    for rec in git(*args).split("\x01"):
        if rec.strip():
            sha, author, committer, body = rec.strip("\n").split("\x00", 3)
            out.append((sha[:10], f"author: {author}\ncommitter: {committer}\n{body}"))
    return out


def check_commits(rev_range: str | None, rules, findings: list[str]) -> None:
    for sha, text in identities(rev_range):
        scan_text(f"commit {sha}", text, rules, findings)


def check_files(paths_and_data, rules, findings: list[str]) -> None:
    for path, data in paths_and_data:
        if path == CHECKER or path.endswith(SKIP_SUFFIXES):
            continue
        scan_text(f"{path} (file name)", path, rules, findings)
        scan_blob(path, data, rules, findings)


def staged() -> list[tuple[str, bytes]]:
    names = [n for n in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if n]
    return [(n, git_bytes("show", f":{n}")) for n in names]


def tracked() -> list[tuple[str, bytes]]:
    return [(n, (REPO / n).read_bytes()) for n in git("ls-files", "-z").split("\0") if n and (REPO / n).is_file()]


def history_blobs(rev_range: str | None) -> list[tuple[str, bytes]]:
    """Every file version introduced in the range (the whole history without one)."""
    seen, out = set(), []
    revs = git("rev-list", rev_range or "--all").split()
    for rev in revs:
        for n in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", "--diff-filter=ACMR", rev).split("\0"):
            if n and (rev, n) not in seen:
                seen.add((rev, n))
                try:
                    out.append((f"{n}@{rev[:10]}", git_bytes("show", f"{rev}:{n}")))
                except subprocess.CalledProcessError:
                    pass
    return out


HOOK = """#!/bin/sh
# Installed by scripts/privacy_check.py --install-hooks
{body}
"""


def install_hooks() -> None:
    hooks = Path(git("rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else REPO / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    py = "python3"
    bodies = {
        "pre-commit": f'exec {py} "$(git rev-parse --show-toplevel)/{CHECKER}" --staged',
        "pre-push": (f'zero=0000000000000000000000000000000000000000\n'
                     f'while read local_ref local_sha remote_ref remote_sha; do\n'
                     f'  [ "$local_sha" = "$zero" ] && continue\n'
                     f'  if [ "$remote_sha" = "$zero" ]; then range="$local_sha"; else range="$remote_sha..$local_sha"; fi\n'
                     f'  {py} "$(git rev-parse --show-toplevel)/{CHECKER}" --push "$range" || exit 1\n'
                     f'done'),
    }
    for name, body in bodies.items():
        path = hooks / name
        path.write_text(HOOK.format(body=body))
        path.chmod(0o755)
        print(f"installed {path}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true", help="files staged for the next commit, and your git identity")
    g.add_argument("--push", metavar="RANGE", help="commits about to be pushed (A..B, or one commit for a new branch)")
    g.add_argument("--files", action="store_true", help="every tracked file as it is now")
    g.add_argument("--all", action="store_true", help="every tracked file and the whole history")
    g.add_argument("--install-hooks", action="store_true")
    a = p.parse_args()
    if a.install_hooks:
        install_hooks()
        return 0
    rules = BUILTIN + denylist()
    findings: list[str] = []
    if a.staged:
        check_files(staged(), rules, findings)
        who = f"{git('config', 'user.name').strip()} <{git('config', 'user.email').strip()}>"
        scan_text("your git identity (git config user.name / user.email)", who, rules, findings)
    elif a.files:
        check_files(tracked(), rules, findings)
    elif a.push:
        check_commits(a.push, rules, findings)
        check_files(history_blobs(a.push), rules, findings)
    else:
        check_files(tracked(), rules, findings)
        check_commits(None, rules, findings)
        check_files(history_blobs(None), rules, findings)
    denied = len(rules) - len(BUILTIN)
    if findings:
        print(f"privacy check failed ({len(findings)} finding(s); {denied} denylist term(s) in use):", file=sys.stderr)
        for f in findings[:50]:
            print(f"  {f}", file=sys.stderr)
        if len(findings) > 50:
            print(f"  ... and {len(findings) - 50} more", file=sys.stderr)
        print("Remove them (or rewrite the commits) before committing or pushing.", file=sys.stderr)
        return 1
    print(f"privacy check passed ({denied} denylist term(s) in use)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
