"""`health` (catalog health) and `jobs --check` (job steps against the deployed catalog)."""
import json

from ssisbliss.agent.checks import same_server, step_problems
from ssisbliss.ssis import cli
from ssisbliss.ssis.fake import FakeCatalog, dtsx, ispac
from ssisbliss.ssis.health import judge

SERVER = "LABSQL\\SQLDEV"
PKG_PARAM = ('<DTS:PackageParameters><DTS:PackageParameter DTS:ObjectName="BatchSize" /></DTS:PackageParameters>'
             '<DTS:ConnectionManagers><DTS:ConnectionManager DTS:ObjectName="Files" /></DTS:ConnectionManagers>')


def step(job, step_id, command, subsystem="SSIS", on_success=1, on_success_step=0):
    return {"job": job, "step_id": step_id, "step_name": f"step {step_id}", "subsystem": subsystem,
            "command": command, "database_name": None, "on_success_action": on_success,
            "on_success_step_id": on_success_step, "on_fail_action": 2, "on_fail_step_id": 0, "proxy": None}


def ssis(package, server=SERVER, env_ref=None, pars=()):
    cmd = f'/ISSERVER "\\"\\SSISDB\\STAGE\\STAGE\\{package}\\"" /SERVER "\\"{server}\\""'
    if env_ref is not None:
        cmd += f" /ENVREFERENCE {env_ref}"
    for name in pars:
        cmd += f' /Par "\\"{name}\\"";X'
    return cmd + " /CALLERINFO SQLAGENT /REPORTING E"


def catalog():
    c = FakeCatalog(SERVER)
    c.add_project("STAGE", "STAGE",
                  ispac("STAGE", {"Main.dtsx": dtsx("Main.dtsx", PKG_PARAM), "Stage_Customer.dtsx": dtsx("Stage_Customer.dtsx")},
                        parameters=["TargetDb"], connection_managers=["Warehouse"]),
                  env_refs=[{"reference_id": 3, "reference_type": "R", "environment_folder_name": None,
                             "environment_name": "prod"}])
    return c


def problems(steps):
    return step_problems(steps, catalog(), SERVER)


def test_a_step_that_matches_the_catalog_has_no_problems():
    good = ssis("Main.dtsx", env_ref=3, pars=["$Project::TargetDb", "$Package::BatchSize",
                                              "$Project::CM.Warehouse.ConnectionString",
                                              "$Package::CM.Files.ConnectionString",
                                              "$ServerOption::LOGGING_LEVEL(Int16)"])
    assert problems([step("1 IMPORT", 1, good)]) == {}


def test_a_renamed_package_fails_the_next_run_and_names_the_deployed_one():
    found = problems([step("2 STAGE", 1, ssis("StageCustomer.dtsx"))])["2 STAGE"]
    assert len(found) == 1 and found[0]["severity"] == "error"
    assert "StageCustomer.dtsx, which is not deployed" in found[0]["problem"]
    assert "did you mean: Stage_Customer.dtsx" in found[0]["problem"]


def test_package_names_ignore_case_like_the_catalog():
    assert problems([step("j", 1, ssis("main.dtsx"))]) == {}


def test_a_missing_parameter_environment_reference_folder_and_jump_are_each_found():
    found = problems([
        step("j", 1, ssis("Main.dtsx", env_ref=7, pars=["$Project::TargetDB2", "$Package::Batch",
                                                         "$Project::CM.Files.ConnectionString"]), on_success=4,
             on_success_step=9),
        step("k", 1, ssis("Main.dtsx").replace("\\STAGE\\STAGE\\", "\\GOLD\\GOLD\\")),
    ])
    text = [p["problem"] for p in found["j"]]
    assert any("goes to step 9, which does not exist" in t for t in text)
    assert any("environment reference 7" in t and "it has 3 (.\\prod)" in t for t in text)
    assert any("the project has no parameter 'TargetDB2'" in t and "TargetDb" in t for t in text)
    assert any("Main.dtsx has no parameter 'Batch'" in t and "BatchSize" in t for t in text)
    assert any("the project has no connection manager 'Files'" in t for t in text)   # Files is the package's own
    assert "no folder 'GOLD'" in found["k"][0]["problem"]


