"""Who else is working on a package, or on a package that depends on it; and which packages are strays.

Combines, all read-only:
- git: the team's remote branches that change the package (gitscan.py), with who and when;
- the server: whose version is deployed (matched by content against the default branch, your base
  record and every version on every branch), and who deployed the project lately;
- runs: catalog executions of the package started by people, not by SQL Agent, in the last days;
- SQL Agent: jobs whose steps run the package;
- dependencies (deps.py): Execute Package calls and tables one package writes and another reads,
  including dependencies that only exist on someone's branch.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import deps
from .gitscan import Branch, GitScan, PackageChange
from .ispac import Ispac

SERVICE = re.compile(r"^(nt service|nt authority)\\", re.I)


def ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - t).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            return f"{int(s // size)} {unit} ago"
    return "just now"


def _who(name: str, email: str) -> str:
    return f"{name} <{email}>" if email else name


def branch_entry(b: Branch, c: PackageChange) -> dict:
    out = {"branch": b.name, "author": _who(c.author, c.email), "last_change": c.date, "ago": ago(c.date),
           "commits": c.commits}
    if c.deleted:
        out["deletes_it"] = True
    if b.yours:
        out["yours"] = True
    return out


class Activity:
    """Everything needed to answer "is anyone else on this?" for the packages of one project."""

    def __init__(self, scan: GitScan | None, server: Ispac | None = None, local: dict[str, bytes] | None = None,
                 bases: dict[str, dict] | None = None, catalog=None, folder: str = "", project: str = "",
                 agent=None, days: float = 14, versions: list[dict] | None = None):
        self.scan, self.server, self.catalog, self.agent = scan, server, catalog, agent
        self.folder, self.project, self.days = folder, project, days
        self.local = local or {}
        self.bases = {k.lower(): v for k, v in (bases or {}).items()}
        self.server_hashes = server.package_hashes() if server else {}
        self.versions = versions or []
        self._facts: dict[str, deps.Facts] | None = None
        self._jobs: dict[str, list[str]] | None = None
        self._run_as: set[str] | None = None

    # ------------------------------------------------------------ building blocks --

    def facts(self) -> dict[str, deps.Facts]:
        """Facts per package: your working copy where you have the file, else the server's copy."""
        if self._facts is None:
            found: dict[str, deps.Facts] = {}
            if self.server:
                mine = {n.lower() for n in self.local}
                for n in self.server.package_names:     # your copy replaces it below, so it is not read for nothing
                    found[n] = deps.Facts(n) if n.lower() in mine else deps.facts(n, self.server.package(n))
            for n, data in self.local.items():
                match = next((k for k in found if k.lower() == n.lower()), n)
                found.pop(match, None)
                found[match] = deps.facts(match, data)
            self._facts = found
        return self._facts

    def jobs(self) -> dict[str, list[str]]:
        """package name (lower) -> ["JOB (step N)"] for SQL Agent steps that run it from this folder/project."""
        if self._jobs is None:
            self._jobs = {}
            if self.agent is not None:
                from ..agent.status import parse_ssis_command
                try:
                    steps = self.agent.steps()
                except Exception:   # noqa: BLE001 - no msdb access: no job information
                    steps = []
                for s in steps:
                    if (s.get("subsystem") or "").upper() != "SSIS":
                        continue
                    p = parse_ssis_command(s.get("command") or "")
                    if (p.get("folder", "").lower(), p.get("project", "").lower()) == (self.folder.lower(), self.project.lower()):
                        self._jobs.setdefault(p["package"].lower(), []).append(f"{s['job']} (step {s['step_id']})")
        return self._jobs

    def run_as(self) -> set[str]:
        if self._run_as is None:
            try:
                self._run_as = self.agent.run_as_accounts() if self.agent is not None else set()
            except Exception:   # noqa: BLE001
                self._run_as = set()
        return self._run_as

    def runs_by_people(self, name: str) -> list[dict]:
        if self.catalog is None:
            return []
        try:
            rows = self.catalog.executions(self.folder, self.project, name, since_hours=self.days * 24, limit=50)
        except Exception:   # noqa: BLE001 - no read permission on executions
            return []
        status = {1: "created", 2: "running", 3: "canceled", 4: "failed", 5: "pending", 6: "ended unexpectedly",
                  7: "succeeded", 8: "stopping", 9: "completed"}
        out = []
        for r in rows:
            who = r.get("caller_name") or ""
            if who.lower() in self.run_as() or SERVICE.match(who):
                continue
            out.append({"by": who, "at": str(r.get("start_time") or "")[:19], "ago": ago(r.get("start_time")),
                        "status": status.get(r.get("status"), str(r.get("status")))})
        return out[:5]

    def server_state(self, name: str) -> dict | None:
        if self.server is None:
            return None
        deployed = next((n for n in self.server_hashes if n.lower() == name.lower()), None)
        if deployed is None:
            return {"state": "not deployed"}
        sha = self.server_hashes[deployed]
        main = self.scan.main_packages.get(name.lower()) if self.scan else None
        base = self.bases.get(name.lower())
        if main and sha == main:
            return {"state": "same as the default branch"}
        if base and sha == base.get("sha256"):
            return {"state": "your version (as you last deployed or pulled it)"}
        found = self.scan.branch_with_version(sha) if self.scan else []
        found = [(b, c) for b, c in found if c.name.lower() == name.lower()]
        if found:
            b, c = found[0]
            return {"state": f"from branch {b.name}" + (" (yours)" if b.yours else f" ({c.author})"),
                    "branch": b.name, "author": _who(c.author, c.email), "yours": b.yours}
        recent = [f"lsn {v['object_version_lsn']} by {v.get('created_by')} {ago(v.get('created_time'))}".strip()
                  for v in self.versions[:3]]
        if (self.scan and self.scan.default and main is None
                and not any(not c.deleted for _, c in self.scan.changing(name, include_yours=True))
                and name.lower() not in {n.lower() for n in self.local}):
            # in no branch at all, not just a different version: a stray candidate, which `strays` decides
            out = {"state": "not in the default branch and on no branch: a leftover, or someone's work that was never "
                            "pushed. `ssisbliss strays` tells which (it also checks callers, jobs and recent runs); "
                            "remove-packages --strays removes only a package it calls a stray",
                   "recent_project_deploys": recent}
            gone = self.scan.deleted_on_default(name)
            if gone:
                out["removed_from_default_branch"] = gone
            return out
        return {"state": "differs from the default branch and from every branch: someone's unpushed or local work",
                "recent_project_deploys": recent}

    # -------------------------------------------------------------------- answers --

    def others_on(self, name: str) -> list[dict]:
        return [branch_entry(b, c) for b, c in (self.scan.changing(name) if self.scan else [])]

    def report(self, name: str) -> dict:
        out: dict = {"package": name}
        out["branches"] = [branch_entry(b, c) for b, c in (self.scan.changing(name, include_yours=True) if self.scan else [])]
        state = self.server_state(name)
        if state is not None:
            out["server"] = state
        out["recent_runs_by_people"] = self.runs_by_people(name)
        out["jobs"] = self.jobs().get(name.lower(), [])
        facts = self.facts()
        depends, used = deps.related(name, facts)
        out["depends_on"] = [self._edge(e) for e in depends]
        out["used_by"] = [self._edge(e) for e in used]
        known = {e.package.lower() for e in used}
        out["used_by"] += [d for d in self._branch_dependents(name, facts) if d["package"].lower() not in known]
        unknown = facts.get(next((k for k in facts if k.lower() == name.lower()), name))
        if unknown and unknown.unknown_sql:
            out["not_followed"] = unknown.unknown_sql
        out["summary"] = self.summary(out)
        return out

    def _edge(self, e: deps.Edge) -> dict:
        d = {"package": e.package, "via": e.via}
        work = self.others_on(e.package)
        if work:
            d["others_working"] = work
        return d

    def _branch_dependents(self, name: str, facts: dict[str, deps.Facts]) -> list[dict]:
        """Dependencies that exist on someone else's branch (a branch version of another package that calls
        this one, or reads a table this one writes); the caller drops the ones the released version has too."""
        if not self.scan:
            return []
        me = facts.get(next((k for k in facts if k.lower() == name.lower()), name), deps.Facts(name))
        out = []
        for b in self.scan.branches:
            if b.yours:
                continue
            for c in b.changes.values():
                if c.deleted or c.name.lower() == name.lower() or not c.path:
                    continue
                data = self.scan.show(b.sha, c.path)
                if not data:
                    continue
                try:
                    f = deps.facts(c.name, data)
                except Exception:   # noqa: BLE001
                    continue
                via = []
                if name.lower() in {x.lower() for x in f.calls}:
                    via.append(f"{c.name} runs {name} (Execute Package task)")
                shared = sorted(f.reads & me.writes)
                if shared:
                    via.append(f"{c.name} reads {', '.join(shared)}, which {name} writes")
                if via:
                    out.append({"package": c.name, "via": "; ".join(via) + f" (on {b.name})",
                                "others_working": [branch_entry(b, c)]})
        return out

    @staticmethod
    def summary(r: dict) -> str:
        parts = []
        others = [b for b in r["branches"] if not b.get("yours")]
        for b in others:
            n = b["commits"]
            parts.append(f"{b['author']} changes {r['package']} on {b['branch']} ({n} commit{'s' if n != 1 else ''}, {b['ago']})")
        s = r.get("server", {})
        if s.get("branch") and not s.get("yours"):
            parts.append(f"the server has the version from {s['branch']}")
        elif s.get("state", "").startswith("differs"):
            parts.append("the server has a version that is on no branch")
        elif s.get("state", "").startswith("not in the default branch"):
            parts.append(f"only the server has {r['package']} (no branch does): `ssisbliss strays` says whether it is a "
                         "stray")
        for run in r["recent_runs_by_people"][:2]:
            parts.append(f"{run['by']} ran it {run['ago']} ({run['status']})")
        for d in r["depends_on"] + r["used_by"]:
            for w in d.get("others_working", []):
                if not w.get("yours"):
                    parts.append(f"{w['author']} changes {d['package']} on {w['branch']}, and {d['via']}")
        if not parts:
            return f"nobody else appears to be working on {r['package']} or on the packages linked to it"
        return "; ".join(parts)


