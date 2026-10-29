"""Troubleshooting scenarios: the nightly chain on prod did not finish; find out why.

Each world adds to prod an Agent job chain (fake msdb), an IMPORTAPI project built from the
lab's real packages (tests/fixtures), and execution history. The agent may only read; the
outcome is graded on whether its explanation names the actual cause.
"""
from __future__ import annotations

import json
from pathlib import Path

from ssisbliss.ssis.fake import ispac
from world import World

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
NIGHT, YESTERDAY = "2026-10-29T02:", "2026-10-28T02:"
SSIS = ('/ISSERVER "\\"\\SSISDB\\{folder}\\{project}\\{package}\\"" /SERVER "\\"LABSQL\\PROD\\"" /ENVREFERENCE 2 '
        '/Par "\\"$ServerOption::LOGGING_LEVEL(Int16)\\"";{logging} /Par "\\"$ServerOption::SYNCHRONIZED(Boolean)\\"";True '
        '{extra}/CALLERINFO SQLAGENT /REPORTING E')
CHAIN = [("1 IMPORT_API", "BRONZE_API", "IMPORTAPI", "Fetch_Prices.dtsx"),
         ("2 LOAD_PRICES", "BRONZE_API", "IMPORTAPI", "Load_Prices.dtsx"),
         ("3 STAGE", "STAGE", "STAGE", "Main.dtsx"),
         ("4 GOLD", None, None, None)]
PROMPT = ("The nightly load on prod didn't finish. Find out what happened and tell me what needs fixing. "
          "Environments are in ./ssisbliss.toml.")


def _job(name, start=None, status=1, duration=300, running=False, since=None, message=None, enabled=True):
    return {"name": name, "enabled": enabled, "category": "Data Collector" if name.startswith("DBA") else
            "[Uncategorized (Local)]", "description": None, "owner": "sa", "running": running,
            "running_since": since, "last_executed_step_id": 1 if running else None, "next_run": None,
            "last_start": start, "last_status": status if start else None, "last_duration": duration if start else None,
            "last_message": message or ("The job succeeded." if status == 1 else "The job failed."),
            "enabled_schedules": 1 if name.startswith("1 ") or name.startswith("DBA") else 0}


def _step(job, step_id, name, subsystem, command, on_success=3):
    return {"job": job, "step_id": step_id, "step_name": name, "subsystem": subsystem, "command": command,
            "database_name": "master", "on_success_action": on_success, "on_success_step_id": 0,
            "on_fail_action": 2, "on_fail_step_id": 0, "proxy": None}


def _hist(job, run_id, steps, outcome_status, start):
    rows, iid = [], run_id * 10
    for i, (name, status, message) in enumerate(steps, start=1):
        rows.append({"job": job, "run_id": run_id, "instance_id": iid + i, "step_id": i, "step_name": name,
                     "run_status": status, "run_duration": 300, "start_time": start, "message": message,
                     "retries_attempted": 0})
    rows.append({"job": job, "run_id": run_id, "instance_id": iid + 9, "step_id": 0, "step_name": "(Job outcome)",
                 "run_status": outcome_status, "run_duration": 300, "start_time": start,
                 "message": f"The job {'succeeded' if outcome_status == 1 else 'failed'}. The Job was invoked by "
                            f"Schedule 1 (Nightly). The last step to run was step {len(steps)} ({steps[-1][0]})."})
    return rows


def _ssis_ok(eid):
    return (f"Executed as user: LAB\\svc_agent. Microsoft (R) SQL Server Execute Package Utility Version 15.0.4470.1 "
            f"for 64-bit Copyright (C) 2019 Microsoft. All rights reserved. Started: 2:00:01 AM Execution ID: {eid}. "
            f"Finished: 2:05:01 AM Elapsed: 300 seconds. The step succeeded.")