def test_another_server_is_a_note_not_an_error():
    found = problems([step("j", 1, ssis("Gone.dtsx", server="OTHER\\PROD"))])["j"]
    assert found[0]["severity"] == "note" and "not checked" in found[0]["problem"]
    assert same_server("localhost\\SQLDEV", SERVER) and same_server(".\\SQLDEV", SERVER)
    assert not same_server("localhost", SERVER)


def test_jobs_check_command(tmp_path, capsys):
    c = catalog()
    c.save(tmp_path)
    (tmp_path / "msdb.json").write_text(json.dumps({
        "server_name": SERVER, "history": [],
        "jobs": [{"name": n, "enabled": True, "category": None, "description": None, "owner": "sa", "running": False,
                  "running_since": None, "last_executed_step_id": None, "next_run": None, "last_start": None,
                  "last_status": None, "last_duration": None, "last_message": None, "enabled_schedules": 1}
                 for n in ("1 IMPORT", "2 STAGE")],
        "steps": [step("1 IMPORT", 1, ssis("Main.dtsx")), step("2 STAGE", 1, ssis("Stage_Cust.dtsx"))]}))
    assert cli.main(["jobs", "--server", f"fake://{tmp_path}", "--check", "--problems", "--brief"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["checked"].startswith("1 step problem")
    assert [j["name"] for j in doc["jobs"]] == ["2 STAGE"] and "steps" not in doc["jobs"][0]


def checks(facts, job=None, **kw):
    base = FakeCatalog().health_facts()
    base.update(facts)
    healthy_job = {"enabled": True, "enabled_schedules": 1, "last_status": 1}
    return {f["check"]: f for f in judge(base, healthy_job if job is None else job, **kw)}


def test_a_default_catalog_is_healthy():
    found = checks({})
    assert all(f["severity"] == "info" for f in found.values()) and set(found) == {"retention"}


def test_health_finds_what_breaks_loads():
    found = checks({"properties": {"RETENTION_WINDOW": "365", "OPERATION_CLEANUP_ENABLED": "False",
                                   "SERVER_LOGGING_LEVEL": "0"},
                    "data_mb": 40 * 1024, "log_mb": 60 * 1024, "recovery_model": "FULL",
                    "log_reuse_wait": "LOG_BACKUP", "startup_procedure": False,
                    "open_executions": [{"execution_id": 81, "folder_name": "STAGE", "project_name": "STAGE",
                                         "package_name": "Main.dtsx", "status": 2, "minutes": 3000}]})
    assert {"logging_level", "size", "log", "cleanup", "stuck_execution", "startup_procedure"} <= set(found)
    assert "records no error" in found["logging_level"]["message"]
    assert "RETENTION_WINDOW', @property_value = 60" in found["size"]["fix"]
    assert found["stuck_execution"]["fix"].endswith("@operation_id = 81")
    assert "retention" not in found   # the size warning already says it


def test_health_judges_the_cleanup_job():
    assert "disabled" in checks({}, job={"enabled": False})["cleanup_job"]["message"]
    assert "failed on its last run" in checks({}, job={"enabled": True, "enabled_schedules": 1, "last_status": 0,
                                                       "last_message": "timeout"})["cleanup_job"]["message"]
    found = judge(FakeCatalog().health_facts() | {"oldest_operation_days": 400}, None)
    assert {f["check"] for f in found} >= {"cleanup_job", "cleanup_lag"}
    assert "cleanup_job" not in {f["check"] for f in judge(FakeCatalog().health_facts(), False)}   # msdb not readable


def test_health_command(tmp_path, capsys):
    c = FakeCatalog()
    c.health = {"properties": {"SERVER_LOGGING_LEVEL": "3"}}
    c.save(tmp_path)
    assert cli.main(["health", "--server", f"fake://{tmp_path}"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "1 warning(s)" and doc["findings"][0]["check"] == "logging_level"
    assert doc["catalog"]["cleanup_job"] == "not readable" and doc["catalog"]["logging_level"] == "3"
