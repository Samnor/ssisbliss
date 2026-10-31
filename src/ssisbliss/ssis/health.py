"""`health`: what is wrong with an SSIS catalog before it breaks a load. Pure judgement of the facts
SqlCatalog.health_facts() reads, plus the SSISDB cleanup job from msdb. Read-only: every fix is for the
server's DBA, written as the T-SQL to run.

The usual trouble: SSISDB grows until its disk or log is full (a year of logs, a cleanup job that is
disabled or failing, full recovery without log backups); logging level None, so a failed run records no
error; executions left "running" by a restart.
"""
from __future__ import annotations

from typing import Literal

LOGGING_LEVELS = {"0": "None", "1": "Basic", "2": "Performance", "3": "Verbose", "4": "RuntimeLineage",
                  "100": "Customized"}
CLEANUP_JOB = "SSIS Server Maintenance Job"
BIG_MB = 10 * 1024


def _configure(prop: str, value: str) -> str:
    return f"EXEC SSISDB.catalog.configure_catalog @property_name = N'{prop}', @property_value = {value}"


def judge(facts: dict, cleanup_job: dict | None | Literal[False], stuck_hours: float = 24) -> list[dict]:
    """Findings, most serious first. cleanup_job: the job's msdb row, None when it does not exist, False when
    msdb could not be read."""
    out: list[dict] = []

    def add(severity: str, check: str, message: str, fix: str | None = None) -> None:
        out.append({"severity": severity, "check": check, "message": message, **({"fix": fix} if fix else {})})

    props = facts.get("properties", {})
    retention = int(props["RETENTION_WINDOW"]) if str(props.get("RETENTION_WINDOW", "")).isdigit() else None
    cleanup = str(props.get("OPERATION_CLEANUP_ENABLED", "")).lower()

    level = str(props.get("SERVER_LOGGING_LEVEL", ""))
    if level == "0":
        add("warning", "logging_level", "the default logging level is None: a failed run records no error message, "
            "so executions cannot say why it failed", _configure("SERVER_LOGGING_LEVEL", "1") + "  -- Basic")
    elif level == "3":
        add("warning", "logging_level", "the default logging level is Verbose: every run logs every event, which "
            "fills SSISDB and slows packages", _configure("SERVER_LOGGING_LEVEL", "1") + "  -- Basic")

    data, log = facts.get("data_mb"), facts.get("log_mb")
    if data is not None and data >= BIG_MB:
        add("warning", "size", f"SSISDB data is {data / 1024:.1f} GB"
            + (f" with {retention} days of logs kept" if retention else ""),
            "lower the retention, then let the cleanup job catch up in small steps: "
            + _configure("RETENTION_WINDOW", "60" if (retention or 365) > 60 else "14"))
    if (facts.get("recovery_model") == "FULL" and facts.get("log_reuse_wait") == "LOG_BACKUP"
            and log is not None and log >= max(1024, data or 0)):
        add("warning", "log", f"SSISDB's log is {log / 1024:.1f} GB and waits for a log backup (FULL recovery): it "
            "grows until the disk is full", "back up SSISDB's log on a schedule, or ALTER DATABASE SSISDB SET "
            "RECOVERY SIMPLE if point-in-time restore of SSISDB is not needed")

    if cleanup == "false":
        add("warning", "cleanup", "operation cleanup is off: logs are never deleted and SSISDB keeps growing",
            _configure("OPERATION_CLEANUP_ENABLED", "1"))
    if cleanup_job is None:
        add("warning", "cleanup_job", f"there is no '{CLEANUP_JOB}': nothing deletes old logs or project versions",
            "recreate the catalog's maintenance job (it comes with SSISDB; script it from another server's SSISDB)")
    elif isinstance(cleanup_job, dict):
        if not cleanup_job.get("enabled"):
            add("warning", "cleanup_job", f"'{CLEANUP_JOB}' is disabled: nothing deletes old logs",
                f"EXEC msdb.dbo.sp_update_job @job_name = N'{CLEANUP_JOB}', @enabled = 1")
        elif not cleanup_job.get("enabled_schedules"):
            add("warning", "cleanup_job", f"'{CLEANUP_JOB}' has no enabled schedule: it never runs",
                "enable its daily schedule in SQL Server Agent")
        elif cleanup_job.get("last_status") == 0:
            add("warning", "cleanup_job", f"'{CLEANUP_JOB}' failed on its last run ({cleanup_job.get('last_start')}): "
                f"{cleanup_job.get('last_message') or 'see its history'}",
                f"ssisbliss history --env ENV \"{CLEANUP_JOB}\" --runs 1; a cleanup that times out on a big SSISDB "
                "needs a shorter retention first, lowered a few days at a time")
    oldest = facts.get("oldest_operation_days")
    if retention and oldest is not None and oldest > retention + 3 and cleanup != "false":
        add("warning", "cleanup_lag", f"the oldest operation is {oldest} days old but the retention is {retention} "
            "days: the cleanup is not keeping up", f"check '{CLEANUP_JOB}'s history")

    for e in facts.get("open_executions", []):
        if e.get("minutes") is not None and e["minutes"] >= stuck_hours * 60:
            state = "running" if e.get("status") == 2 else "pending"
            add("warning", "stuck_execution", f"execution {e['execution_id']} ({e['folder_name']}\\"
                f"{e['project_name']}\\{e['package_name']}) has been {state} for {e['minutes'] // 60} hours: "
                "if no job or person is running it, the server restarted under it",
                f"EXEC SSISDB.catalog.stop_operation @operation_id = {e['execution_id']}")
    if facts.get("startup_procedure") is False:
        add("info", "startup_procedure", "sp_ssis_startup does not run at server start: executions cut off by a "
            "restart stay 'running' instead of 'ended unexpectedly'",
            "EXEC sp_procoption N'sp_ssis_startup', 'startup', 'on'  -- in master")

    if retention and retention > 90 and not any(f["check"] == "size" for f in out):
        add("info", "retention", f"logs are kept {retention} days (SSISDB's default is 365): fine while SSISDB is "
            "small, the first thing to lower when it grows")
    order = {"warning": 0, "info": 1}
    return sorted(out, key=lambda f: order[f["severity"]])
