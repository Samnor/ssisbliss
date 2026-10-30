"""SQL Agent views: step parsing, chain status and run history (msdb rows as the lab returns them)."""
import json

import pytest

from ssisbliss.agent import cli
from ssisbliss.agent.status import (
    chains,
    job_views,
    parse_ssis_command,
    runs,
    started_jobs,
)

# The command the lab seed (and SSMS) writes for a catalog package step.
SSIS_STEP = ('/ISSERVER "\\"\\SSISDB\\STAGE\\STAGE\\Main.dtsx\\"" /SERVER "\\"LABHOST\\SQLPROD\\"" /ENVREFERENCE 3 '
             '/Par "\\"$Project::TargetDb\\"";GOLD_ARCHIVE /Par "\\"$ServerOption::LOGGING_LEVEL(Int16)\\"";1 '
             '/Par "\\"$ServerOption::SYNCHRONIZED(Boolean)\\"";True /CALLERINFO SQLAGENT /REPORTING E')


def test_parse_catalog_package_step():
    assert parse_ssis_command(SSIS_STEP) == {
        "source": "catalog", "folder": "STAGE", "project": "STAGE", "package": "Main.dtsx",
        "server": "LABHOST\\SQLPROD", "environment_reference": 3, "parameters": {"$Project::TargetDb": "GOLD_ARCHIVE"},
        "server_options": {"LOGGING_LEVEL": "1", "SYNCHRONIZED": "True"}}


def test_parse_file_and_msdb_package_steps():
    assert parse_ssis_command('/FILE "\\"D:\\ssis\\Load.dtsx\\"" /CHECKPOINTING OFF')["file"] == "D:\\ssis\\Load.dtsx"
    assert parse_ssis_command('/SQL "\\"\\Maintenance\\Cleanup\\"" /SERVER "\\"(local)\\""')["source"] == "msdb"


def test_started_jobs():
    assert started_jobs("EXEC msdb.dbo.sp_start_job @job_name = N'2 IMPORT';") == ["2 IMPORT"]
    assert started_jobs("exec sp_start_job 'Next one'") == ["Next one"]
    assert started_jobs("SELECT 1") == []


def job(name, start=None, status=1, running=False, since=None, enabled=True):
    return {"name": name, "enabled": enabled, "category": "[Uncategorized (Local)]", "description": None,
            "owner": "sa", "running": running, "running_since": since, "last_executed_step_id": None,
            "next_run": None, "last_start": start, "last_status": status if start else None,
            "last_duration": 5 if start else None, "last_message": "The job failed." if status == 0 else "ok",
            "enabled_schedules": 0}


def step(job_name, step_id, command, subsystem="TSQL"):
    return {"job": job_name, "step_id": step_id, "step_name": f"step {step_id}", "subsystem": subsystem,
            "command": command, "database_name": "master", "on_success_action": 3, "on_success_step_id": 0,
            "on_fail_action": 2, "on_fail_step_id": 0, "proxy": None}


def chain_of(jobs):
    names = [j["name"] for j in jobs]
    steps = []
    for i, n in enumerate(names):
        steps.append(step(n, 1, SSIS_STEP, "SSIS"))
        if i + 1 < len(names):
            steps.append(step(n, 2, f"EXEC msdb.dbo.sp_start_job @job_name = N'{names[i + 1]}';"))
    return chains(job_views(jobs, steps))


def test_chain_completed():
    [c] = chain_of([job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-29T07:00:05"),
                    job("3 C", "2026-10-29T07:00:09")])
    assert c["verdict"] == "completed" and [link["job"] for link in c["links"]] == ["1 A", "2 B", "3 C"]
    assert c["links"][0]["runs"] == ["STAGE/STAGE/Main.dtsx"]


def test_chain_failed_link_is_named_with_its_message():
    [c] = chain_of([job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-29T07:00:05", status=0),
                    job("3 C", "2026-10-28T07:00:09")])
    assert c["verdict"] == "failed at 2 B"
    assert c["links"][1]["message"] == "The job failed."
    assert c["links"][2]["state"] == "not_started"   # its last run was yesterday's


