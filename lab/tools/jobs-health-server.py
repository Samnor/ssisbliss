"""`jobs --check` and `health` against the real SQL Server. A scratch job gets steps a deploy would break
(a renamed package, a /Par for a parameter that is gone, an environment reference the project lacks, a
jump to a missing step); each must be found, read through msdb and catalog.get_project. Health reads the
real catalog, and finds logging level None while it is set (then puts the level back)."""
import json

from ssisbliss.agent.checks import step_problems
from ssisbliss.agent.source import SqlAgent
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.health import CLEANUP_JOB, judge

SERVER, JOB = "localhost,14331", "LABCHECK broken steps"
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
agent = SqlAgent(SERVER, None, True)
name = agent.server_name()


def run(sql: str, *params) -> None:
    agent.conn.cursor().execute(sql, *params)


def ssis(package: str, extra: str = "") -> str:
    return (f'/ISSERVER "\\"\\SSISDB\\SILVER\\SILVER\\{package}\\"" /SERVER "\\"{name}\\"" {extra}'
            '/CALLERINFO SQLAGENT /REPORTING E')


run("IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = ?) EXEC msdb.dbo.sp_delete_job @job_name = ?", JOB, JOB)
run("EXEC msdb.dbo.sp_add_job @job_name = ?, @enabled = 0", JOB)
for step_name, command, success, target in (
        ("renamed package", ssis("Main_Old.dtsx"), 3, 0),
        ("gone parameter", ssis("Main.dtsx", '/ENVREFERENCE 999 /Par "\\"$Project::NoSuchParam\\"";1 '), 4, 7)):
    run("EXEC msdb.dbo.sp_add_jobstep @job_name = ?, @step_name = ?, @subsystem = N'SSIS', @command = ?, "
        "@on_success_action = ?, @on_success_step_id = ?", JOB, step_name, command, success, target)
try:
    found = step_problems(agent.steps(), c, name)
finally:
    run("EXEC msdb.dbo.sp_delete_job @job_name = ?", JOB)
mine = [p["problem"] for p in found.get(JOB, [])]

level = c._rows("SELECT property_value FROM catalog.catalog_properties WHERE property_name = 'SERVER_LOGGING_LEVEL'")[0]
c.conn.cursor().execute("EXEC catalog.configure_catalog @property_name = N'SERVER_LOGGING_LEVEL', @property_value = 0")
try:
    facts_none = c.health_facts()
finally:
    c.conn.cursor().execute("EXEC catalog.configure_catalog @property_name = N'SERVER_LOGGING_LEVEL', "
                            "@property_value = ?", int(level["property_value"]))
facts = c.health_facts()
cleanup_job = next((j for j in agent.jobs() if j["name"] == CLEANUP_JOB), None)
result = {
    "server": name,
    "scratch_job": mine,
    "other_jobs": {j: [p["problem"] for p in ps] for j, ps in found.items() if j != JOB},
    "facts": {k: v for k, v in facts.items() if k != "open_executions"} | {"open_executions": len(facts["open_executions"])},
    "cleanup_job": bool(cleanup_job),
    "findings": judge(facts, cleanup_job),
    "findings_with_level_none": [f["check"] for f in judge(facts_none, cleanup_job)],
}
result["ok"] = (any("Main_Old.dtsx, which is not deployed" in p for p in mine)
                and any("no parameter 'NoSuchParam'" in p for p in mine)
                and any("environment reference 999" in p for p in mine)
                and any("goes to step 7" in p for p in mine)
                and all(k in facts for k in ("properties", "data_mb", "operations", "open_executions",
                                             "startup_procedure"))
                and "logging_level" in result["findings_with_level_none"]
                and facts["properties"]["SERVER_LOGGING_LEVEL"] == level["property_value"])
print(json.dumps(result, indent=1, default=str))
