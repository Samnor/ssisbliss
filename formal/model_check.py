#!/usr/bin/env python3
"""Bounded model checking of ssisbliss's core promises, on the real code.

Every sequence of actions up to --depth is run against a fake SSIS catalog shared by two developers using
ssisbliss (me, ann) and a colleague who deploys from Visual Studio (bob): edit a package, pull, deploy (plan,
then apply with its plan id), plan now and apply later, roll back to any stored version. Each action runs the
real command (cli.run, the code the CLI and the MCP server run), and after each one the properties below are
checked against a ghost model kept apart from the tool: who wrote each server version, and which version each
developer's work started from. A violation prints the shortest action sequence that breaks it.

Safety
  no-lost-work    a command by X replaces a package's server copy only if X wrote that copy or X's work
                  started from it at some point: nobody's deploy is overwritten or rolled back unseen
  scope           deploy-packages changes no package but the one deployed
  plan-binding    an apply with a plan id changes nothing if the server moved since the plan
  refusal-atomic  a command that does not succeed leaves the server as it was
  contract        ok is true exactly when the exit code is 0; error_kind when it is not; an applicable plan's
                  next carries its plan id
Progress (the features work, not only refuse)
  deploy          X deploys a changed package whose server copy is the one X started from
  apply-later     a plan applied later goes through when neither the server nor the file changed
  rollback-own    X rolls back to a version when every version after it is X's own
  pull            pull always works

    uv run python formal/model_check.py [--depth 3]
"""
from __future__ import annotations

import argparse
import copy
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ssisbliss.ssis import cli
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.fake import FakeCatalog, dtsx, ispac
from ssisbliss.ssis.ispac import Ispac

F = P = "STAGE"
PACKAGES = ("Load_A.dtsx", "Load_B.dtsx")
TOOL_USERS = ("me", "ann")
LOGIN = {"me": "LAB\\me", "ann": "LAB\\ann", "bob": "LAB\\bob", "init": "LAB\\init"}
MAX_EDITS = 2   # per developer and package: keeps the state space finite


def content(who: str, name: str, n: int) -> bytes:
    """A version of a package: an element, not a comment (the package hash, rightly, ignores comments)."""
    return dtsx(name, body=f'<DTS:Variables><DTS:Variable DTS:ObjectName="{who}_{n}" /></DTS:Variables>')


class World:
    """The files at one fixed path (base records are keyed by the folder's path), saved and restored by copy."""

    def __init__(self, root: Path):
        self.root = root
        self.server = root / "server"

    def work(self, who: str) -> Path:
        return self.root / who / "work"

    def env(self, who: str) -> dict:
        return {"SSISBLISS_FAKE_USER": LOGIN[who], "SSISBLISS_STATE_DIR": str(self.root / who / "state"),
                "SSISBLISS_BACKUP_DIR": str(self.root / "backups"), "SSISBLISS_CONFIG": str(self.root / "none.toml")}

    def run(self, who: str, *argv: str):
        saved = {k: os.environ.get(k) for k in self.env(who)}
        os.environ.update(self.env(who))
        try:
            return cli.run([argv[0], "--server", f"fake://{self.server}", "--folder", F, "--project", P, *argv[1:]])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def catalog(self) -> FakeCatalog:
        return FakeCatalog.load(self.server)

    def server_hashes(self) -> dict[str, str]:
        return Ispac(self.catalog().get_project(F, P)).package_hashes()

    def history(self) -> tuple[int, dict[int, str]]:
        """The current lsn and who created each stored version."""
        cat = self.catalog()
        p = cat._p(F, P)
        return p.lsn, {lsn: who for lsn, who in p.created_by.items()}

    def local(self, who: str, name: str) -> str:
        return package_sha256((self.work(who) / name).read_bytes())

    def save(self, to: Path) -> None:
        shutil.copytree(self.root, to)

    def restore(self, src: Path) -> None:
        shutil.rmtree(self.root)
        shutil.copytree(src, self.root)


class Ghost:
    """What the tool must respect, kept by the checker alone: authors of contents, each developer's base."""

    def __init__(self):
        self.author: dict[str, str] = {}                  # content hash -> who wrote it
        self.base: dict[str, dict[str, str]] = {}         # who -> package -> hash their work starts from now
        self.built_on: dict[str, dict[str, set]] = {}     # who -> package -> every hash their work started from
        self.edits: dict[tuple[str, str], int] = {}
        self.pending: dict[str, dict] = {}                # who -> a plan made earlier, not yet applied
        self.trace: list[str] = []