def _ssis_failed(eid):
    return (f"Executed as user: LAB\\svc_agent. Microsoft (R) SQL Server Execute Package Utility Version 15.0.4470.1 "
            f"for 64-bit Copyright (C) 2019 Microsoft. All rights reserved. Started: 2:05:01 AM Package execution on "
            f"IS Server failed. Execution ID: {eid}, Execution Status:4. To view the details for the execution, "
            f"right-click on the Integration Services Catalog, and open the [All Executions] report. The package "
            f"execution failed. The step failed.")


def _not_found(pkg):
    return (f"Executed as user: LAB\\svc_agent. Microsoft (R) SQL Server Execute Package Utility Version 15.0.4470.1 "
            f"for 64-bit Copyright (C) 2019 Microsoft. All rights reserved. Started: 2:05:01 AM Failed to execute IS "
            f"server package because of error 0x80131904. Server: LABSQL\\PROD, Package path: "
            f"\\SSISDB\\BRONZE_API\\IMPORTAPI\\{pkg}, Environment reference Id: 2.  Description: Cannot access the "
            f"package or the package does not exist. Verify that the package exists and that the user has permissions "
            f"to it. Source: .Net SqlClient Data Provider Started: 2:05:01 AM Finished: 2:05:02 AM Elapsed: 0.25 "
            f"seconds. The package execution failed. The step failed.")