def test_chain_stopped_between_links():
    [c] = chain_of([job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-28T07:00:05")])
    assert c["verdict"] == "stopped after 1 A: 2 B did not start"


def test_chain_running():
    [c] = chain_of([job("1 A", "2026-10-29T07:00:00"),
                    job("2 B", "2026-10-28T07:00:05", running=True, since="2026-10-29T07:00:05")])
    assert c["verdict"] == "running: 2 B"
    assert c["links"][1]["last_run_took_s"] == 5
    assert "nothing in this chain has failed" in c["why"] and "last run took 5 s" in c["why"]


def test_chain_stopped_by_a_step_that_quits_on_success():
    jobs = [job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-28T07:00:05")]
    steps = [{**step("1 A", 1, SSIS_STEP, "SSIS"), "on_success_action": 1},
             step("1 A", 2, "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';")]
    views = job_views(jobs, steps)
    assert views[0]["steps"][0]["on_success"] == "quit reporting success"
    assert views[0]["steps"][1]["on_failure"] == "quit reporting failure"
    [skipped] = views[0]["skipped_starts"]
    assert skipped["step_id"] == 2 and skipped["would_start"] == ["2 B"]
    [c] = chains(views)
    assert c["verdict"] == "stopped after 1 A: 2 B did not start"
    assert "step 1 (step 1) is set to 'quit reporting success'" in c["why"]
    assert "the whole cause" in c["why"] and "so that step 2 (step 2) runs" in c["why"]
    assert "next" not in c


def test_go_to_step_actions_are_followed():
    steps = [{**step("1 A", 1, "SELECT 1"), "on_success_action": 4, "on_success_step_id": 3},
             step("1 A", 2, "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';"),
             step("1 A", 3, "EXEC msdb.dbo.sp_start_job @job_name = N'3 C';")]
    [v] = job_views([job("1 A", "2026-10-29T07:00:00")], steps)
    assert v["steps"][0]["on_success"] == "go to step 3"
    assert [s["would_start"] for s in v["skipped_starts"]] == [["2 B"]]
    assert "skipped_starts" not in job_views([job("2 B")], [step("2 B", 1, SSIS_STEP, "SSIS")])[0]


def test_chain_reports_a_missing_next_job():
    jobs = [job("1 A", "2026-10-29T07:00:00")]
    steps = [step("1 A", 1, "EXEC msdb.dbo.sp_start_job @job_name = N'2 Renamed';")]
    [c] = chains(job_views(jobs, steps))
    assert c["missing_jobs"] == ["2 Renamed"]


HISTORY = [
    {"run_id": 20, "instance_id": 19, "step_id": 1, "step_name": "Run STAGE Main", "run_status": 0,
     "run_duration": 5, "start_time": "2026-10-29T07:17:44", "retries_attempted": 0,
     "message": "Executed as user: X. Microsoft (R) SQL Server Execute Package Utility  Version 15.0 "
                "All rights reserved.  Package execution on IS Server failed. Execution ID: 69, Execution Status:4."},
    {"run_id": 20, "instance_id": 20, "step_id": 0, "step_name": "(Job outcome)", "run_status": 0, "run_duration": 5,
     "start_time": "2026-10-29T07:17:44", "retries_attempted": 0, "message": "The job failed."},
    {"run_id": 10, "instance_id": 9, "step_id": 1, "step_name": "Run STAGE Main", "run_status": 1,
     "run_duration": 102, "start_time": "2026-10-28T07:00:00", "retries_attempted": 0,
     "message": "Executed as user: X. Execution ID: 40."},
    {"run_id": 10, "instance_id": 10, "step_id": 0, "step_name": "(Job outcome)", "run_status": 1,
     "run_duration": 102, "start_time": "2026-10-28T07:00:00", "retries_attempted": 0, "message": "The job succeeded."},
]


def test_history_runs_link_failed_ssis_steps_to_their_execution():
    r = runs(HISTORY)
    assert [x["outcome"] for x in r] == ["failed", "succeeded"]
    assert r[1]["duration_s"] == 62   # run_duration is HHMMSS
    failed = r[0]["steps"][0]
    assert failed["ssis_execution_id"] == 69 and failed["next"] == "ssisbliss executions --id 69"
    assert "Execute Package Utility" not in failed["message"]
    assert "next" not in r[1]["steps"][0]


def test_cli_with_fake_msdb(tmp_path, capsys):
    jobs = [job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-29T07:00:05", status=0)]
    steps = [step("1 A", 1, "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';"), step("2 B", 1, SSIS_STEP, "SSIS")]
    state = tmp_path / "msdb.json"
    state.write_text(json.dumps({"server_name": "LAB\\SQL01", "jobs": jobs, "steps": steps,
                                 "history": [{**h, "job": "2 B"} for h in HISTORY]}))
    srv = ["--server", f"fake://{state}"]
    assert cli.main(["chain", *srv]) == 0
    [c] = json.loads(capsys.readouterr().out)["chains"]
    assert c["verdict"] == "failed at 2 B" and c["next"] == f'ssisbliss history --server "fake://{state}" "2 B" --runs 1'
    assert cli.main(["jobs", *srv, "--failed", "--brief"]) == 0
    [failed] = json.loads(capsys.readouterr().out)["jobs"]
    assert failed["name"] == "2 B" and failed["next"] == 'ssisbliss history "2 B" --runs 1'
    assert cli.main(["history", *srv, "2 B", "--runs", "1"]) == 0
    assert len(json.loads(capsys.readouterr().out)["runs"]) == 1
    assert cli.main(["history", *srv, "B"]) == 2
    assert "did you mean: 2 B" in json.loads(capsys.readouterr().out)["error"]


def test_history_shows_a_step_that_a_successful_run_skips(tmp_path, capsys):
    steps = [{**step("1 A", 1, SSIS_STEP, "SSIS"), "on_success_action": 1},
             step("1 A", 2, "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';")]
    state = tmp_path / "msdb.json"
    state.write_text(json.dumps({"server_name": "LAB\\SQL01", "jobs": [job("1 A", "2026-10-29T07:00:00")],
                                 "steps": steps, "history": [{**h, "job": "1 A"} for h in HISTORY]}))
    assert cli.main(["history", "--server", f"fake://{state}", "1 A", "--runs", "1"]) == 0
    [skipped] = json.loads(capsys.readouterr().out)["skipped_starts"]
    assert skipped["name"] == "step 2" and "never runs" in skipped["reason"]


def test_history_names_a_package_the_catalog_cannot_find():
    msg = ("Executed as user: LAB\\svc_agent. Failed to execute IS server package because of error 0x80131904. "
           "Server: LABHOST\\SQLPROD, Package path: \\SSISDB\\BRONZE_API\\IMPORTAPI\\Load_Prices.dtsx, Environment "
           "reference Id: 2.  Description: Cannot access the package or the package does not exist. The step failed.")
    rows = [{"run_id": 1, "step_id": 1, "step_name": "Run", "run_status": 0, "run_duration": 1,
             "start_time": "2026-10-29T02:05:01", "message": msg},
            {"run_id": 1, "step_id": 0, "step_name": "(Job outcome)", "run_status": 0, "run_duration": 1,
             "start_time": "2026-10-29T02:05:01", "message": "The job failed."}]
    step = runs(rows)[0]["steps"][0]
    assert "Load_Prices.dtsx" in step["hint"] and "renamed" in step["hint"]
    assert step["next"] == 'ssisbliss inspect --folder "BRONZE_API" --project "IMPORTAPI"'


def test_troubleshooting_help_points_at_chain_first(capsys):
    from ssisbliss.ssis.cli import parser
    for command in ("jobs", "chain", "history", "executions"):
        with pytest.raises(SystemExit):
            parser().parse_args([command, "--help"])
        text = " ".join(capsys.readouterr().out.split())
        assert "Start with `ssisbliss chain --env ENV`" in text
        assert "an empty list does not mean the load is fine" in text
