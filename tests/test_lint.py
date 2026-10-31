"""ssisbliss lint, format and sql-diff on packages in a git repository, the way a pull request uses them."""
import json
import subprocess
import uuid
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from ssisbliss.ssis import cli
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.lint import lint_package
from ssisbliss.ssis.sqlsites import sites

CONN = "{00000000-0000-0000-0000-000000000001}"


def test_a_repeated_statement_is_parsed_once_and_each_task_still_gets_its_findings(monkeypatch):
    import sqlglot

    from ssisbliss.ssis import lint
    calls = []
    parse = sqlglot.parse
    monkeypatch.setattr(lint.sqlglot, "parse", lambda *a, **k: calls.append(a) or parse(*a, **k))
    lint._sql_checks.cache_clear()
    sql = "delete from stage.RepeatedOnce"
    found = lint_package("p.dtsx", pkg("p", [("Empty one", sql), ("Empty two", sql), ("Empty three", sql)]))
    assert len(calls) == 1
    assert [f.task for f in found if f.rule == "SSB202"] == ["Empty one", "Empty three", "Empty two"]


def test_the_same_select_star_keeps_the_message_of_its_kind_of_task():
    from ssisbliss.ssis.lint import _sql_checks
    task, flow = _sql_checks("select * from stage.T", "execute_sql"), _sql_checks("select * from stage.T", "dataflow")
    assert "hides which columns" in task[0][3] and "data-flow source" in flow[0][3]


def pkg(name: str, tasks: list[tuple[str, str]], params: dict[str, int] | None = None, conn_type: str = "OLEDB",
        result: str = "ResultSetType_None") -> bytes:
    """A package with Execute SQL tasks (task name, SQL) on one connection manager."""
    params = params or {}
    body = "".join(
        f'<DTS:Executable DTS:refId="Package\\{n}" DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="{n}">'
        f'<DTS:ObjectData><SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" '
        f'SQLTask:Connection="{CONN}" SQLTask:ResultType="{result}" SQLTask:SqlStatementSource={quoteattr(q).replace(chr(10), "&#xA;")}>'
        + "".join(f'<SQLTask:ParameterBinding SQLTask:ParameterName="{i}" SQLTask:DtsVariableName="User::v{i}" />'
                  for i in range(params.get(n, 0)))
        + '</SQLTask:SqlTaskData></DTS:ObjectData></DTS:Executable>' for n, q in tasks)
    return f"""<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package"
  DTS:ObjectName="{name}" DTS:DTSID="{{{uuid.uuid5(uuid.NAMESPACE_URL, name)}}}" DTS:VersionGUID="{{{uuid.uuid4()}}}" DTS:ProtectionLevel="0">
  <DTS:Property DTS:Name="PackageFormatVersion">8</DTS:Property>
  <DTS:ConnectionManagers><DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Target]" DTS:CreationName="{conn_type}"
    DTS:DTSID="{CONN}" DTS:ObjectName="Target" /></DTS:ConnectionManagers>
  <DTS:Executables>{body}</DTS:Executables>
</DTS:Executable>
""".encode()


def rules(data: bytes) -> list[str]:
    return [f.rule for f in lint_package("p.dtsx", data)]


def test_rules_find_what_they_say():
    assert "SSB102" in rules(pkg("P", [("Mark batch", "UPDATE dbo.Batch SET Status = ? WHERE Id = ?")], {"Mark batch": 1}))
    assert "SSB102" not in rules(pkg("P", [("Mark batch", "UPDATE dbo.Batch SET Status = ? WHERE Id = ?")], {"Mark batch": 2}))
    assert "SSB102" in rules(pkg("P", [("Mark", "UPDATE dbo.B SET s = ? WHERE i = 1")], {"Mark": 1}, conn_type="ADO.NET:System.Data.SqlClient"))
    assert "SSB103" in rules(pkg("P", [("Count", "SELECT COUNT(*) FROM dbo.T")], result="ResultSetType_SingleRow"))
    assert "SSB104" in rules(pkg("P", [("Execute SQL Task 1", "SELECT 1")]))
    assert "SSB202" in rules(pkg("P", [("Empty", "DELETE FROM dbo.Fact")]))
    assert "SSB202" not in rules(pkg("P", [("Empty", "DELETE FROM dbo.Fact WHERE d < '2020-01-01'")]))
    assert "SSB203" in rules(pkg("P", [("Copy", "INSERT INTO s.T (a) SELECT * FROM b.T")]))
    assert "SSB203" not in rules(pkg("P", [("Check", "IF EXISTS (SELECT * FROM b.T) SELECT 1")]))
    assert "SSB204" in rules(pkg("P", [("Read", "SELECT a FROM b.T WITH (NOLOCK)")]))
    assert "SSB205" in rules(pkg("P", [("Copy", "INSERT INTO s.T SELECT a, b FROM b.T")]))
    assert "SSB206" in rules(pkg("P", [("Copy", "INSERT INTO s.T (a) SELECT a FROM OtherDb.b.T")]))
    assert "SSB301" in rules(pkg("P", [("Load", "insert into s.T (a, b) select a, b from b.T where x = 1 and y = 2")]))
    assert "SSB304" in rules(pkg("P", [("Load", "SELECT 1\n-- SELECT a FROM old.T WHERE x = 1")]))


