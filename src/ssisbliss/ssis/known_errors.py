"""Known SSIS failure messages: what they usually mean and what to do.

The catalog log has the error, but not why: `0xC0202009` only says an OLE DB error happened,
VS_NEEDSNEWMETADATA says a table changed, "provider is not registered" is a 32/64-bit driver
problem on the server. `explain` turns a task's messages into causes and fixes, including the
classic "works in Visual Studio, fails in the job" ones (the run's account, the job step's 32-bit
setting, the server's own files and drivers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Known:
    id: str
    pattern: str
    cause: str
    fix: str


# Most specific first; a message can match several (an OLE DB wrapper and the login failure inside it).
KNOWN = [
    Known("needs-new-metadata", r"VS_NEEDSNEWMETADATA|0xC004706B",
          "a table or query the data flow reads or writes changed after the package was built (a column was "
          "added, removed, renamed or changed type), so the package's column metadata no longer matches",
          "open the data flow in Visual Studio and refresh the component's columns, then rebuild and redeploy. "
          "`ssisbliss lint --env ENV` compares every data flow's columns with the server's tables before a deploy"),
    Known("truncation", r"truncat|0xC020902A|0xC0209029|DTS_E_INDUCEDTRANSFORMFAILUREONERROR|"
          r"potential loss of data|String or binary data would be truncated",
          "a value is longer (or bigger) than the column it goes into: the source data grew, or a source or "
          "destination column is narrower in the package or the table than the data",
          "find the column named in the message; widen the destination column (and the data flow column), or "
          "send the long rows to the error output. `ssisbliss lint --env ENV` shows columns whose length differs "
          "between the package and the table"),
    Known("conversion", r"could not be converted|Conversion failed when converting|not recognized as a valid "
          r"DateTime|0xC0049064|Arithmetic overflow|Error converting data type",
          "a value in the data does not fit the type it is converted to (a date in another format, text in a "
          "number column, a number too big for its type)",
          "look for the row with the unexpected value in the source; convert it explicitly in the source query "
          "or a Derived Column, or redirect failing rows to the error output"),
    Known("divide-by-zero", r"Divide by zero|divide-by-zero",
          "an expression or query divides by a value that was 0 for some row",
          "guard the division (NULLIF(x, 0) in SQL, a conditional in an SSIS expression)"),
    Known("provider-not-registered", r"provider is not registered on the local machine|"
          r"Microsoft\.ACE\.OLEDB|Microsoft\.Jet\.OLEDB|0xC00F9304",
          "the OLE DB provider the connection uses (often the Excel/Access ACE provider) is not installed on the "
          "SSIS server for the bitness the package runs in. Visual Studio runs packages 32-bit; the catalog and "
          "SQL Agent run them 64-bit unless the job step says 32-bit",
          "install the provider's 64-bit version (Access Database Engine) on the SSIS server, or tick 32-bit "
          "runtime in the job step if only the 32-bit provider can be installed. The provider version in the "
          "message (12.0, 16.0) must be the one installed"),
    Known("login-failed", r"Login failed for user|0x80040E4D",
          "the database refused the login. With Windows authentication the package connects as the account the "
          "run uses, not as the developer who tested it in Visual Studio",
          "give that account a login and the database permissions it needs, or set the right SQL login in the "
          "connection's parameters or environment"),
    Known("cannot-open-database", r"Cannot open database",
          "the login works but has no access to that database, or the database name in the connection is wrong "
          "for this server (check connection_parameters)",
          "grant the account access to the database, or fix the database name in the project parameter or "
          "environment variable"),
    Known("invalid-object", r"Invalid object name|Invalid column name|Could not find stored procedure",
          "a table, column or procedure the package uses is not in this database: the database change it needs "
          "was not deployed to this environment, or the connection points at another database",
          "deploy the database change to this environment first, or fix the database in connection_parameters. "
          "`ssisbliss lint --env ENV` checks the data flows' tables and columns against the server"),
    Known("permission-denied", r"permission was denied|The \w+ permission was denied|is not able to access the "
          r"database|0x80040E09",
          "the account the run uses lacks a permission on a table, procedure or schema",
          "grant the account the permission named in the message (on the object named there)"),
    Known("server-unreachable", r"network-related or instance-specific error|Login timeout expired|"
          r"server was not found or was not accessible|Named Pipes Provider|TCP Provider",
          "the SSIS server cannot reach the database server: a wrong server name for this environment, a "
          "firewall, or the server is down. The name may work from your PC and not from the SSIS server",
          "check the server name in connection_parameters for this environment, and that the SSIS server can "
          "connect to it (port 1433 or the instance's port)"),
    Known("acquire-connection", r"AcquireConnection method call to the connection manager .* failed|"
          r"DTS_E_CANNOTACQUIRECONNECTIONFROMCONNECTIONMANAGER|0xC020801C",
          "the task could not open its connection; the message before this one usually says why",
          "read the other messages for this task (login failed, server not found); connection_parameters shows the "
          "values the run used"),
    Known("oledb-wrapper", r"DTS_E_OLEDBERROR|0xC0202009",
          "an OLE DB error: this code only wraps the real error, which is in the OLE DB record's Description",
          "read the Description in the message (real_error below when ssisbliss found it)"),
    Known("file-missing", r"Could not find (a part of )?(the )?(file|path)|(file|path|directory|folder)\b.{0,200}does not exist|"
          r"FileNotFoundException|"
          r"DirectoryNotFoundException|0x80070002|0x80070003|cannot find the file",
          "a file or folder the package uses is not there on the SSIS server: the path is local to a developer's "
          "PC, a mapped drive (drive letters are per user and do not exist for a service), or the file has not "
          "arrived yet",
          "use a UNC path (\\\\server\\share\\...) from a parameter, check that the file exists on the server at "
          "run time, or rerun once it has arrived"),
    Known("access-denied", r"Access to the path .* is denied|Access is denied|0x80070005|UnauthorizedAccess",
          "the account the run uses may not read or write that file, folder or share (script tasks also compile "
          "into the account's temp folder)",
          "give the account rights on the folder or share; for a proxy, also on its temp folder"),
    Known("no-environment", r"no environment has been selected|environment reference .* not|"
          r"configured to use an environment variable",
          "a parameter is mapped to an environment variable, but the run was started without an environment "
          "(the SQL Agent job step's Environment box is not ticked, or points at another environment)",
          "tick the environment in the job step's configuration, or run with the environment reference. "
          "`ssisbliss jobs` shows each step's environment"),
    Known("script-version", r"Failed to load task|script that is not supported in this release|Cannot load script "
          r"for execution|The script task .* could not be created|0xC0012050|0xC001F02A|version \d+ script",
          "the package was built for another SQL Server version than the server runs (TargetServerVersion), or "
          "the script was compiled by a newer Visual Studio extension",
          "set TargetServerVersion to the server's version in the project, open and rebuild the script tasks, and "
          "redeploy; ssisbliss deploy plans refuse builds for a newer server than the target"),
    Known("decrypt", r"Failed to decrypt|0xC0016016|EncryptSensitiveWithUserKey|protected XML node",
          "the package was saved with a protection level that only the developer's Windows account can decrypt",
          "save the package and project with DontSaveSensitive and put secrets in sensitive parameters; deploy "
          "again"),
    Known("deadlock", r"deadlocked on lock resources|deadlock victim",
          "the query was chosen as a deadlock victim: another process (often another load running at the same "
          "time) held locks it needed",
          "rerun it; if it repeats, check what else runs then (`ssisbliss chain`, `ssisbliss jobs`) and move one "
          "of them, or change the order tables are written in"),
    Known("timeout", r"Query timeout expired|Timeout expired|Execution Timeout Expired",
          "the query ran longer than the command timeout (often because it was blocked, or the data grew)",
          "look for blocking during the run, tune the query, or raise the task's TimeOut/CommandTimeout"),
    Known("duplicate-key", r"Violation of (PRIMARY KEY|UNIQUE KEY) constraint|Cannot insert duplicate key",
          "the load inserted a row whose key is already in the table: duplicates in the source, or a rerun after "
          "a partial load that already inserted some rows",
          "find the duplicate key in the message; clean the target before a rerun, or deduplicate the source"),
    Known("null-not-allowed", r"Cannot insert the value NULL into column",
          "the source has NULL in a column the table requires",
          "fix the source rows, or give the column a default in the data flow (Derived Column / ISNULL)"),
    Known("foreign-key", r"conflicted with the (FOREIGN KEY|CHECK) constraint",
          "rows reference a parent row that is not loaded yet, or break a check constraint",
          "load the parent table first (package order), or find the offending rows"),
    Known("out-of-memory", r"0x8007000E|out of memory|Unable to allocate memory|buffer manager .* failed|"
          r"DTS_E_BUFFERGETTEMPFILENAME|insufficient memory",
          "the data flow ran out of memory: a full-cache Lookup over a big table, a Sort or Aggregate on many rows, "
          "or other loads using the server's memory at the same time",
          "use a partial or no-cache Lookup (or a join in the source query), sort in SQL instead of SSIS, or run "
          "the load when the server is less busy"),
    Known("too-many-errors", r"MaximumErrorCount|maximum allowed \(\d+\)",
          "the package stopped because the number of errors reached MaximumErrorCount; the real errors are the "
          "other messages",
          "read the other messages; do not raise MaximumErrorCount to hide them"),
]

# "An OLE DB record is available.  Source: ...  Hresult: 0x...  Description: "the real error"."
_RECORD = re.compile(r'Description:\s*"(.+?)"\.?(?=\s*An OLE DB record|\s*$)', re.S)


def real_errors(message: str) -> list[str]:
    """The underlying errors inside an OLE DB wrapper message."""
    return [m.strip() for m in _RECORD.findall(message)]


def explain(messages: list[str], run_as: str | None = None) -> list[dict]:
    """Causes and fixes for the known errors in a task's messages, most specific first."""
    text = "\n".join(messages)
    out = []
    for k in KNOWN:
        if re.search(k.pattern, text, re.I):
            e: dict = {"error": k.id, "cause": k.cause, "fix": k.fix}
            if k.id == "oledb-wrapper":
                inner = [r for m in messages for r in real_errors(m)]
                if inner:
                    e["real_error"] = list(dict.fromkeys(inner))
            if run_as and k.id in ("login-failed", "permission-denied", "cannot-open-database", "access-denied",
                                   "file-missing"):
                e["run_as"] = run_as
            out.append(e)
    # The wrapper adds nothing once the error inside it is explained too.
    if len(out) > 1:
        out = [e for e in out if e["error"] != "oledb-wrapper" or "real_error" in e]
    return out
