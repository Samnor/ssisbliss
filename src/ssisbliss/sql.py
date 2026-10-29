"""ODBC connection strings shared by the tools. Windows auth by default; SQL auth reads the
password from ONPREM_SQL_PASSWORD so it never appears on a command line."""
from __future__ import annotations

import os


def connection_string(server: str, sql_user: str | None = None, trust_server_certificate: bool = False,
                      driver: str | None = None, database: str = "SSISDB",
                      app: str = "ssisbliss") -> str:
    import pyodbc

    if driver is None:
        installed = pyodbc.drivers()
        driver = next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
                       if d in installed), None)
        if driver is None:
            raise RuntimeError("No SQL Server ODBC driver found (install ODBC Driver 18 for SQL Server)")
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}", f"APP={app}"]
    if sql_user:
        password = os.environ.get("ONPREM_SQL_PASSWORD")
        if password is None:
            raise RuntimeError("--sql-user needs the password in ONPREM_SQL_PASSWORD")
        parts += [f"UID={sql_user}", "PWD={" + password.replace("}", "}}") + "}"]
    else:
        parts.append("Trusted_Connection=yes")
    if trust_server_certificate:
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)