def build(root: Path, case: str) -> World:
    w = World.build(root)
    prod = w.server("prod")
    prod.add_project("BRONZE_API", "IMPORTAPI", ispac("IMPORTAPI", {
        (f"{n}.dtsx" if not (case == "renamed" and n == "Load_Prices") else "LoadPrices.dtsx"):
        (FIXTURES / f"{n}.dtsx").read_bytes() for n in ("Fetch_Prices", "Archive_Files", "Load_Prices")},
        parameters=["TargetServer", "TargetDb", "LoadMode"]))

    states = {"1 IMPORT_API": "ok", "2 LOAD_PRICES": "ok", "3 STAGE": "ok", "4 GOLD": "ok"}
    logging, override, quits_early = 1, "", False
    if case == "readonly":
        states.update({"2 LOAD_PRICES": "failed", "3 STAGE": "old", "4 GOLD": "old"})
        override = '/Par "\\"$Project::TargetDb\\"";GOLD_ARCHIVE '
    elif case == "python":
        states.update({"1 IMPORT_API": "failed", "2 LOAD_PRICES": "old", "3 STAGE": "old", "4 GOLD": "old"})
    elif case == "quits":
        states.update({"3 STAGE": "old", "4 GOLD": "old"})
        quits_early = True
    elif case == "nolog":
        states.update({"1 IMPORT_API": "failed", "2 LOAD_PRICES": "old", "3 STAGE": "old", "4 GOLD": "old"})
        logging = 0
    elif case == "running":
        states.update({"3 STAGE": "running", "4 GOLD": "old"})
    elif case == "renamed":     # a deploy renamed Load_Prices.dtsx; the job step still names the old package
        states.update({"2 LOAD_PRICES": "failed", "3 STAGE": "old", "4 GOLD": "old"})
    elif case == "child":       # Main runs Stage_Customer through an Execute Package task; the child fails
        states.update({"3 STAGE": "failed", "4 GOLD": "old"})
    # "earlier": tonight was fine; the night before, 1 IMPORT_API failed (the Python API task)

    jobs, steps, history, execs = [], [], [], []
    for i, (name, folder, project, pkg) in enumerate(CHAIN):
        eid = 60 + i
        start = f"{NIGHT}{i * 5:02d}:01"
        if folder:
            extra = override if name == "2 LOAD_PRICES" else ""
            steps.append(_step(name, 1, f"Run {pkg[:-5]}", "SSIS",
                               SSIS.format(folder=folder, project=project, package=pkg,
                                           logging=logging if name == "1 IMPORT_API" else 1, extra=extra),
                               on_success=1 if (quits_early and name == "2 LOAD_PRICES") else 3))
        else:
            steps.append(_step(name, 1, "Load GOLD", "TSQL", "EXEC dbo.LoadGold;", on_success=3))
        if i + 1 < len(CHAIN):
            steps.append(_step(name, 2, "Start next", "TSQL",
                               f"EXEC msdb.dbo.sp_start_job @job_name = N'{CHAIN[i + 1][0]}';", on_success=1))
        st = states[name]
        yesterday = f"{YESTERDAY}{i * 5:02d}:01"
        if st == "running":
            jobs.append(_job(name, yesterday, running=True, since=start))
        elif st == "old":
            jobs.append(_job(name, yesterday))
        else:
            jobs.append(_job(name, start, status=0 if st == "failed" else 1))
        # History: yesterday's good run, then tonight's (if it ran).
        ok_steps = [(f"Run {pkg[:-5]}" if folder else "Load GOLD", 1, _ssis_ok(eid - 20) if folder else "ok")]
        if i + 1 < len(CHAIN):
            ok_steps.append(("Start next", 1, f"Job '{CHAIN[i + 1][0]}' started successfully."))
        if case == "earlier" and i == 0:
            history += _hist(name, 100, [(f"Run {pkg[:-5]}", 0, _ssis_failed(40))], 0, yesterday)
            execs.append((40, folder, project, pkg, yesterday))
        elif not (case == "earlier" and i > 0):
            history += _hist(name, 100 + i, ok_steps, 1, yesterday)
        if st == "failed" and case == "renamed":
            history += _hist(name, 200 + i, [(f"Run {pkg[:-5]}", 0, _not_found(pkg))], 0, start)
        elif st == "failed":
            history += _hist(name, 200 + i, [(f"Run {pkg[:-5]}", 0, _ssis_failed(eid))], 0, start)
            execs.append((eid, folder, project, pkg, start))
        elif st == "ok":
            tonight = [(ok_steps[0][0], 1, _ssis_ok(eid) if folder else "ok")]
            if i + 1 < len(CHAIN) and not (quits_early and name == "2 LOAD_PRICES"):
                tonight.append(ok_steps[1])
            history += _hist(name, 200 + i, tonight, 1, start)
    for name in ("DBA Backup", "syspolicy_purge_history"):
        jobs.append(_job(name, f"{NIGHT}30:00"))
        steps.append(_step(name, 1, "Run", "TSQL", "EXEC dbo.DatabaseBackup @Databases = 'USER_DATABASES';"))
        history += _hist(name, 300 + len(jobs), [("Run", 1, "ok")], 1, f"{NIGHT}30:00")
    (w.server_dir("prod") / "msdb.json").write_text(json.dumps(
        {"server_name": "LABSQL\\PROD", "jobs": jobs, "steps": steps, "history": history}, indent=1))

    prod = w.server("prod")
    base = {"status": 4, "environment_folder_name": None, "environment_name": "Default",
            "caller_name": "LAB\\svc_agent", "executed_as_name": "LAB\\svc_agent", "duration_s": 4,
            "server_name": "LABSQL\\PROD", "machine_name": "LABPROD01"}
    for eid, folder, project, pkg, start in execs:
        prod.execution_rows.append({**base, "execution_id": eid, "folder_name": folder, "project_name": project,
                                    "package_name": pkg, "start_time": f"{start}.0000000+00:00",
                                    "end_time": f"{start[:-2]}05.0000000+00:00"})
    ev = {"event_message_id": 1, "message_time": "", "message_type": 120, "message_source_type": 40,
          "subcomponent_name": None, "execution_path": ""}
    if case == "readonly":
        prod.event_rows[61] = [{**ev, "message_source_name": "Load", "package_name": "Load_Prices.dtsx",
                                "package_path": "\\Package\\Load",
                                "message": 'Load:Error: Executing the query "INSERT dbo.LoadLog (Package, RowsLoaded) '
                                           'SELECT N\'..." failed with the following error: "Failed to update database '
                                           '"GOLD_ARCHIVE" because the database is read-only.". Possible failure reasons: '
                                           'Problems with the query, "ResultSet" property not set correctly, parameters '
                                           'not set correctly, or connection not established correctly.'},
                               {**ev, "message_type": 130, "message_source_name": "Load",
                                "package_name": "Load_Prices.dtsx", "package_path": "\\Package\\Load",
                                "message": "Load:The task, 'Load', failed."}]
        prod.parameter_rows[61] = [
            {"object_type": 20, "parameter_name": "TargetDb", "parameter_value": "GOLD_ARCHIVE", "sensitive": False,
             "runtime_override": True},
            {"object_type": 20, "parameter_name": "TargetServer", "parameter_value": "LABSQL\\PROD", "sensitive": False,
             "runtime_override": False}]
    elif case == "python":
        prod.event_rows[60] = [{**ev, "message_source_name": "Call price API", "package_name": "Fetch_Prices.dtsx",
                                "package_path": "\\Package\\Call price API",
                                "message": 'Call price API:Error: In Executing "C:\\Python312\\python.exe" '
                                           '"C:\\lab\\scripts\\fetch_prices.py" at "C:\\lab\\scripts", The process exit '
                                           'code was "2" while the expected was "0".'}]
    elif case == "earlier":
        prod.event_rows[40] = [{**ev, "message_source_name": "Call price API", "package_name": "Fetch_Prices.dtsx",
                                "package_path": "\\Package\\Call price API",
                                "message": 'Call price API:Error: In Executing "C:\\Python312\\python.exe" '
                                           '"C:\\lab\\scripts\\fetch_prices.py" at "C:\\lab\\scripts", The process exit '
                                           'code was "2" while the expected was "0".'}]
    elif case == "child":
        prod.event_rows[62] = [
            {**ev, "message_source_name": "Data Flow Task", "package_name": "Stage_Customer.dtsx",
             "package_path": "\\Package\\Data Flow Task", "subcomponent_name": "OLE DB Destination [2]",
             "message": 'Data Flow Task:Error: There was an error with OLE DB Destination.Inputs[OLE DB Destination '
                        'Input].Columns[CustomerName] on OLE DB Destination.Inputs[OLE DB Destination Input]. The column '
                        'status returned was: "The value could not be converted because of a potential loss of data.". '
                        'Text was truncated or one or more characters had no match in the target code page.'},
            {**ev, "message_type": 130, "message_source_name": "Data Flow Task", "package_name": "Stage_Customer.dtsx",
             "package_path": "\\Package\\Data Flow Task", "message": "Data Flow Task:The task, 'Data Flow Task', failed."},
            {**ev, "message_source_name": "Execute Stage_Customer", "package_name": "Main.dtsx",
             "package_path": "\\Package\\Execute Stage_Customer",
             "message": "Execute Stage_Customer:Error: Error 0xC0202009 while preparing to load the package. The child "
                        "package failed."}]
    # "nolog": logging level None records neither messages nor executable statistics.
    prod.save()
    w.snapshot()
    return w


def grades():
    """(name, case, required groups: each group is satisfied by any one of its words, trap)."""
    return [
        ("chain-readonly-target", "readonly",
         [("GOLD_ARCHIVE",), ("read-only", "read only", "readonly"), ("override", "/Par", "job step", "step command")],
         "a job step points TargetDb at a read-only history database"),
        ("chain-python-api", "python",
         [("fetch_prices",), ("exit code", "exit 2", "returned 2", "code 2")],
         "the Python API task failed; SSIS logged only its exit code"),
        ("chain-start-next-skipped", "quits",
         [("2 LOAD_PRICES", "LOAD_PRICES"), ("Start next", "step 2"),
          ("quit", "on success", "on_success", "never ran", "did not run", "didn't run", "not run", "skipped")],
         "job 2 quits after step 1, so its Start next step never runs; nothing failed"),
        ("chain-no-logging", "nolog",
         [("1 IMPORT_API", "IMPORT_API", "Fetch_Prices"), ("logging",)],
         "the failed execution ran with logging level None; no details were recorded"),
        ("chain-still-running", "running",
         [("running", "in progress", "still executing"), ("3 STAGE",)],
         "nothing failed: 3 STAGE is still running"),
    ]
