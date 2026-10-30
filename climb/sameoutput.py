#!/usr/bin/env python3
"""Does a change leave everything the agents saw exactly as it was?

Replays every ssisbliss command the agents ran in a scored run (from each run's grade.json), on fresh
copies of its scenario world, once with the code at a git revision and once with the working tree, plus
every --help page; then compares exit codes and output. Only the ssisbliss parts of a command are run
(`cd X && ssisbliss ... | head` runs the ssisbliss part in X); everything else the agent did is skipped,
the same way for both, so the two sides stay comparable.

climb.py uses it for changes that make the tools faster: when nothing an agent would see changed, the
agents cannot have scored differently, so the change is judged on speed alone instead of on a noisy
agent run.

    uv run python climb/sameoutput.py RUN_DIR [--rev HEAD]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))

from run import unwrap_shell  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402
import world  # noqa: E402
from world import ME  # noqa: E402

# Both sides build the same worlds; with one commit time for every commit the scenarios make (unless a
# scenario sets its own), the commit ids, and everything derived from them, are the same on both sides.
_STAMP = f"{int(__import__('time').time()) - 60} +0000"
_git = world.git


def _same_time_git(cwd: Path, *args: str, env: dict | None = None) -> None:
    _git(cwd, *args, env={"GIT_AUTHOR_DATE": _STAMP, "GIT_COMMITTER_DATE": _STAMP, **(env or {})})


for _m in list(sys.modules.values()):
    if getattr(_m, "git", None) is _git:
        _m.git = _same_time_git

# Package files get a random VersionGUID; here each world build draws the same sequence on both sides.
_draws = threading.local()
_uuid4 = uuid.uuid4


def _counted_uuid4() -> uuid.UUID:
    if not hasattr(_draws, "n"):
        return _uuid4()
    _draws.n += 1
    return uuid.uuid5(uuid.NAMESPACE_OID, str(_draws.n))


uuid.uuid4 = _counted_uuid4

PY = REPO / ".venv" / "bin" / "python"
MAIN = "import sys; from ssisbliss.ssis.cli import main; sys.exit(main())"
STOP = {"|", "||", ">", ">>", "2>", "&>", "<"}
TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?([+-]\d\d:?\d\d|Z)?|\b\d{8}-\d{6}\b")


def ssisbliss_calls(command: str) -> list[tuple[str | None, list[str]]]:
    """(directory to cd to first or None, ssisbliss arguments) for each ssisbliss part of a shell command;
    parts using shell variables or substitutions are left out."""
    out, cd = [], None
    for part in re.split(r"&&|;|\n", unwrap_shell(command)):
        if "$" in part or "`" in part:
            continue
        try:
            words = shlex.split(part)
        except ValueError:
            continue
        if not words:
            continue
        if words[0] == "cd" and len(words) == 2:
            cd = words[1]
        elif words[0] == "ssisbliss":
            args = []
            for w in words[1:]:
                if w in STOP or w.startswith(("2>", ">", "|")):
                    break
                args.append(w)
            out.append((cd, args))
    return out


def _env(root: Path, src: Path) -> dict:
    return {"PATH": f"{REPO / '.venv' / 'bin'}:/usr/bin:/bin:/usr/local/bin", "HOME": str(root),
            "SSISBLISS_FAKE_USER": ME, "PYTHONPATH": str(src), "SSISBLISS_BACKUP_DIR": str(root / "backups"),
            "SSISBLISS_FAKE_ROOT": str(root / "servers"), "SSISBLISS_STATE_DIR": str(root / "state")}


def _run(src: Path, root: Path, cwd: Path, args: list[str], old_root: str) -> str:
    args = [a.replace(old_root, str(root)) for a in args]
    p = subprocess.run([str(PY), "-c", MAIN, *args], cwd=cwd if cwd.is_dir() else root / "work",
                       env=_env(root, src), capture_output=True, text=True, timeout=120)
    text = f"exit {p.returncode}\n{p.stdout}\n{p.stderr}".replace(str(root), "<world>")
    return TIME.sub("<time>", text)


def replay(scenario: str, commands: list[str], old_root: str, trees: dict[str, Path], tmp: Path) -> list[str]:
    """Differences (readable lines) between the trees for one agent's commands in one scenario."""
    s = next((x for x in SCENARIOS if x.name == scenario), None)
    if s is None:
        return []
    calls = [c for command in commands for c in ssisbliss_calls(command)]
    seen: dict[str, list[str]] = {}
    for side, src in trees.items():   # one side after the other, in the same folder: paths in the world match
        _draws.n = 0
        w = s.build(tmp / "world")
        del _draws.n
        (w.root / "backups").mkdir(exist_ok=True)
        seen[side] = []
        for cd, args in calls:
            cwd = Path(cd.replace(old_root, str(w.root))) if cd else w.work
            if not cwd.is_absolute():
                cwd = w.work / cwd
            seen[side].append(_run(src, w.root, cwd, args, old_root))
        shutil.rmtree(tmp / "world")
    a, b = seen.values()
    return [f"{scenario}: ssisbliss {' '.join(args)[:120]}" for (_, args), x, y in zip(calls, a, b) if x != y]


def helps(trees: dict[str, Path]) -> list[str]:
    """Every --help page, top level and per command."""
    top = subprocess.run([str(PY), "-c", MAIN, "--help"], env=_env(REPO, trees["old"]), capture_output=True, text=True).stdout
    m = re.search(r"\{([a-z,-]+)\}", top)
    names = m.group(1).split(",") if m else []
    diffs = []
    for args in [["--help"]] + [[n, "--help"] for n in names]:
        a, b = (subprocess.run([str(PY), "-c", MAIN, *args], env=_env(REPO, src), capture_output=True, text=True).stdout
                for src in trees.values())
        if a != b:
            diffs.append(f"ssisbliss {' '.join(args)}")
    return diffs


def compare(run_dir: Path, rev: str = "HEAD", jobs: int = 8) -> dict:
    """{"same": bool, "commands": n, "differences": [...]} for the working tree against rev."""
    runs = []
    for grade in sorted(run_dir.glob("*/*/grade.json")):
        g = json.loads(grade.read_text())
        old_root = str(grade.parent / "world")
        cmds = [c for c in g.get("commands", []) if "ssisbliss" in c]
        if cmds:
            runs.append((g["scenario"], cmds, old_root))
    with tempfile.TemporaryDirectory(prefix="ssisbliss-same-") as tmp:
        tmp = Path(tmp)
        old = tmp / "old-tree"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(old), rev], cwd=REPO, check=True,
                       capture_output=True)
        try:
            trees = {"old": old / "src", "new": REPO / "src"}
            diffs = helps(trees)
            with ThreadPoolExecutor(jobs) as pool:
                found = pool.map(lambda x: replay(x[1][0], x[1][1], x[1][2], trees, tmp / f"r{x[0]}"), enumerate(runs))
                for d in found:
                    diffs += d
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(old)], cwd=REPO, capture_output=True)
    n = sum(len(ssisbliss_calls(c)) for _, cmds, _ in runs for c in cmds)
    return {"same": not diffs, "commands": n, "differences": sorted(set(diffs))}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", help="a scored run: the folder with one subfolder per agent")
    p.add_argument("--rev", default="HEAD")
    a = p.parse_args()
    print(json.dumps(compare(Path(a.run_dir).resolve(), a.rev), indent=1))


if __name__ == "__main__":
    os.environ.pop("CLAUDECODE", None)
    main()
