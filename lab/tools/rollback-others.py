"""Rollback's "others deployed since" check against the real SSISDB, the parts the developer-login check
does not cover: the login the tool sees matches object_versions.created_by, so your own deploys never count
as someone else's; and a colleague's version that a restore already undid (labdev's, which the developer-login
check restores away) no longer counts either."""
import json
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.deploy import plan_rollback, undone_versions

SERVER, F, P = "localhost,14331", "ROLLBACK", "SILVER"
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
c.conn.cursor().execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{F}') EXEC catalog.create_folder @folder_name = N'{F}'")
if c.project(F, P):
    c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)
data = c.get_project("SILVER", "SILVER")
c.deploy_project(F, P, data)
first = c.project(F, P).object_version_lsn
c.deploy_project(F, P, data)
me = c.login()
result = {"login": me, "created_by": sorted({v["created_by"] for v in c.versions(c.project(F, P).project_id)}),
          "own_rollback_errors": [x["detail"] for x in plan_rollback(c, F, P, first).errors]}
c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)

stage = c.project("SILVER", "SILVER")
versions = c.versions(stage.project_id)
gone = undone_versions(versions)
undone = next((v for v in versions if v["object_version_lsn"] in gone and v["created_by"].lower() != me.lower()), None)
if undone:
    below = [v["object_version_lsn"] for v in versions if v["object_version_lsn"] < undone["object_version_lsn"]
             and v["object_version_lsn"] != stage.object_version_lsn]
    result["undone"] = {"lsn": undone["object_version_lsn"], "by": undone["created_by"]}
    if below:
        rb = plan_rollback(c, "SILVER", "SILVER", max(below))
        result["undone"]["rollback_errors"] = [x["detail"] for x in rb.errors if x["check"] == "others"
                                               and f"lsn {undone['object_version_lsn']} " in x["detail"]]
result["ok"] = (not result["own_rollback_errors"] and "undone" in result
                and result["undone"].get("rollback_errors") == [])
print(json.dumps(result, indent=1))