# ------------------------------------------------------------------------ strays --

def strays(act: Activity, local_names: set[str]) -> list[dict]:
    """Deployed packages that are not in the default branch and not in your working copy, with the
    evidence for and against removing each."""
    scan = act.scan
    out = []
    if act.server is None or scan is None or not scan.default:
        return out
    facts = act.facts()
    for name in sorted(act.server_hashes, key=str.lower):
        if name.lower() in scan.main_packages or name.lower() in {n.lower() for n in local_names}:
            continue
        r: dict = {"package": name}
        reasons_keep = []
        on_branches = [branch_entry(b, c) for b, c in scan.changing(name, include_yours=True) if not c.deleted]
        if on_branches:
            r["on_branches"] = on_branches
            reasons_keep.append("it is on " + ", ".join(f"{b['branch']} ({b['author']})" for b in on_branches))
        gone = scan.deleted_on_default(name)
        if gone:
            r["removed_from_default_branch"] = gone
        callers = [k for k, f in facts.items() if k.lower() != name.lower() and name.lower() in {c.lower() for c in f.calls}]
        if callers:
            r["called_by"] = callers
            reasons_keep.append("called by " + ", ".join(callers))
        jobs = act.jobs().get(name.lower(), [])
        if jobs:
            r["jobs"] = jobs
            reasons_keep.append("run by job " + ", ".join(jobs))
        runs = act.runs_by_people(name)
        if runs:
            r["recent_runs_by_people"] = runs
            reasons_keep.append(f"run by {runs[0]['by']} {runs[0]['ago']}")
        if reasons_keep:
            r["verdict"] = "keep for now" if (on_branches or callers or jobs) else "check first"
            r["why"] = "; ".join(reasons_keep)
        else:
            r["verdict"] = "stray"
            r["why"] = ("not in the default branch, on no branch, called by no package, run by no job"
                        + (f", and no one ran it in {act.days:g} days" if act.catalog is not None else ""))
        out.append(r)
    return out


def stray_blockers(act: Activity, name: str) -> list[str]:
    """Why a package must not be removed as a stray (empty when it is one)."""
    scan = act.scan
    if scan is None or not scan.default:
        return ["no git repository with a default branch here, so it cannot be shown to be a stray"]
    if name.lower() in scan.main_packages:
        return [f"{name} is in {scan.default}"]
    r = next((x for x in strays(act, set()) if x["package"].lower() == name.lower()), None)
    if r is None:
        return [f"{name} is not deployed"]
    return [] if r["verdict"] == "stray" else [r["why"]]


def local_packages(directory: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in directory.glob("*.dtsx") if not f.name.lower().endswith(".server.dtsx")}