def start(world: World) -> Ghost:
    g = Ghost()
    world.root.mkdir(parents=True)
    cat = FakeCatalog(user=LOGIN["init"])
    cat.save(world.server)
    cat.add_project(F, P, ispac(P, {n: content("init", n, 0) for n in PACKAGES}))
    cat.save(world.server)
    for n in PACKAGES:
        g.author[package_sha256(content("init", n, 0))] = "init"
    for who in TOOL_USERS:
        world.work(who).mkdir(parents=True)
        r = world.run(who, "pull", "--out", str(world.work(who)))
        assert r.code == 0, r.doc
        g.base[who] = dict(world.server_hashes())
        g.built_on[who] = {n: {h} for n, h in g.base[who].items()}
    return g


def actions(world: World, g: Ghost) -> list[tuple[str, tuple]]:
    out: list[tuple[str, tuple]] = []
    for who in TOOL_USERS:
        for n in PACKAGES:
            if g.edits.get((who, n), 0) < MAX_EDITS:
                out.append(("edit", (who, n)))
            out.append(("deploy", (who, n)))
            if who not in g.pending:
                out.append(("plan", (who, n)))
        out.append(("pull", (who,)))
        if who in g.pending:
            out.append(("apply_later", (who,)))
        lsn, created = world.history()
        out += [("rollback", (who, to)) for to in sorted(created) if to != lsn]
    for n in PACKAGES:
        if g.edits.get(("bob", n), 0) < MAX_EDITS:
            out.append(("bob_deploys", (n,)))
    return out


class Violation(Exception):
    pass


def contract(r) -> None:
    if r.doc is None:
        return
    if r.doc.get("ok") is not (r.code == 0):
        raise Violation(f"contract: ok={r.doc.get('ok')} with exit code {r.code}")
    if r.code and "error_kind" not in r.doc:
        raise Violation(f"contract: exit code {r.code} without error_kind")
    if r.doc.get("can_apply") and r.doc.get("plan_id") and r.doc["plan_id"] not in (r.doc.get("next") or [""])[0]:
        raise Violation("contract: an applicable plan whose next does not carry its plan id")


def check_change(g: Ghost, who: str, before: dict[str, str], after: dict[str, str], what: str) -> None:
    for n in PACKAGES:
        old, new = before.get(n), after.get(n)
        if old != new and g.author.get(old) != who and old not in g.built_on[who].get(n, set()):
            raise Violation(f"no-lost-work: {what} by {who} replaced {n} written by {g.author.get(old)}, "
                            f"which {who}'s work did not start from")


def step(world: World, g: Ghost, kind: str, args: tuple) -> None:
    g.trace.append(f"{kind}{args}")
    before = world.server_hashes()
    if kind == "edit":
        who, n = args
        k = g.edits[(who, n)] = g.edits.get((who, n), 0) + 1
        data = content(who, n, k)
        (world.work(who) / n).write_bytes(data)
        g.author[package_sha256(data)] = who
        return
    if kind == "bob_deploys":
        (n,) = args
        k = g.edits[("bob", n)] = g.edits.get(("bob", n), 0) + 1
        data = content("bob", n, k)
        cat = world.catalog()
        cat.someone_else_deploys(F, P, n, data, who=LOGIN["bob"])
        g.author[package_sha256(data)] = "bob"
        return
    if kind == "pull":
        (who,) = args
        r = world.run(who, "pull", "--out", str(world.work(who)))
        contract(r)
        if r.code != 0:
            raise Violation(f"pull: {who}'s pull failed: {r.doc}")
        for n, h in world.server_hashes().items():
            if world.local(who, n) == h:   # pull records the server copy as your base where your file is that copy
                g.base[who][n] = h
                g.built_on[who][n].add(h)
        if world.server_hashes() != before:
            raise Violation("refusal-atomic: pull changed the server")
        return
    if kind in ("deploy", "plan"):
        who, n = args
        path = str(world.work(who) / n)
        plan = world.run(who, "deploy-packages", path)
        contract(plan)
        if world.server_hashes() != before:
            raise Violation("refusal-atomic: a plan changed the server")
        local = world.local(who, n)
        expect = before.get(n) == g.base[who].get(n) and local != before.get(n)
        if kind == "plan":
            if plan.doc.get("plan_id"):
                g.pending[who] = {"name": n, "plan_id": plan.doc["plan_id"], "server": world.history()[0],
                                  "hashes": before, "local": local}
            return
        if not plan.doc.get("plan_id"):
            if expect:
                raise Violation(f"deploy: {who}'s deploy of {n} was refused though the server copy is the one their "
                                f"work started from: {plan.doc.get('refused_because') or plan.doc.get('error')}")
            return
        r = world.run(who, "deploy-packages", path, "--apply", "--plan-id", plan.doc["plan_id"])
        finish_apply(world, g, who, n, r, before, local, "deploy")
        if expect and r.code != 0:
            raise Violation(f"deploy: applying {who}'s plan for {n} failed: {r.doc}")
        return
    if kind == "apply_later":
        (who,) = args
        pend = g.pending.pop(who)
        n = pend["name"]
        lsn = world.history()[0]
        moved = lsn != pend["server"] or before != pend["hashes"]   # before the apply runs
        local = world.local(who, n)
        r = world.run(who, "deploy-packages", str(world.work(who) / n), "--apply", "--plan-id", pend["plan_id"])
        if moved and (world.history()[0] != lsn or world.server_hashes() != before):
            raise Violation(f"plan-binding: {who}'s plan for {n} changed the server after the server moved")
        finish_apply(world, g, who, n, r, before, local, "apply")
        if not moved and local == pend["local"] and r.code != 0:
            raise Violation(f"apply-later: {who}'s plan for {n} failed though nothing changed: {r.doc}")
        return
    if kind == "rollback":
        who, to = args
        _, created = world.history()
        plan = world.run(who, "rollback", "--to-lsn", str(to))
        contract(plan)
        own = all(created[lsn] == LOGIN[who] for lsn in created if lsn > to)
        if not plan.doc.get("plan_id"):
            if own:
                raise Violation(f"rollback-own: {who}'s rollback to lsn {to} was refused though every later version "
                                f"is theirs: {plan.doc.get('refused_because') or plan.doc.get('error')}")
            return
        r = world.run(who, "rollback", "--to-lsn", str(to), "--apply", "--plan-id", plan.doc["plan_id"])
        contract(r)
        after = world.server_hashes()
        if r.code != 0 and after != before:
            raise Violation(f"refusal-atomic: a failed rollback changed the server: {r.doc}")
        check_change(g, who, before, after, f"rollback to lsn {to}")
        if r.code == 0:   # what you restored yourself, you have seen
            for n, h in after.items():
                g.built_on[who][n].add(h)
        if own and r.code != 0:
            raise Violation(f"rollback-own: {who}'s rollback to lsn {to} failed: {r.doc}")
        return
    raise ValueError(kind)


