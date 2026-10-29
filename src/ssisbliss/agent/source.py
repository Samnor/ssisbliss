"""Read SQL Server Agent state from msdb. Read-only."""
from __future__ import annotations

from typing import Any, Protocol

from ..sql import connection_string


class AgentSource(Protocol):
    def server_name(self) -> str: ...
    def jobs(self) -> list[dict]: ...
    def steps(self) -> list[dict]: ...
    def history(self, job_name: str, runs: int) -> list[dict]: ...


# run_status in sysjobhistory
OUTCOME = {0: "failed", 1: "succeeded", 2: "retry", 3: "canceled", 4: "in_progress"}


class SqlAgent:
    def __init__(self, server: str, sql_user: str | None = None, trust_server_certificate: bool = False):
        import pyodbc

        self.conn = pyodbc.connect(connection_string(server, sql_user, trust_server_certificate, database="msdb",
                                                     app="onprem-agent"), autocommit=True, timeout=15)

    def _rows(self, sql: str, *params: Any) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def server_name(self) -> str:
        return self._rows("SELECT @@SERVERNAME AS n")[0]["n"]

    def jobs(self) -> list[dict]:
        # Running = an open activity row in the newest Agent session. Last run = newest job outcome row.
        return self._rows("""
            WITH session AS (SELECT MAX(session_id) AS id FROM dbo.syssessions),
            last_run AS (
                SELECT h.job_id, h.run_status, h.message, h.run_duration,
                       dbo.agent_datetime(h.run_date, h.run_time) AS start_time,
                       ROW_NUMBER() OVER (PARTITION BY h.job_id ORDER BY h.instance_id DESC) AS rn
                FROM dbo.sysjobhistory h WHERE h.step_id = 0)
            SELECT j.name, CAST(j.enabled AS bit) AS enabled, c.name AS category, j.description,
                   SUSER_SNAME(j.owner_sid) AS owner,
                   CAST(CASE WHEN a.start_execution_date IS NOT NULL AND a.stop_execution_date IS NULL
                             THEN 1 ELSE 0 END AS bit) AS running,
                   CONVERT(varchar(19), a.start_execution_date, 126) AS running_since,
                   a.last_executed_step_id,
                   CONVERT(varchar(19), a.next_scheduled_run_date, 126) AS next_run,
                   CONVERT(varchar(19), lr.start_time, 126) AS last_start, lr.run_status AS last_status,
                   lr.run_duration AS last_duration, lr.message AS last_message,
                   (SELECT COUNT(*) FROM dbo.sysjobschedules js JOIN dbo.sysschedules s ON s.schedule_id = js.schedule_id
                    WHERE js.job_id = j.job_id AND s.enabled = 1) AS enabled_schedules
            FROM dbo.sysjobs j
            LEFT JOIN dbo.syscategories c ON c.category_id = j.category_id
            LEFT JOIN dbo.sysjobactivity a ON a.job_id = j.job_id AND a.session_id = (SELECT id FROM session)
            LEFT JOIN last_run lr ON lr.job_id = j.job_id AND lr.rn = 1
            ORDER BY j.name""")

    def steps(self) -> list[dict]:
        return self._rows("""SELECT j.name AS job, s.step_id, s.step_name, s.subsystem, s.command, s.database_name,
                                    s.on_success_action, s.on_success_step_id, s.on_fail_action, s.on_fail_step_id,
                                    p.name AS proxy
                             FROM dbo.sysjobsteps s JOIN dbo.sysjobs j ON j.job_id = s.job_id
                             LEFT JOIN dbo.sysproxies p ON p.proxy_id = s.proxy_id
                             ORDER BY j.name, s.step_id""")

    def history(self, job_name: str, runs: int) -> list[dict]:
        # The newest `runs` job outcome rows and every step row of those runs.
        return self._rows("""
            WITH outcomes AS (
                SELECT TOP (?) h.instance_id FROM dbo.sysjobhistory h JOIN dbo.sysjobs j ON j.job_id = h.job_id
                WHERE j.name = ? AND h.step_id = 0 ORDER BY h.instance_id DESC),
            bounds AS (
                SELECT o.instance_id AS hi,
                       ISNULL((SELECT MAX(h2.instance_id) FROM dbo.sysjobhistory h2
                               WHERE h2.job_id = (SELECT job_id FROM dbo.sysjobs WHERE name = ?)
                                 AND h2.step_id = 0 AND h2.instance_id < o.instance_id), 0) AS lo
                FROM outcomes o)
            SELECT b.hi AS run_id, h.instance_id, h.step_id, h.step_name, h.run_status, h.run_duration,
                   CONVERT(varchar(19), dbo.agent_datetime(h.run_date, h.run_time), 126) AS start_time,
                   h.message, h.retries_attempted
            FROM bounds b JOIN dbo.sysjobhistory h ON h.instance_id > b.lo AND h.instance_id <= b.hi
            JOIN dbo.sysjobs j ON j.job_id = h.job_id AND j.name = ?
            ORDER BY b.hi DESC, h.instance_id""", runs, job_name, job_name, job_name)
