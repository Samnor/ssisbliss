"""lint, format and sql-diff: the packages and the SQL in them, read from files (lint --env also asks the server)."""
from __future__ import annotations

import json
from pathlib import Path

from .. import base
from ..deploy import EXIT_OK, EXIT_REFUSED
from ..dtsx import package_sha256
from .common import Reply, _env, _server_connection, reply


# ---------------------------------------------------------- lint, format, sql-diff --

def _package_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths or ["."]):
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in sorted(p.rglob("*.dtsx")) if not f.name.lower().endswith(".server.dtsx")
                    and not ({"bin", "obj", ".git"} & {x.lower() for x in f.relative_to(p).parts[:-1]})]
        else:
            raise ValueError(f"{p} does not exist")
    return out


def _changed_files(files: list[Path], base_ref: str | None) -> tuple[list[Path], str]:
    """The files that differ from the base (default: where this branch left the default branch), committed or not."""
    from ..gitscan import _default_branch, _git
    if not files:
        return files, ""
    root_s = _git(files[0].resolve().parent, "rev-parse", "--show-toplevel")
    if not root_s:
        raise ValueError("--changed needs a git repository")
    root = Path(root_s)
    ref = base_ref or _default_branch(root)
    if not ref:
        raise ValueError("--changed: no default branch found; pass --base REF")
    base = _git(root, "merge-base", ref, "HEAD") or ref
    changed = set((_git(root, "diff", "--name-only", base) or "").splitlines())
    changed |= set((_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    return [f for f in files if str(f.resolve().relative_to(root)) in changed], ref


def _baseline_path(directory: Path) -> Path:
    import hashlib
    return base.state_dir() / "lint-baseline" / (hashlib.sha256(base.repository_id(directory).encode()).hexdigest()[:20] + ".json")


def _oledb_providers(conn) -> set[str] | None:
    """The server's installed (64-bit) OLE DB providers; None when this login may not list them."""
    try:
        cur = conn.cursor()
        cur.execute("EXEC master.dbo.xp_enum_oledb_providers")
        return {r[0] for r in cur.fetchall()}
    except Exception:   # noqa: BLE001 - needs sysadmin on some servers: the check is skipped
        return None


def cmd_lint(a) -> Reply:
    from ..lint import RULES, SEVERITY, lint_package, server_parse
    from ..sqlsites import sites
    files = _package_files(a.paths)
    ref = ""
    if a.changed:
        files, ref = _changed_files(files, a.base)
    ignore = {r.strip().upper() for x in a.ignore for r in x.split(",") if r.strip()}
    findings = []
    for f in files:
        findings += [x for x in lint_package(str(f), f.read_bytes(), a.max_line, f.parent) if x.rule not in ignore]
    if a.env:
        from ..columns import ServerSchema, check
        from ..design import missing_providers
        from ..lint import Finding
        env = _env(a)
        raw = _server_connection(env, a)
        label = env.name or env.server
        schema = ServerSchema(raw)
        providers = _oledb_providers(raw) if "SSB511" not in ignore else None
        for f in files:
            data = f.read_bytes()
            if providers is not None:
                findings += missing_providers(str(f), data, providers, label)
            if "SSB210" not in ignore:
                for s in sites(data):
                    if s.dynamic:
                        continue
                    err = server_parse(raw, s.sql)
                    if err:
                        findings.append(Finding("SSB210", "error", str(f), s.task, s.path, 1, 1,
                                                f"{label} rejects it: {err}", "fix the SQL"))
            findings += [x for x in check(str(f), data, schema, label, f.parent) if x.rule not in ignore]
    baselined = 0
    bpath = _baseline_path(files[0].parent if files else Path("."))
    if a.update_baseline:
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(json.dumps(sorted({x.fingerprint() for x in findings}), indent=1))
    elif bpath.exists() and not a.no_baseline:
        known = set(json.loads(bpath.read_text()))
        baselined = sum(1 for x in findings if x.fingerprint() in known)
        findings = [x for x in findings if x.fingerprint() not in known]
    counts = {sev: sum(1 for x in findings if x.severity == sev) for sev in ("error", "warn", "info")}
    fail = SEVERITY[a.fail_on] if a.fail_on != "never" else 99
    doc: dict = {"packages": len(files), **({"compared_with": ref} if a.changed else {}), "counts": counts,
                 **({"baselined": baselined} if baselined else {}),
                 **({"baseline_written": str(bpath)} if a.update_baseline else {}),
                 "findings": [x.to_dict() for x in findings]}
    if counts["warn"] or counts["error"]:
        fixable = sorted({x.file for x in findings if x.rule == "SSB301"})
        if fixable:
            doc["format_command"] = "ssisbliss format " + " ".join(f'"{x}"' for x in fixable)
    if a.rules:
        doc = {"rules": {k: {"name": v[0], "severity": v[1], "what": v[2]} for k, v in RULES.items()}}
    return reply(doc, EXIT_REFUSED if any(SEVERITY[x.severity] >= fail for x in findings) else EXIT_OK)


def cmd_format(a) -> Reply:
    from ..sqlformat import format_sql
    from ..sqlsites import replace, sites
    files = _package_files(a.paths)
    if a.changed:
        files, _ = _changed_files(files, a.base)
    report, would_change = [], 0
    for f in files:
        data = f.read_bytes()
        found = sites(data)
        changes, skipped = {}, []
        for i, st in enumerate(found):
            if st.dynamic:
                continue
            new, why = format_sql(st.sql)
            if why:
                skipped.append({"task": st.path, "reason": why})
            elif new != st.sql:
                changes[i] = new
        entry: dict = {"file": str(f), "statements": len(found), "laid_out": len(changes)}
        if skipped:
            entry["left_as_is"] = skipped
        if changes:
            would_change += 1
            new_data = replace(data, changes, found)
            after = sites(new_data)          # the safety net: only the intended SQL changed
            ok = (len(after) == len(found) and all(after[i].sql == changes.get(i, found[i].sql) for i in range(len(found)))
                  and package_sha256(new_data) != "")
            if not ok:
                entry["error"] = "the rewritten package did not read back as intended; left unchanged"
            elif not a.check:
                f.write_bytes(new_data)
                entry["written"] = True
        report.append(entry)
    doc = {"files": len(files), "would_change" if a.check else "changed": would_change, "packages": report}
    if a.check and would_change:
        doc["fix"] = "ssisbliss format " + " ".join(f'"{e["file"]}"' for e in report if e["laid_out"])
    return reply(doc, EXIT_REFUSED if a.check and would_change else EXIT_OK)


def cmd_sql_diff(a) -> Reply:
    import difflib
    from ..gitscan import _default_branch, _git, _git_bytes
    from ..sqlformat import format_sql
    from ..sqlsites import sites
    files = _package_files(a.paths)
    root_s = _git(Path(a.paths[0] if a.paths else ".").resolve() if Path(a.paths[0] if a.paths else ".").is_dir()
                  else Path(a.paths[0]).resolve().parent, "rev-parse", "--show-toplevel")
    if not root_s:
        raise ValueError("sql-diff needs a git repository")
    root = Path(root_s)
    ref = a.base or _default_branch(root)
    if not ref:
        raise ValueError("no default branch found; pass --base REF")
    merge_base = _git(root, "merge-base", ref, "HEAD") or ref
    changed = set((_git(root, "diff", "--name-only", merge_base) or "").splitlines())
    changed |= set((_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    removed = [Path(root, n) for n in (_git(root, "diff", "--name-only", "--diff-filter=D", merge_base) or "").splitlines()
               if n.lower().endswith(".dtsx")]
    norm = (lambda q: format_sql(q)[0]) if a.formatted else (lambda q: q)
    packages = []
    for f in files + [r for r in removed if r not in files]:
        rel = str(f.resolve().relative_to(root)) if f.exists() else str(f.relative_to(root))
        if rel not in changed and f.exists():
            continue
        old = _git_bytes(root, "show", f"{merge_base}:{rel}")
        new = f.read_bytes() if f.exists() else None
        before = {(s.kind, s.path, s.extra.get("property", "")): s for s in (sites(old) if old else [])}
        after = {(s.kind, s.path, s.extra.get("property", "")): s for s in (sites(new) if new else [])}
        tasks = []
        for key in list(before) + [k for k in after if k not in before]:
            b, n = before.get(key), after.get(key)
            bs, ns = (norm(b.sql) if b else ""), (norm(n.sql) if n else "")
            if bs == ns:
                continue
            diff = "".join(difflib.unified_diff((bs + "\n").splitlines(True) if b else [], (ns + "\n").splitlines(True) if n else [],
                                               f"{rel} ({ref})", f"{rel}", n=3))
            tasks.append({"task": key[1], "change": "added" if not b else "removed" if not n else "changed", "diff": diff})
        if tasks or old is None or new is None:
            packages.append({"package": rel, "status": "added" if old is None else "removed" if new is None else "changed",
                             "sql_changes": tasks})
    if a.markdown:
        lines = [f"### SQL changes (compared with `{ref}`)", ""]
        if not any(p["sql_changes"] for p in packages):
            lines.append("No SQL changed.")
        for p in packages:
            for t in p["sql_changes"]:
                lines += [f"**{p['package']}** › `{t['task']}` ({t['change']})", "", "```diff", t["diff"].rstrip("\n"), "```", ""]
        return Reply(None, text="\n".join(lines).rstrip() + "\n")
    return reply({"compared_with": ref, "merge_base": merge_base, "formatted": bool(a.formatted), "packages": packages})