def finish_apply(world: World, g: Ghost, who: str, n: str, r, before: dict, local: str, what: str) -> None:
    contract(r)
    after = world.server_hashes()
    if r.code != 0:
        if after != before:
            raise Violation(f"refusal-atomic: a failed {what} changed the server: {r.doc}")
        return
    if any(after.get(m) != before.get(m) for m in PACKAGES if m != n):
        raise Violation(f"scope: {who}'s {what} of {n} changed another package")
    if after.get(n) != local:
        raise Violation(f"{what}: {who}'s {n} on the server is not their file after a successful apply")
    check_change(g, who, before, after, what)
    g.base[who][n] = local   # an applied deploy records the deployed copy as your base
    g.built_on[who][n].add(local)


def key(world: World, g: Ghost) -> tuple:
    lsn, created = world.history()
    locals_ = tuple(world.local(w, n) for w in TOOL_USERS for n in PACKAGES)
    return (tuple(sorted(world.server_hashes().items())), lsn, tuple(sorted(created.items())), locals_,
            tuple(sorted((w, tuple(sorted(b.items()))) for w, b in g.base.items())),
            tuple(sorted((w, p["name"], p["server"], p["local"]) for w, p in g.pending.items())),
            tuple(sorted(g.edits.items())))


class _Stop(Exception):
    pass


def check(depth: int, workdir: Path | None = None, log=print, first: bool = False) -> dict:
    """Run every action sequence up to `depth`; with `first`, stop at the first violation."""
    tmp = Path(tempfile.mkdtemp(prefix="ssisbliss-mc-", dir=workdir))
    world = World(tmp / "world")
    snaps = tmp / "snaps"
    snaps.mkdir()
    g0 = start(world)
    seen: dict[tuple, int] = {}
    stats = {"depth": depth, "steps": 0, "states": 0, "violations": []}
    counter = iter(range(10**9))

    def explore(g: Ghost, left: int) -> None:
        snap = snaps / str(next(counter))
        world.save(snap)
        for kind, args in actions(world, g):
            world.restore(snap)
            child = copy.deepcopy(g)
            stats["steps"] += 1
            try:
                step(world, child, kind, args)
            except Violation as v:
                stats["violations"].append({"property": str(v).split(":")[0], "detail": str(v), "trace": child.trace})
                log(f"VIOLATION {v}\n  after: {' -> '.join(child.trace)}")
                if first:
                    raise _Stop
                continue
            if left > 1:
                k = key(world, child)
                if seen.get(k, 0) >= left - 1:
                    continue
                seen[k] = left - 1
                stats["states"] += 1
                explore(child, left - 1)
        shutil.rmtree(snap)

    started = time.time()
    try:
        explore(g0, depth)
    except _Stop:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stats["seconds"] = round(time.time() - started, 1)
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--depth", type=int, default=3, help="longest action sequence checked (default 3)")
    a = p.parse_args()
    stats = check(a.depth)
    print(f"depth {stats['depth']}: {stats['steps']} actions run, {stats['states']} distinct states, "
          f"{len(stats['violations'])} violation(s), {stats['seconds']}s")
    return 1 if stats["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
