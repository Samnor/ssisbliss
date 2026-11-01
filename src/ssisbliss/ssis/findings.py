"""Lint's vocabulary: every rule, with its stable id, name, severity and meaning, and what a finding says.
Below everything that reports findings (lint, the design rules, the data flow column checks)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

RULES = {
    # package and task rules
    "SSB101": ("sql-not-static", "info", "SQL comes from an expression, variable or file, so it is not checked"),
    "SSB102": ("parameter-markers", "error", "? parameter markers and parameter mappings do not match"),
    "SSB103": ("result-set", "warn", "result set setting and result bindings do not match"),
    "SSB104": ("default-name", "warn", "task still has its default name, which says nothing in a review"),
    "SSB105": ("disabled-task", "info", "task is disabled"),
    # SQL correctness
    "SSB201": ("sql-parse", "info", "ssisbliss's own T-SQL parser could not read it (not necessarily wrong)"),
    "SSB202": ("no-where", "warn", "DELETE or UPDATE without a WHERE clause"),
    "SSB203": ("select-star", "warn", "SELECT * (column changes break data flows and hide what is used)"),
    "SSB204": ("nolock", "warn", "NOLOCK / READUNCOMMITTED hint (dirty reads)"),
    "SSB205": ("insert-columns", "warn", "INSERT without a column list"),
    "SSB206": ("database-name", "info", "three-part name with a hard-coded database"),
    "SSB210": ("server-parse", "error", "the server's parser rejects this SQL"),
    # data flow columns against the server's tables (lint --env)
    "SSB401": ("table-missing", "error", "a data flow's table is not on the server"),
    "SSB402": ("column-missing", "error", "a data flow column is not in the table any more (VS_NEEDSNEWMETADATA)"),
    "SSB403": ("column-type", "error", "a data flow column has another type than the table's"),
    "SSB404": ("column-size", "warn", "a data flow column's length differs from the table's (error when it truncates)"),
    "SSB405": ("new-required-column", "error", "the destination table has a NOT NULL column the package does not fill"),
    "SSB409": ("columns-not-checked", "info", "a data flow source or destination could not be compared with the server"),
    # readability in a pull request
    # package design: speed, and what fails under SQL Agent but not in Visual Studio (design.py)
    "SSB501": ("row-by-row", "warn", "a data flow component runs one statement per row (OLE DB Command, SCD)"),
    "SSB502": ("no-fast-load", "warn", "a destination inserts one row at a time"),
    "SSB503": ("lookup-no-cache", "warn", "a lookup without a cache queries the server once per row"),
    "SSB504": ("blocking-sort", "info", "a Sort holds every row in memory"),
    "SSB510": ("excel-driver", "warn", "Excel/Access through Jet (32-bit only) or ACE (an extra driver on the server)"),
    "SSB511": ("provider-missing", "error", "the server does not have an OLE DB provider the package uses"),
    "SSB520": ("temp-table-session", "warn", "a temp table on a connection that does not keep its session"),
    "SSB521": ("hard-coded-server", "info", "a connection string with a server name written in the package"),
    "SSB301": ("not-formatted", "warn", "SQL is not laid out the readable way (`ssisbliss format`)"),
    "SSB302": ("cannot-format", "info", "SQL could not be laid out automatically"),
    "SSB303": ("long-line", "info", "a line longer than the limit even after layout"),
    "SSB304": ("commented-out-sql", "info", "commented-out SQL"),
}
SEVERITY = {"error": 3, "warn": 2, "info": 1}


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    task: str
    path: str
    line: int
    column: int
    message: str
    hint: str = ""

    @property
    def name(self) -> str:
        return RULES[self.rule][0]

    def to_dict(self) -> dict:
        return {"rule": self.rule, "name": self.name, **{k: v for k, v in asdict(self).items() if k != "rule"}}

    def fingerprint(self) -> str:
        """Identifies a finding across edits that only move lines (for baselines)."""
        message = re.sub(r"line \d+", "", self.message)
        return f"{self.rule}|{self.file.lower()}|{self.path}|{message}"
