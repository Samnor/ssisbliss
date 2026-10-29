"""Deploy Visual Studio's build and onprem-ssis build of the same projects side by side and compare
what SSISDB records. Needs lab/tools/build-corpus.ps1 to have run (C:\\lab\\work\\corpus).

    lab.py run lab/tools/compare_build.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ssisbliss.ssis.build import build
from ssisbliss.ssis.catalog import SqlCatalog, connection_string

CORPUS = Path(r"C:\lab\work\corpus")
PROJECT = "Corpus"   # SSISDB requires the name in the .ispac


def rows(cat: SqlCatalog, sql: str, *params) -> list[tuple]:
    cur = cat.conn.cursor()
    cur.execute(sql, *params)
    return [tuple(str(v) if v is not None else None for v in r) for r in cur.fetchall()]


def recorded(cat: SqlCatalog, folder: str) -> dict:
    return {
        "packages": rows(cat, f"""SELECT k.name, k.description, k.version_major, k.version_minor, k.version_build,
                                         k.package_format_version, k.entry_point
                                  FROM catalog.packages k JOIN catalog.projects p ON p.project_id = k.project_id
                                  JOIN catalog.folders f ON f.folder_id = p.folder_id
                                  WHERE f.name = ? AND p.name = ? ORDER BY k.name""", folder, PROJECT),
        "parameters": rows(cat, f"""SELECT op.object_type, op.object_name, op.parameter_name, op.data_type, op.sensitive,
                                           op.required, CAST(op.design_default_value AS nvarchar(4000))
                                    FROM catalog.object_parameters op JOIN catalog.projects p ON p.project_id = op.project_id
                                    JOIN catalog.folders f ON f.folder_id = p.folder_id
                                    WHERE f.name = ? AND p.name = ? ORDER BY 1, 2, 3""", folder, PROJECT),
    }


def main() -> int:
    cat = SqlCatalog(connection_string("localhost,14331", trust_server_certificate=True, app="onprem-compare"))
    cur = cat.conn.cursor()
    results = []
    for variant in ("basic", "configs", "drift", "pkgparams"):
        vs = (CORPUS / variant / "build-devenv" / "bin" / "Development" / "Corpus.ispac").read_bytes()
        mine, report = build(CORPUS / variant / "src" / "Corpus.dtproj")
        out: dict = {"variant": variant}
        sb = (CORPUS / variant / "build-ssisbuild" / "out" / "Development" / "Development" / "Corpus.ispac").read_bytes()
        for label, data in (("vs", vs), ("ours", mine), ("ssisbuild", sb)):
            folder = f"BUILDCMP_{variant}_{label}"
            cur.execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{folder}') "
                        f"EXEC catalog.create_folder @folder_name = N'{folder}'")
            try:
                op = cat.deploy_project(folder, PROJECT, data)
                out[f"{label}_deploy"] = cat.operation(op)["status"]
            except Exception as e:  # noqa: BLE001
                out[f"{label}_deploy"] = f"failed: {e}"[:400]
        if out["vs_deploy"] != "succeeded" or out["ours_deploy"] != "succeeded":
            results.append(out)
            continue
        a, b = recorded(cat, f"BUILDCMP_{variant}_vs"), recorded(cat, f"BUILDCMP_{variant}_ours")
        out["packages_recorded"] = len(a["packages"])
        out["parameters_recorded"] = len(a["parameters"])
        for key in ("packages", "parameters"):
            out[f"{key}_identical"] = a[key] == b[key]
            if a[key] != b[key]:   # names only: SSM returns at most 24 KB
                label = (lambda r: f"{r[1]}::{r[2]}") if key == "parameters" else (lambda r: "|".join(map(str, r)))
                out[f"{key}_only_vs"] = sorted({label(r) for r in set(a[key]) - set(b[key])})
                out[f"{key}_only_ours"] = sorted({label(r) for r in set(b[key]) - set(a[key])})
        if out["ssisbuild_deploy"] == "succeeded":
            c = recorded(cat, f"BUILDCMP_{variant}_ssisbuild")
            # SSISBuild ships stale package metadata when a package changed outside Visual Studio.
            out["ours_equals_ssisbuild"] = {k: b[k] == c[k] for k in ("packages", "parameters")}
        out["refreshed_metadata"] = report["refreshed_metadata"]
        results.append(out)
    print(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
