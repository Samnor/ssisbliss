"""Access to the SSIS catalog (SSISDB).

`Catalog` is the interface the deploy logic needs; `SqlCatalog` implements it with
pyodbc. Tests use an in-memory fake.

Note: SSISDB refuses deploy_project / deploy_packages / restore_project for SQL
Server logins (error 27123). Reads and plans work with SQL auth; apply needs Windows auth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

# catalog.operations.status
OPERATION_STATUS = {1: "created", 2: "running", 3: "canceled", 4: "failed", 5: "pending",
                    6: "ended_unexpectedly", 7: "succeeded", 8: "stopping", 9: "completed"}


@dataclass
class ServerInfo:
    server_name: str
    major_version: int
    product_version: str
    has_ssisdb: bool
    is_admin: bool


@dataclass
class Project:
    project_id: int
    folder: str
    name: str
    object_version_lsn: int
    deployed_by: str | None
    last_deployed: str | None


class Catalog(Protocol):
    def server_info(self) -> ServerInfo: ...
    def folder_exists(self, folder: str) -> bool: ...
    def project(self, folder: str, project: str) -> Project | None: ...
    def packages(self, project_id: int) -> list[str]: ...
    def can_modify_project(self, project_id: int) -> bool: ...
    def login(self) -> str: ...
    def project_parameters(self, project_id: int) -> list[str]: ...
    def parameter_bindings(self, project_id: int) -> list[dict]: ...
    def environment_references(self, project_id: int) -> list[dict]: ...
    def running_executions(self, folder: str, project: str) -> int: ...
    def versions(self, project_id: int) -> list[dict]: ...
    def get_project(self, folder: str, project: str) -> bytes: ...
    def deploy_packages(self, folder: str, project: str, packages: list[tuple[str, bytes]]) -> int: ...
    def deploy_project(self, folder: str, project: str, ispac: bytes) -> int: ...
    def restore_project(self, folder: str, project: str, lsn: int) -> None: ...
    def operation(self, operation_id: int) -> dict: ...
    def list_projects(self, folder: str | None) -> list[dict]: ...
    def executions(self, folder: str | None = None, project: str | None = None, package: str | None = None,
                   statuses: list[int] | None = None, since_hours: float | None = None, limit: int = 20,
                   execution_id: int | None = None) -> list[dict]: ...
    def execution_events(self, execution_id: int, message_types: tuple[int, ...] = (120, 130)) -> list[dict]: ...
    def failed_executables(self, execution_id: int) -> list[dict]: ...
    def execution_parameters(self, execution_id: int) -> list[dict]: ...


def package_table_name(name: str) -> str:
    """catalog.deploy_packages appends ".dtsx" itself: "X.dtsx" would create a new package "X.dtsx.dtsx"."""
    return name[:-5] if name.lower().endswith(".dtsx") else name


from ..sql import connection_string  # noqa: E402,F401  (re-exported)


class CatalogError(RuntimeError):
    pass


class SqlCatalog:
    def __init__(self, conn_str: str):
        import pyodbc

        # SSISDB connections need a database that exists; fall back to master to report it missing.
        try:
            self.conn = pyodbc.connect(conn_str, autocommit=True, timeout=15)
            self._ssisdb = True
        except pyodbc.Error as e:
            if "SSISDB" not in str(e):
                raise
            self.conn = pyodbc.connect(conn_str.replace("DATABASE=SSISDB", "DATABASE=master"),
                                       autocommit=True, timeout=15)
            self._ssisdb = False

    def _rows(self, sql: str, *params) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _scalar(self, sql: str, *params) -> Any:
        import pyodbc

        cur = self.conn.cursor()
        try:
            cur.execute(sql, *params)
        except pyodbc.Error as e:
            # A failed deployment only says "query operation_messages for operation N": do that.
            cur.close()  # frees the connection, which is otherwise busy with the failed batch
            m = re.search(r"operation identifier '(\d+)'", str(e))
            if not m:
                raise
            msgs = self._rows("SELECT message FROM catalog.operation_messages WHERE operation_id = ? "
                              "AND message_type IN (110, 120, 130) ORDER BY operation_message_id", int(m.group(1)))
            raise CatalogError(f"operation {m.group(1)} failed: " +
                               " | ".join(r["message"] for r in msgs) if msgs else str(e)) from e
        # Procedures emit row counts and messages first; skip to the first result set.
        while cur.description is None and cur.nextset():
            pass
        row = cur.fetchone()
        return row[0] if row else None

    def server_info(self) -> ServerInfo:
        r = self._rows("""SELECT @@SERVERNAME AS server_name,
                                 CAST(SERVERPROPERTY('ProductMajorVersion') AS int) AS major,
                                 CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)) AS version,
                                 CASE WHEN DB_ID('SSISDB') IS NULL THEN 0 ELSE 1 END AS has_ssisdb,
                                 IS_SRVROLEMEMBER('sysadmin') AS sysadmin""")[0]
        admin = bool(r["sysadmin"])
        if r["has_ssisdb"] and self._ssisdb and not admin:
            admin = bool(self._scalar("SELECT IS_MEMBER('ssis_admin')"))
        return ServerInfo(r["server_name"], r["major"], r["version"], bool(r["has_ssisdb"]) and self._ssisdb, admin)

    def login(self) -> str:
        """The login deploys are recorded under (object_versions.created_by)."""
        return self._scalar("SELECT SUSER_SNAME()") or ""

    def can_modify_project(self, project_id: int) -> bool:
        # Effective permissions include what a folder grant (MODIFY_OBJECTS) gives on its projects.
        return self._scalar("""SELECT COUNT(*) FROM catalog.effective_object_permissions
                               WHERE object_type = 2 AND object_id = ? AND permission_type = 2""", project_id) > 0

    def folder_exists(self, folder: str) -> bool:
        return self._scalar("SELECT COUNT(*) FROM catalog.folders WHERE name = ?", folder) > 0

    def project(self, folder: str, project: str) -> Project | None:
        rows = self._rows("""SELECT p.project_id, f.name AS folder, p.name, p.object_version_lsn,
                                    p.deployed_by_name, CONVERT(varchar(33), p.last_deployed_time, 126) AS last_deployed
                             FROM catalog.projects p JOIN catalog.folders f ON f.folder_id = p.folder_id
                             WHERE f.name = ? AND p.name = ?""", folder, project)
        if not rows:
            return None
        r = rows[0]
        return Project(r["project_id"], r["folder"], r["name"], r["object_version_lsn"],
                       r["deployed_by_name"], r["last_deployed"])

    def packages(self, project_id: int) -> list[str]:
        return [r["name"] for r in self._rows(
            "SELECT name FROM catalog.packages WHERE project_id = ? ORDER BY name", project_id)]

    def project_parameters(self, project_id: int) -> list[str]:
        return [r["parameter_name"] for r in self._rows(
            "SELECT parameter_name FROM catalog.object_parameters WHERE project_id = ? AND object_type = 20",
            project_id)]

    def parameter_bindings(self, project_id: int) -> list[dict]:
        # Literal values of sensitive parameters are never read.
        return self._rows("""SELECT object_type, object_name, parameter_name, value_type,
                                    referenced_variable_name, sensitive,
                                    CASE WHEN sensitive = 0 AND value_type = 'V'
                                         THEN CONVERT(nvarchar(4000), default_value) END AS value
                             FROM catalog.object_parameters
                             WHERE project_id = ? AND value_set = 1
                             ORDER BY object_type, object_name, parameter_name""", project_id)

    def environment_references(self, project_id: int) -> list[dict]:
        return self._rows("""SELECT reference_id, reference_type, environment_folder_name, environment_name
                             FROM catalog.environment_references WHERE project_id = ?
                             ORDER BY reference_id""", project_id)

    def running_executions(self, folder: str, project: str) -> int:
        return self._scalar("""SELECT COUNT(*) FROM catalog.executions
                               WHERE folder_name = ? AND project_name = ? AND status IN (2, 5, 8)""",
                            folder, project)

    def versions(self, project_id: int) -> list[dict]:
        return self._rows("""SELECT object_version_lsn, created_by,
                                    CONVERT(varchar(33), created_time, 126) AS created_time,
                                    restored_by, CONVERT(varchar(33), last_restored_time, 126) AS last_restored_time
                             FROM catalog.object_versions
                             WHERE object_id = ? AND object_type = 20
                             ORDER BY object_version_lsn DESC""", project_id)

    def get_project(self, folder: str, project: str) -> bytes:
        return bytes(self._scalar("EXEC catalog.get_project @folder_name = ?, @project_name = ?", folder, project))

    def deploy_packages(self, folder: str, project: str, packages: list[tuple[str, bytes]]) -> int:
        values = ", ".join("(?, ?)" for _ in packages)
        params = [v for name, data in packages for v in (package_table_name(name), data)]
        sql = f"""SET NOCOUNT ON;
                  DECLARE @t catalog.Package_Table_Type;
                  INSERT INTO @t (name, package_data) VALUES {values};
                  DECLARE @op bigint;
                  EXEC catalog.deploy_packages @folder_name = ?, @project_name = ?,
                       @packages_table = @t, @operation_id = @op OUTPUT;
                  SELECT @op;"""
        return int(self._scalar(sql, *params, folder, project))

    def deploy_project(self, folder: str, project: str, ispac: bytes) -> int:
        sql = """SET NOCOUNT ON;
                 DECLARE @op bigint;
                 EXEC catalog.deploy_project @folder_name = ?, @project_name = ?,
                      @project_stream = ?, @operation_id = @op OUTPUT;
                 SELECT @op;"""
        return int(self._scalar(sql, folder, project, ispac))

    def restore_project(self, folder: str, project: str, lsn: int) -> None:
        self.conn.cursor().execute(
            "EXEC catalog.restore_project @folder_name = ?, @project_name = ?, @object_version_lsn = ?",
            folder, project, lsn)

    def operation(self, operation_id: int) -> dict:
        status = self._scalar("SELECT status FROM catalog.operations WHERE operation_id = ?", operation_id)
        messages = self._rows("""SELECT message_type, message FROM catalog.operation_messages
                                 WHERE operation_id = ? ORDER BY message_time""", operation_id)
        return {"operation_id": operation_id, "status": OPERATION_STATUS.get(status, str(status)),
                "messages": [m["message"] for m in messages if m["message_type"] in (110, 120, 130)]}

    def list_projects(self, folder: str | None) -> list[dict]:
        sql = """SELECT f.name AS folder, p.name AS project, p.object_version_lsn, p.deployed_by_name,
                        CONVERT(varchar(33), p.last_deployed_time, 126) AS last_deployed,
                        (SELECT COUNT(*) FROM catalog.packages k WHERE k.project_id = p.project_id) AS packages
                 FROM catalog.folders f LEFT JOIN catalog.projects p ON p.folder_id = f.folder_id"""
        if folder:
            return self._rows(sql + " WHERE f.name = ? ORDER BY f.name, p.name", folder)
        return self._rows(sql + " ORDER BY f.name, p.name")

    # ------------------------------------------------------------ executions --

    def executions(self, folder=None, project=None, package=None, statuses=None, since_hours=None, limit=20,
                   execution_id=None) -> list[dict]:
        where, params = [], []
        for col, value in (("folder_name", folder), ("project_name", project), ("package_name", package),
                           ("execution_id", execution_id)):
            if value is not None:
                where.append(f"{col} = ?")
                params.append(value)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params += statuses
        if since_hours is not None:
            where.append("start_time >= DATEADD(minute, -?, SYSDATETIMEOFFSET())")
            params.append(int(since_hours * 60))
        sql = f"""SELECT TOP ({int(limit)}) execution_id, folder_name, project_name, package_name, status,
                         environment_folder_name, environment_name, caller_name, executed_as_name,
                         CONVERT(varchar(33), start_time, 126) AS start_time,
                         CONVERT(varchar(33), end_time, 126) AS end_time,
                         DATEDIFF(second, start_time, ISNULL(end_time, SYSDATETIMEOFFSET())) AS duration_s,
                         server_name, machine_name
                  FROM catalog.executions {'WHERE ' + ' AND '.join(where) if where else ''}
                  ORDER BY execution_id DESC"""
        return self._rows(sql, *params)

    def execution_events(self, execution_id: int, message_types: tuple[int, ...] = (120, 130)) -> list[dict]:
        return self._rows(f"""SELECT event_message_id, CONVERT(varchar(33), message_time, 126) AS message_time,
                                     message_type, message_source_type, message_source_name, package_name,
                                     package_path, execution_path, subcomponent_name, message
                              FROM catalog.event_messages
                              WHERE operation_id = ? AND message_type IN ({', '.join(str(int(t)) for t in message_types)})
                              ORDER BY event_message_id""", execution_id)

    def failed_executables(self, execution_id: int) -> list[dict]:
        # Recorded at every logging level, even when event messages are sparse.
        return self._rows("""SELECT e.executable_name, e.package_name, e.package_path, s.execution_path,
                                    s.execution_duration AS duration_ms
                             FROM catalog.executable_statistics s
                             JOIN catalog.executables e ON e.executable_id = s.executable_id
                             WHERE s.execution_id = ? AND s.execution_result = 1
                             ORDER BY s.statistics_id""", execution_id)

    def execution_parameters(self, execution_id: int) -> list[dict]:
        # Project (20) and package (30) parameters as the run saw them; sensitive values are never returned.
        return self._rows("""SELECT object_type, parameter_name,
                                    CASE WHEN sensitive = 1 THEN NULL ELSE CAST(parameter_value AS nvarchar(4000)) END
                                        AS parameter_value,
                                    CAST(sensitive AS bit) AS sensitive, CAST(runtime_override AS bit) AS runtime_override
                             FROM catalog.execution_parameter_values
                             WHERE execution_id = ? AND object_type IN (20, 30)
                             ORDER BY object_type, parameter_name""", execution_id)