def test_a_clean_package_has_no_findings():
    sql = "INSERT INTO stage.Customer (Id, Name)\nSELECT\n    Id,\n    Name\nFROM bronze.Customer\nWHERE Active = 1;"
    assert lint_package("p.dtsx", pkg("P", [("Load stage.Customer", sql)])) == []


def test_finding_points_at_the_line_in_the_sql():
    f = next(x for x in lint_package("p.dtsx", pkg("P", [("Read", "SELECT a\nFROM b.T WITH (NOLOCK)")])) if x.rule == "SSB204")
    assert (f.line, f.column, f.task) == (2, 16, "Read")


# --------------------------------------------------------------- the commands --

def run(cwd: Path, *args, ok=(0,)):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev",
                            "GIT_COMMITTER_EMAIL": "dev@example.com", "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert p.returncode in ok, p.stderr
    return p.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    run(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "STAGE").mkdir()
    (tmp_path / "STAGE" / "Load.dtsx").write_bytes(pkg("Load", [("Load customers",
        "insert into stage.Customer (Id, Name) select c.Id, c.Name from bronze.Customer c where c.Active = 1")]))
    (tmp_path / "STAGE" / "Other.dtsx").write_bytes(pkg("Other", [("Count orders", "SELECT COUNT(*) FROM dbo.Orders")]))
    run(tmp_path, "add", "-A")
    run(tmp_path, "commit", "-q", "-m", "released")
    run(tmp_path, "checkout", "-q", "-b", "pr/customer")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def call(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, out


def test_format_lays_out_only_the_sql_and_is_stable(repo, capsys):
    path = repo / "STAGE" / "Load.dtsx"
    before = path.read_bytes()
    code, out = call(capsys, "format", "--check", "STAGE")
    assert code == 2 and out["would_change"] == 2 and path.read_bytes() == before      # both packages are one-liners
    code, out = call(capsys, "format", "STAGE")
    after = path.read_bytes()
    assert code == 0 and out["changed"] == 2 and all(p["written"] for p in out["packages"])
    assert sites(after)[0].sql == ("INSERT INTO stage.Customer (Id, Name)\nSELECT\n    c.Id,\n    c.Name\n"
                                   "FROM bronze.Customer c\nWHERE c.Active = 1")
    s = sites(before)[0]
    assert after[:s.start] == before[:s.start] and after.endswith(before[s.end:])      # nothing else moved
    assert package_sha256(after)
    code, out = call(capsys, "format", "--check", "STAGE")
    assert code == 0 and out["would_change"] == 0                                    # stable


def test_sql_diff_shows_the_change_readably(repo, capsys):
    path = repo / "STAGE" / "Load.dtsx"
    call(capsys, "format", "STAGE")
    code, out = call(capsys, "sql-diff", "--formatted", "STAGE")
    assert out["packages"] == []                                   # layout only: no real SQL change
    code, out = call(capsys, "sql-diff", "STAGE")
    assert out["packages"][0]["sql_changes"][0]["change"] == "changed"
    data = sites(path.read_bytes())
    new = data[0].sql.replace("WHERE c.Active = 1", "WHERE c.Active = 1\n    AND c.Region = 'N'")
    from ssisbliss.ssis.sqlsites import replace
    path.write_bytes(replace(path.read_bytes(), {0: new}, data))
    code, md = call(capsys, "sql-diff", "--formatted", "--markdown", "STAGE")
    assert "**STAGE/Load.dtsx** › `Package\\Load customers` (changed)" in md
    assert "+    AND c.Region = 'N'" in md and "-insert" not in md and "```diff" in md
    raw = run(repo, "diff", "--", "STAGE/Load.dtsx")
    assert len([l for l in raw.splitlines() if l.startswith("+") and not l.startswith("+++")]) == 1   # one long line in git


def test_lint_changed_baseline_and_exit_codes(repo, capsys):
    code, out = call(capsys, "lint", "STAGE")
    assert code == 0 and out["counts"]["warn"] >= 1 and "format_command" in out
    code, out = call(capsys, "lint", "--changed", "STAGE")
    assert out["packages"] == 0                                    # nothing changed on the branch yet
    code, out = call(capsys, "lint", "--fail-on", "warn", "STAGE")
    assert code == 2
    call(capsys, "lint", "--update-baseline", "STAGE")
    code, out = call(capsys, "lint", "--fail-on", "warn", "STAGE")
    assert code == 0 and out["findings"] == [] and out["baselined"] >= 1
    (repo / "STAGE" / "Other.dtsx").write_bytes(pkg("Other", [("Count orders", "SELECT COUNT(*) FROM dbo.Orders"),
                                                              ("Clean", "DELETE FROM dbo.Orders")]))
    code, out = call(capsys, "lint", "--changed", "STAGE")
    assert out["packages"] == 1 and {f["rule"] for f in out["findings"]} == {"SSB202"}


def test_rules_listing(capsys):
    code, out = call(capsys, "lint", "--rules")
    assert out["rules"]["SSB102"]["severity"] == "error" and len(out["rules"]) >= 15
