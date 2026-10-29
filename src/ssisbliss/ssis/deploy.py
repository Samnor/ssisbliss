"""Plan and apply SSIS catalog deployments with guard rails.

Flow: build a Plan (read-only, runs every check), show it, then apply it with the
plan's plan_id. Apply recomputes the plan and refuses if anything moved.

Overwrite protection: with `bases` (package -> sha256 of the deployed version the local
copy started from), updating a package whose server copy differs from its base is an
error unless the package is named in `overwrite`. bases=None disables the check.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog, Project, ServerInfo
from .dtsx import (DONT_SAVE_SENSITIVE, MAX_PACKAGE_FORMAT, SERVER_STORAGE, PackageError, has_sensitive_values,
                   read_package, sha256)
from .ispac import Ispac

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_PLAN_MISMATCH, EXIT_VERIFY = 0, 1, 2, 3, 4
# ServerStorage is what the catalog exports; such a file holds no sensitive values.
DEPLOYABLE_PROTECTION = {DONT_SAVE_SENSITIVE, SERVER_STORAGE}


@dataclass
class Plan:
    action: str
    folder: str
    project: str
    method: str | None = None
    server: ServerInfo | None = None
    target: Project | None = None
    checks: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    # Not serialized: what apply sends, and the server state it was planned against.
    payload: object = None
    server_ispac: Ispac | None = None
    # For overwrite errors: the project version each base was taken at, and the project's versions.
    base_lsns: dict = field(default_factory=dict)
    versions: list = field(default_factory=list)

    def check(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"check": name, "status": status, "detail": detail})

    @property
    def errors(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "error"]

    @property
    def to_deploy(self) -> list[dict]:
        return [c for c in self.changes if c["change"] != "unchanged"]

    @property
    def can_apply(self) -> bool:
        return not self.errors and (bool(self.to_deploy) or self.action == "rollback"
                                    or bool(self.extra.get("project_items_changed")))

    @property
    def plan_id(self) -> str:
        basis = {
            "action": self.action, "method": self.method,
            "server": self.server.server_name if self.server else None,
            "folder": self.folder, "project": self.project,
            "lsn": self.target.object_version_lsn if self.target else None,
            "deploy": sorted((c["package"], c["sha256"]) for c in self.to_deploy),
            "extra": {k: v for k, v in self.extra.items() if k in ("project_items", "to_lsn", "items_deploy")},
        }
        return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        refused = {"refused_because": [f"{c['check']}: {c['detail']}" for c in self.errors]} if self.errors else {}
        return {
            **refused,          # first, so a reader (or an agent) sees why before the details
            "action": self.action,
            "method": self.method,
            "target": {
                "server": self.server.server_name if self.server else None,
                "version": self.server.product_version if self.server else None,
                "folder": self.folder,
                "project": self.project,
                "object_version_lsn": self.target.object_version_lsn if self.target else None,
                "last_deployed": self.target.last_deployed if self.target else None,
                "deployed_by": self.target.deployed_by if self.target else None,
            },
            "changes": self.changes,
            "retained_packages": self.retained,
            **self.extra,
            "checks": self.checks,
            "can_apply": self.can_apply,
            "plan_id": self.plan_id if self.can_apply else None,
        }


# ------------------------------------------------------------------ planning --

def _target(catalog: Catalog, plan: Plan, allow_running: bool) -> bool:
    """Common target checks. Returns False if planning cannot continue."""
    plan.server = info = catalog.server_info()
    if not info.has_ssisdb:
        plan.check("ssisdb", "error", f"{info.server_name} has no SSISDB catalog")
        return False
    plan.check("ssisdb", "ok", f"{info.server_name} {info.product_version}")
    # The catalog only shows a non-admin the folders and projects they have READ on.
    hidden = "" if info.is_admin else " (or you have no READ permission on it)"
    if not catalog.folder_exists(plan.folder):
        plan.check("folder", "error", f"folder '{plan.folder}' does not exist{hidden}")
        return False
    plan.target = catalog.project(plan.folder, plan.project)
    if plan.target is None:
        plan.check("project", "error", f"project '{plan.project}' does not exist in folder '{plan.folder}'{hidden}")
        return False
    plan.check("project", "ok", f"project_id {plan.target.project_id}, lsn {plan.target.object_version_lsn}")
    if info.is_admin:
        plan.check("permission", "ok", "member of ssis_admin or sysadmin")
    elif catalog.can_modify_project(plan.target.project_id):
        plan.check("permission", "ok", "MODIFY permission on the project")
    else:
        plan.check("permission", "error", f"no MODIFY permission on project {plan.folder}/{plan.project}; ask a DBA "
                                          f"for MODIFY_OBJECTS on folder {plan.folder} (or ssis_admin)")

    running = catalog.running_executions(plan.folder, plan.project)
    if running:
        plan.check("running_executions", "warn" if allow_running else "error",
                   f"{running} execution(s) of this project are running"
                   + ("" if allow_running else "; wait, or pass --allow-running"))
    else:
        plan.check("running_executions", "ok", "none")
    return True


def _check_package(plan: Plan, name: str, data: bytes, current: dict[str, str], allow_new: bool,
                   params: set[str], conmgrs: set[str], bases: dict[str, str] | None,
                   overwrite: set[str]) -> None:
    if not name.lower().endswith(".dtsx"):
        plan.check(f"package:{name}", "error", "package names must end in .dtsx")
        return
    try:
        pkg = read_package(name, data)
    except PackageError as e:
        plan.check(f"package:{name}", "error", str(e))
        return

    if name in current:
        change = "unchanged" if current[name] == pkg.sha256 else "update"
    elif allow_new:
        change = "add"
    else:
        plan.check(f"package:{name}", "error", "not in the deployed project; pass --allow-new to add it")
        change = "add"
    plan.changes.append({"package": name, "change": change, "sha256": pkg.sha256,
                         "current_sha256": current.get(name), "base_sha256": (bases or {}).get(name)})
    if change == "update" and bases is not None:
        _check_overwrite(plan, name, current[name], bases.get(name), overwrite)

    problems: list[tuple[str, str]] = []
    major = plan.server.major_version if plan.server else 0
    max_fmt = MAX_PACKAGE_FORMAT.get(major)
    if max_fmt and pkg.package_format_version and pkg.package_format_version > max_fmt:
        problems.append(("error", f"PackageFormatVersion {pkg.package_format_version} is newer than "
                                  f"SQL Server {major} supports ({max_fmt})"))
    if pkg.max_assembly_major and major:
        if pkg.max_assembly_major > major:
            problems.append(("error", f"built against SSIS {pkg.max_assembly_major}.0 components; server is {major}.0. "
                                      f"Set TargetServerVersion to match the server and rebuild"))
        elif pkg.max_assembly_major < major:
            problems.append(("warn", f"built against SSIS {pkg.max_assembly_major}.0 components; "
                                     f"server {major}.0 upgrades it on load"))
    if pkg.protection_level not in DEPLOYABLE_PROTECTION:
        problems.append(("error", f"ProtectionLevel is {pkg.protection_level_name}; catalog deployments must use "
                                  f"DontSaveSensitive with sensitive values in parameters/environments"))
    missing_params = sorted(pkg.project_parameters - params)
    if missing_params:
        problems.append(("error", f"uses project parameters not in the project: {', '.join(missing_params)}"))
    missing_cm = sorted(pkg.project_connection_managers - conmgrs)
    if missing_cm:
        problems.append(("error", f"uses project connection managers not in the project: {', '.join(missing_cm)}"))

    if problems:
        for status, detail in problems:
            plan.check(f"package:{name}", status, detail)
    elif change != "unchanged":
        plan.check(f"package:{name}", "ok", f"{change}, {pkg.protection_level_name}")
    else:
        plan.check(f"package:{name}", "ok", "identical to deployed package; skipped")


def deploys_since(versions: list[dict], lsn: int | None, limit: int = 5) -> str:
    """'lsn 105 by LAB\\bob at 2026-10-29T10:02, ...' for project versions after `lsn` (newest first)."""
    newer = [v for v in versions if lsn is None or v["object_version_lsn"] > lsn]
    parts = [f"lsn {v['object_version_lsn']}" + (f" by {v['created_by']}" if v.get("created_by") else "")
             + (f" at {str(v['created_time'])[:16]}" if v.get("created_time") else "") for v in newer[:limit]]
    more = f" and {len(newer) - limit} more" if len(newer) > limit else ""
    return ", ".join(parts) + more


def _check_overwrite(plan: Plan, name: str, server_sha: str, base_sha: str | None, overwrite: set[str]) -> None:
    """Refuse to replace a deployed package that changed since the version this copy started from."""
    if base_sha == server_sha:
        plan.check(f"overwrite:{name}", "ok", "server copy is the version you started from")
        return
    t = plan.target
    base_lsn = plan.base_lsns.get(name)
    since = deploys_since(plan.versions, base_lsn) if plan.versions and base_lsn is not None else ""
    if since:
        who = sorted({v["created_by"] for v in plan.versions if v["object_version_lsn"] > base_lsn and v.get("created_by")})
        last = (f"{' and '.join(who) + ' deployed' if who else 'deployed'} since your version (lsn {base_lsn}): {since}; "
                f"tell the developer who, so they can talk to them before merging")
    else:
        last = f"project last deployed by {t.deployed_by} at {t.last_deployed} (lsn {t.object_version_lsn})" if t else ""
    if name in overwrite:
        plan.check(f"overwrite:{name}", "warn", f"replacing a server copy you have not pulled (--overwrite); {last}")
    elif base_sha is None:
        plan.check(f"overwrite:{name}", "error",
                   f"no record of which deployed version this file started from, and it differs from the server; "
                   f"{last}. Run `ssisbliss pull` to fetch the deployed copy and merge, "
                   f"or pass --overwrite {name} if replacing it is intended")
    else:
        plan.check(f"overwrite:{name}", "error",
                   f"the server copy changed since you pulled or deployed it (someone else deployed it); "
                   f"{last}. Pull and merge, or pass --overwrite {name}")


def _match_case(plan: Plan, names: list[str], current: dict[str, str]) -> dict[str, str]:
    """Map file names that differ from exactly one deployed package only in case to that package.

    The catalog compares package names case-insensitively, so `stage_customer.dtsx` can only
    ever mean the deployed `Stage_Customer.dtsx`; adding it as a new package is never right.
    """
    renamed: dict[str, str] = {}
    for name in names:
        same = [c for c in current if c.lower() == name.lower()]
        if name not in current and len(same) == 1:
            renamed[name] = same[0]
            plan.check(f"name:{name}", "warn",
                       f"file is named {name} but the deployed package is {same[0]} (catalog names ignore case); "
                       f"deploying it as {same[0]}. Renaming the file to {same[0]} (e.g. `git mv`) avoids this")
    if renamed:
        plan.extra["renamed"] = renamed
    return renamed


def plan_packages(catalog: Catalog, folder: str, project: str, packages: list[tuple[str, bytes]],
                  allow_new: bool = False, allow_running: bool = False,
                  bases: dict[str, str] | None = None, overwrite: set[str] | frozenset = frozenset(), base_lsns: dict[str, int] | None = None) -> Plan:
    """Deploy individual .dtsx files into an existing project (catalog.deploy_packages)."""
    plan = Plan("deploy-packages", folder, project, method="deploy_packages")
    if not packages:
        plan.check("input", "error", "no packages given")
        return plan
    names = [n for n, _ in packages]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        plan.check("input", "error", f"duplicate package names: {', '.join(dupes)}")
    if not _target(catalog, plan, allow_running):
        return plan
    plan.base_lsns = dict(base_lsns or {})
    if plan.base_lsns:
        plan.versions = catalog.versions(plan.target.project_id)   # type: ignore[union-attr]

    assert plan.target is not None
    server = plan.server_ispac = Ispac(catalog.get_project(folder, project))
    current = server.package_hashes()
    renamed = _match_case(plan, [n for n, _ in packages], current)
    if renamed:
        packages = [(renamed.get(n, n), d) for n, d in packages]
        bases = None if bases is None else {renamed.get(n, n): s for n, s in bases.items()}
        names = [n for n, _ in packages]
        dupes = sorted({n for n in names if names.count(n) > 1 and n in renamed.values()})
        if dupes:
            plan.check("input", "error", f"several files are the same package (names differ only in case): "
                                         f"{', '.join(dupes)}")
    params = set(catalog.project_parameters(plan.target.project_id))
    conmgrs = server.connection_manager_names()
    for name, data in packages:
        _check_package(plan, name, data, current, allow_new, params, conmgrs, bases, set(overwrite))

    deploying = {c["package"] for c in plan.to_deploy}
    plan.retained = sorted(set(current) - deploying)
    plan.payload = [(n, d) for n, d in packages if n in deploying]
    if not plan.to_deploy and not plan.errors:
        plan.check("changes", "ok", "nothing to deploy; every package is identical to the deployed one")
    return plan


def resolve_only(source: Ispac, only: list[str]) -> list[str]:
    """Accept `Name` for `Name.dtsx` when the .ispac has exactly that package; leave other names as given."""
    names = set(source.package_names)
    return [f"{n}.dtsx" if n not in names and f"{n}.dtsx" in names else n for n in only]


def plan_project(catalog: Catalog, folder: str, project: str, source: Ispac, only: list[str],
                 project_items: str = "keep", allow_new: bool = False, allow_running: bool = False,
                 bases: dict[str, str] | None = None, overwrite: set[str] | frozenset = frozenset(), base_lsns: dict[str, int] | None = None,
                 items: list[str] | None = None, item_bases: dict[str, str] | None = None) -> Plan:
    """Take `only` packages from a built .ispac; keep every other deployed package as it is.

    items: project parameters / connection managers (names) to take from the .ispac as well, one by one;
    every other project item stays as deployed (catalog.deploy_project with a per-item merge).

    project_items="keep": project parameters / connection managers stay as deployed; uses
    catalog.deploy_packages. "update": also take them from the .ispac; uses
    catalog.deploy_project with a merged .ispac.
    """
    method = "deploy_packages" if project_items == "keep" and not items else "deploy_project"
    plan = Plan("deploy-project", folder, project, method=method)
    plan.extra["project_items"] = "items" if items else project_items
    if not only and not items:
        plan.check("input", "error", "--only must name at least one package (or --item a project parameter / "
                                     "connection manager); a full replace is never implied")
        return plan
    item_ids = []
    for n in items or []:
        rid = source.resolve_item(n)
        if rid is None:
            plan.check("input", "error", f"--item {n}: no such project parameter or connection manager in the .ispac")
        item_ids.append(rid)
    if plan.errors:
        return plan
    not_in_source = [n for n in only if n not in source.package_names]
    if not_in_source:
        plan.check("input", "error", f"not in the .ispac: {', '.join(not_in_source)}; it contains "
                                     f"{', '.join(sorted(source.package_names))} (names are case-sensitive)")
        return plan
    if not _target(catalog, plan, allow_running):
        return plan
    plan.base_lsns = dict(base_lsns or {})
    if plan.base_lsns:
        plan.versions = catalog.versions(plan.target.project_id)   # type: ignore[union-attr]

    assert plan.target is not None
    server = plan.server_ispac = Ispac(catalog.get_project(folder, project))
    current = server.package_hashes()

    # Project-level files: compare and decide what packages are checked against.
    src_items, srv_items = source.project_items(), server.project_items()
    differing = sorted(n for n in set(src_items) | set(srv_items) if src_items.get(n) != srv_items.get(n))
    plan.extra["project_items_differ"] = differing
    if items:
        src_h, srv_h = source.item_hashes(), server.item_hashes()
        changes = []
        for rid in item_ids:
            change = "unchanged" if src_h[rid] == srv_h.get(rid) else ("add" if rid not in srv_h else "update")
            changes.append({"item": rid, "change": change, "sha256": src_h[rid], "current_sha256": srv_h.get(rid)})
            if change == "update":
                _check_overwrite(plan, rid, srv_h[rid], (item_bases or {}).get(rid), set(overwrite))
        plan.extra["items"] = changes
        plan.extra["items_deploy"] = sorted((c["item"], c["sha256"]) for c in changes if c["change"] != "unchanged")
        plan.extra["project_items_changed"] = bool(plan.extra["items_deploy"])
        others = [n for n in differing if not any(rid.endswith(n) for rid in item_ids)]
        if others:
            plan.check("project_items", "warn", f"other project items differ from the .ispac and are kept as deployed: "
                                                f"{', '.join(others)}")
        params = set(catalog.project_parameters(plan.target.project_id)) | {
            r.removeprefix("param:") for r in item_ids if r.startswith("param:")}
        conmgrs = server.connection_manager_names() | source.connection_manager_names()
        retained_names = set(current) - set(only)
        sensitive = sorted(n for n in retained_names if has_sensitive_values(server.package(n)))
        if sensitive:
            plan.check("sensitive_values", "error",
                       f"deploying project items needs a full-project redeploy, which cannot carry over the sensitive "
                       f"values in: {', '.join(sensitive)}")
    elif project_items == "keep":
        if differing:
            plan.check("project_items", "warn", f"differ from the .ispac and are NOT deployed (keep): "
                                                f"{', '.join(differing)}")
        params = set(catalog.project_parameters(plan.target.project_id))
        conmgrs = server.connection_manager_names()
    else:
        params, conmgrs = source.parameter_names(), source.connection_manager_names()
        plan.extra["project_items_changed"] = bool(differing)
        plan.check("project_items", "ok", f"will deploy: {', '.join(differing) or 'no differences'}")
        if source.protection_level not in DEPLOYABLE_PROTECTION:
            plan.check("project_protection", "error", "the .ispac project is not DontSaveSensitive")
        retained_names = set(current) - set(only)
        sensitive = sorted(n for n in retained_names if has_sensitive_values(server.package(n)))
        if sensitive:
            plan.check("sensitive_values", "error",
                       f"retained packages hold sensitive values a full-project redeploy cannot carry over: "
                       f"{', '.join(sensitive)}. Use --project-items keep")
        # Retained packages must still resolve against the new project items.
        for n in sorted(retained_names):
            pkg = read_package(n, server.package(n))
            gone = sorted((pkg.project_parameters - params) | (pkg.project_connection_managers - conmgrs))
            if gone:
                plan.check(f"package:{n}", "error", f"retained package uses project items the .ispac removes: "
                                                    f"{', '.join(gone)}")
        removed = sorted(set(catalog.project_parameters(plan.target.project_id)) - params)
        bound = sorted({b["parameter_name"] for b in catalog.parameter_bindings(plan.target.project_id)
                        if b["object_type"] == 20 and b["parameter_name"] in removed})
        if bound:
            plan.check("bindings", "error", f"project parameters with configured values would be removed: "
                                            f"{', '.join(bound)}")

    for name in only:
        _check_package(plan, name, source.package(name), current, allow_new, params, conmgrs, bases,
                       set(overwrite))

    deploying = [c["package"] for c in plan.to_deploy]
    plan.retained = sorted(set(current) - set(deploying))
    if method == "deploy_packages":
        plan.payload = [(n, source.package(n)) for n in deploying]
    elif items:
        plan.payload = server.merged_items(source, deploying, [i for i, _ in plan.extra["items_deploy"]])
    else:
        plan.payload = server.merged(source, deploying, project_items=True)
    if not plan.to_deploy and not plan.extra.get("project_items_changed") and not plan.errors:
        plan.check("changes", "ok", "nothing to deploy")
    return plan


def plan_remove(catalog: Catalog, folder: str, project: str, names: list[str], bases: dict[str, str] | None = None,
                overwrite: set[str] | frozenset = frozenset(), allow_running: bool = False,
                base_lsns: dict[str, int] | None = None, stray: dict[str, list[str]] | None = None) -> Plan:
    """Remove packages from a deployed project (SSISDB has no per-package delete: catalog.deploy_project
    with the deployed project minus those packages). Only packages whose server copy is your version,
    or, with stray (name -> reasons it is not a stray), packages nobody has: not in the default branch,
    on no branch, called by nothing, run by no job and by nobody lately."""
    plan = Plan("remove-packages", folder, project, method="deploy_project")
    if not names:
        plan.check("input", "error", "name at least one package to remove")
        return plan
    if not _target(catalog, plan, allow_running):
        return plan
    assert plan.target is not None
    plan.base_lsns = dict(base_lsns or {})
    plan.versions = catalog.versions(plan.target.project_id)
    server = plan.server_ispac = Ispac(catalog.get_project(folder, project))
    current = server.package_hashes()
    names = [next((c for c in current if c.lower() == n.lower()), n) for n in names]
    unknown = [n for n in names if n not in current]
    if unknown:
        plan.check("input", "error", f"not deployed in {folder}/{project}: {', '.join(unknown)}")
        return plan
    if len(names) >= len(current):
        plan.check("input", "error", "that would remove every package; a project keeps at least one")
        return plan
    for n in names:
        if stray is not None:
            blockers = next((v for k, v in stray.items() if k.lower() == n.lower()), ["not checked"])
            if not blockers:
                plan.check(f"stray:{n}", "ok", "a stray: not in the default branch, on no branch, called by no "
                                               "package, run by no job or person lately")
            elif n in overwrite:
                plan.check(f"stray:{n}", "warn", f"removing although {'; '.join(blockers)} (--overwrite)")
            else:
                plan.check(f"stray:{n}", "error", f"not a stray: {'; '.join(blockers)}. Ask whoever works on it, "
                                                  f"or pass --overwrite {n} if they agreed")
        else:
            _check_overwrite(plan, n, current[n], (bases or {}).get(n), set(overwrite))
        plan.changes.append({"package": n, "change": "remove", "sha256": current[n], "current_sha256": current[n],
                             "base_sha256": (bases or {}).get(n)})
    retained = sorted(set(current) - set(names))
    plan.retained = retained
    for other in retained:   # an Execute Package task calling a removed package would break at run time
        text = server.package(other).decode("utf-8-sig", errors="replace")
        calls = [n for n in names if re.search(rf"<PackageName>\s*{re.escape(n)}\s*</PackageName>", text, re.I)]
        if calls:
            plan.check(f"reference:{other}", "error", f"{other} runs {', '.join(calls)} (Execute Package task)")
    sensitive = sorted(n for n in retained if has_sensitive_values(server.package(n)))
    if sensitive:
        plan.check("sensitive_values", "error", f"removing needs a full-project redeploy, which cannot carry over the "
                                                f"sensitive values in: {', '.join(sensitive)}")
    plan.payload = server.without(names)
    plan.check("method", "ok", "catalog.deploy_project with the deployed project minus the removed packages")
    return plan


def undone_versions(versions: list[dict]) -> set[int]:
    """Versions an earlier restore already took off the server: restoring version R at time T undoes every
    version above R created before T (SSISDB keeps them, but they are no longer the project's history)."""
    gone: set[int] = set()
    for r in versions:
        t = r.get("last_restored_time")
        if t:
            gone |= {v["object_version_lsn"] for v in versions
                     if v["object_version_lsn"] > r["object_version_lsn"] and str(v.get("created_time") or "") < str(t)}
    return gone


def plan_rollback(catalog: Catalog, folder: str, project: str, to_lsn: int, allow_running: bool = False,
                  undo_others: bool = False) -> Plan:
    """Restore a stored project version (catalog.restore_project). This replaces every package and
    parameter, so it is refused when someone else deployed after that version, unless undo_others."""
    plan = Plan("rollback", folder, project, method="restore_project")
    plan.extra["to_lsn"] = to_lsn
    if not _target(catalog, plan, allow_running):
        return plan
    assert plan.target is not None
    versions = catalog.versions(plan.target.project_id)
    plan.extra["versions"] = versions[:10]
    if to_lsn == plan.target.object_version_lsn:
        plan.check("version", "error", f"lsn {to_lsn} is already the current version")
    elif not any(v["object_version_lsn"] == to_lsn for v in versions):
        plan.check("version", "error", f"lsn {to_lsn} is not a stored version of this project "
                                       f"(catalog retention may have removed it)")
    else:
        plan.check("version", "ok", f"restore lsn {to_lsn}")
        me = catalog.login().lower()
        gone = undone_versions(versions)
        others = [v for v in versions if v["object_version_lsn"] > to_lsn and v["object_version_lsn"] not in gone
                  and v.get("created_by")
                  and v["created_by"].lower() != me]
        if others:
            since = deploys_since(others, to_lsn)
            if undo_others:
                plan.check("others", "warn", f"also undoes deploys by others since lsn {to_lsn} (--undo-others): {since}")
            else:
                plan.check("others", "error",
                           f"restoring lsn {to_lsn} replaces the whole project, so it would also undo deploys by others "
                           f"since then: {since}. To put back only your package, deploy its earlier version from git "
                           f"(deploy-packages ... PKG.dtsx --from-ref <commit or branch>, e.g. main); pass --undo-others "
                           f"only if they agreed")
    plan.server_ispac = Ispac(catalog.get_project(folder, project))
    return plan


# ------------------------------------------------------------------- apply --

def _bindings(catalog: Catalog, project_id: int) -> list[dict]:
    out = []
    for b in catalog.parameter_bindings(project_id):
        b = dict(b)
        value = b.pop("value", None)
        b["value_sha256"] = sha256(str(value).encode()) if value is not None else None
        out.append(b)
    return out


def _env_refs(catalog: Catalog, project_id: int) -> list[dict]:
    return [{k: r[k] for k in ("reference_type", "environment_folder_name", "environment_name")}
            for r in catalog.environment_references(project_id)]


def apply(catalog: Catalog, plan: Plan, plan_id: str, backup_dir: Path) -> tuple[dict, int]:
    if plan.errors:
        return {"status": "refused", "reason": "plan has errors", "plan": plan.to_dict()}, EXIT_REFUSED
    if not plan.can_apply:
        return {"status": "noop", "plan": plan.to_dict()}, EXIT_OK
    if plan_id != plan.plan_id:
        t = plan.target
        changed = (f"{plan.folder}/{plan.project} is now version {t.object_version_lsn}, last deployed by {t.deployed_by} at "
                   f"{t.last_deployed}" if t else "the target changed")
        keeps = ("only your packages are replaced; every other package, including whatever changed, is kept as it is "
                 "now" if plan.method == "deploy_packages" else "see the fresh plan for what it replaces")
        nxt = (f"The fresh plan below re-ran every check (including others' changes to your packages) and can be "
               f"applied ({keeps}): review it, then run the same command with --apply --plan-id {plan.plan_id}"
               if plan.can_apply else "The fresh plan below has errors; see its checks")
        return {"status": "refused", "reason": f"the server changed after your plan was made: {changed}. {nxt}.",
                "expected": plan_id, "current": plan.plan_id, "plan": plan.to_dict()}, EXIT_PLAN_MISMATCH
    assert plan.target is not None and plan.server is not None and plan.server_ispac is not None

    pid, before_lsn = plan.target.project_id, plan.target.object_version_lsn
    before_packages = set(catalog.packages(pid))
    before_bindings = _bindings(catalog, pid)
    before_refs = _env_refs(catalog, pid)

    backup = _write_backup(backup_dir, plan, before_bindings, before_refs)
    rollback_cmd = (f'ssisbliss rollback --server "<server>" --folder "{plan.folder}" '
                    f'--project "{plan.project}" --to-lsn {before_lsn}')

    try:
        if plan.method == "deploy_packages":
            op_id = catalog.deploy_packages(plan.folder, plan.project, plan.payload)  # type: ignore[arg-type]
        elif plan.method == "deploy_project":
            op_id = catalog.deploy_project(plan.folder, plan.project, plan.payload)  # type: ignore[arg-type]
        else:
            catalog.restore_project(plan.folder, plan.project, plan.extra["to_lsn"])
            op_id = None
    except Exception as e:  # the catalog raises on failed deployments
        return {"status": "failed", "error": str(e), "backup": str(backup),
                "previous_lsn": before_lsn, "rollback": rollback_cmd}, EXIT_ERROR

    result: dict = {"status": "deployed", "method": plan.method, "backup": str(backup),
                    "previous_lsn": before_lsn, "rollback": rollback_cmd}
    if op_id is not None:
        op = catalog.operation(op_id)
        result["operation"] = op
        if op["status"] != "succeeded":
            result["status"] = "failed"
            return result, EXIT_ERROR

    after = catalog.project(plan.folder, plan.project)
    assert after is not None
    result["new_lsn"] = after.object_version_lsn
    issues = _verify(catalog, plan, after, before_packages, before_bindings, before_refs)
    result["verification"] = issues or ["ok"]
    if issues:
        result["status"] = "deployed_with_differences"
        return result, EXIT_VERIFY
    result["status"] = "verified"
    return result, EXIT_OK


def _verify(catalog: Catalog, plan: Plan, after: Project, before_packages: set[str],
            before_bindings: list[dict], before_refs: list[dict]) -> list[str]:
    issues = []
    assert plan.target is not None and plan.server_ispac is not None
    if plan.action != "rollback" and after.object_version_lsn == plan.target.object_version_lsn:
        issues.append("project version did not change")
    if plan.action == "rollback":
        return issues

    after_packages = set(catalog.packages(after.project_id))
    removed = {c["package"] for c in plan.to_deploy if c["change"] == "remove"}
    lost = sorted(before_packages - after_packages - removed)
    if lost:
        issues.append(f"packages missing after deploy: {', '.join(lost)}")

    deployed = Ispac(catalog.get_project(plan.folder, plan.project)).package_hashes()
    for c in plan.to_deploy:
        if c["change"] == "remove":
            if c["package"] in deployed:
                issues.append(f"{c['package']}: still deployed after removing it")
        elif deployed.get(c["package"]) != c["sha256"]:
            issues.append(f"{c['package']}: deployed content differs from the file")
    before = plan.server_ispac.package_hashes()
    for name in plan.retained:
        if deployed.get(name) != before.get(name):
            issues.append(f"{name}: retained package changed")

    if plan.extra.get("items"):
        now = Ispac(catalog.get_project(plan.folder, plan.project)).item_hashes()
        before_items = plan.server_ispac.item_hashes()
        taken = dict(plan.extra["items_deploy"])
        for rid in set(now) | set(before_items) | set(taken):
            want = taken.get(rid, before_items.get(rid))
            if now.get(rid) != want:
                issues.append(f"{rid}: {'not as in the .ispac' if rid in taken else 'project item changed'} after deploy")

    after_bindings = _bindings(catalog, after.project_id)
    key = lambda b: (b["object_type"], b["object_name"], b["parameter_name"])  # noqa: E731
    after_by_key = {key(b): b for b in after_bindings}
    for b in before_bindings:
        if b["object_type"] == 30 and b["object_name"] in removed:
            continue   # a removed package's own parameter bindings go with it
        a = after_by_key.get(key(b))
        if a is None:
            issues.append(f"parameter binding lost: {b['object_name']}.{b['parameter_name']}")
        elif a != b:
            issues.append(f"parameter binding changed: {b['object_name']}.{b['parameter_name']}")
    if _env_refs(catalog, after.project_id) != before_refs:
        issues.append("environment references changed")
    return issues


def _write_backup(backup_dir: Path, plan: Plan, bindings: list[dict], refs: list[dict]) -> Path:
    assert plan.server is not None and plan.target is not None and plan.server_ispac is not None
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)  # noqa: E731
    d = backup_dir / safe(plan.server.server_name) / safe(plan.folder) / safe(plan.project)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-lsn{plan.target.object_version_lsn}"
    (d / f"{stem}.ispac").write_bytes(plan.server_ispac.data)
    (d / f"{stem}.json").write_text(json.dumps({"plan": plan.to_dict(), "parameter_bindings": bindings,
                                                "environment_references": refs}, indent=2, default=str))
    return d / f"{stem}.ispac"
