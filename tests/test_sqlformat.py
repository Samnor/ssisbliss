"""The SQL layout must be safe (only whitespace and keyword case change), stable (formatting twice changes
nothing) and independent of how the original was spaced; and it must lay out common SSIS SQL readably."""
import random
from pathlib import Path

import pytest

from ssisbliss.ssis.sqlformat import _signature, format_sql, tokenize
from ssisbliss.ssis.sqlsites import decode_attribute, encode_attribute, encode_text, replace, sites

CORPUS = [
    "insert into stage.Customer (Id, Name) select c.Id, c.Name from bronze.Customer c join bronze.Region r on r.Id = c.RegionId where c.Active = 1 and r.Code <> 'XX'",
    "UPDATE dbo.Batch SET Status = ?, EndTime = SYSDATETIME() WHERE BatchId = ? AND Status <> ?",
    "MERGE dbo.Customer AS t USING stage.Customer AS s ON t.Id = s.Id WHEN MATCHED AND t.Name <> s.Name THEN UPDATE SET t.Name = s.Name WHEN NOT MATCHED BY TARGET THEN INSERT (Id, Name) VALUES (s.Id, s.Name) WHEN NOT MATCHED BY SOURCE THEN DELETE OUTPUT $action, inserted.Id INTO #log;",
    "EXEC dbo.LoadCdw @Mode = 'Full', @Rows = ? OUTPUT",
    "BEGIN TRY BEGIN TRANSACTION; DELETE FROM dbo.Fact WHERE LoadDate = ?; COMMIT TRANSACTION; END TRY BEGIN CATCH IF @@TRANCOUNT > 0 ROLLBACK; THROW; END CATCH",
    "SELECT 'select * from x -- not a comment', N'it''s', [from], [Order Details].[Unit Price] FROM [dbo].[Order Details] /* outer /* nested */ still */ WHERE 1 = 1",
    "SELECT Id, ROW_NUMBER() OVER (PARTITION BY Region ORDER BY Created DESC) AS rn, SUM(Qty) OVER () AS total FROM dbo.Orders",
    "SELECT * FROM (SELECT Region, Qty FROM dbo.Orders) src PIVOT (SUM(Qty) FOR Region IN ([N], [S])) p",
    "DECLARE @sql nvarchar(max) = N'SELECT * FROM dbo.T WHERE x = 1'; EXEC sp_executesql @sql;",
    "IF OBJECT_ID('tempdb..#t') IS NOT NULL DROP TABLE #t\nGO 5\nSELECT 1",
    "retry: WAITFOR DELAY '00:00:01'; SELECT CAST(x AS decimal(10, 2)), TRY_CONVERT(int, y), z::geometry FROM dbo.T CROSS APPLY dbo.F(a) f OUTER APPLY (SELECT TOP (1) b FROM dbo.U WHERE U.k = T.k ORDER BY b) u",
    "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT x FROM b UNION ALL SELECT 2 EXCEPT SELECT 3",
    "SELECT CASE WHEN a = 1 THEN CASE WHEN b = 2 THEN 'x' END ELSE 'y' END FROM dbo.T WHERE c BETWEEN 1 AND 5 OR d IN (1, 2, 3)",
    "TRUNCATE TABLE stage.Customer; INSERT stage.Customer SELECT * FROM bronze.Customer WITH (NOLOCK) OPTION (RECOMPILE);",
    "-- header comment\nSELECT 1 -- trailing\n/* own line */\nFROM dbo.T",
    "SET NOCOUNT ON; DECLARE @n int; SELECT @n = COUNT(*) FROM dbo.T; IF @n = 0 THROW 50001, N'empty', 1; ELSE PRINT 'ok';",
    "select a,b,c from t where x=1 and y=2 group by a,b having count(*)>1 order by a desc",
    "",
    "   ",
    "SELECT 'unterminated",
    "SELECT [unterminated",
]


@pytest.mark.parametrize("sql", CORPUS)
def test_safe_and_stable(sql):
    formatted, why = format_sql(sql)
    if why:
        assert formatted == sql
        return
    assert _signature(tokenize(formatted)) == _signature(tokenize(sql))
    assert format_sql(formatted) == (formatted, "")


def _respace(sql: str, rng: random.Random) -> str:
    """The same tokens with different whitespace: a gap with a line break keeps one, a gap without gets none."""
    out = []
    for i, t in enumerate(tokenize(sql)):
        if i and t.newline_before:
            out.append(rng.choice(["\n", "\n\n", " \n\t", "\n    "]))
        elif i and t.space_before:
            out.append(rng.choice([" ", "  ", "\t", " \t "]))
        out.append(t.text)
    return "".join(out)


@pytest.mark.parametrize("sql", [s for s in CORPUS if s.strip() and "unterminated" not in s])
def test_layout_does_not_depend_on_the_original_spacing(sql):
    rng = random.Random(sql)
    expected = format_sql(sql)[0]
    for _ in range(5):
        assert format_sql(_respace(sql, rng))[0] == expected


def test_readable_layout():
    f, _ = format_sql("insert into stage.Customer (Id, Name) select c.Id, c.Name from bronze.Customer c "
                      "join bronze.Region r on r.Id = c.RegionId where c.Active = 1 and r.Code <> 'XX'")
    assert f == ("INSERT INTO stage.Customer (Id, Name)\nSELECT\n    c.Id,\n    c.Name\nFROM bronze.Customer c\n"
                 "JOIN bronze.Region r ON r.Id = c.RegionId\nWHERE c.Active = 1\n    AND r.Code <> 'XX'")
    f, _ = format_sql("MERGE dbo.C AS t USING s.C AS s ON t.Id = s.Id WHEN MATCHED THEN UPDATE SET t.N = s.N "
                      "WHEN NOT MATCHED THEN INSERT (Id, N) VALUES (s.Id, s.N);")
    assert f == ("MERGE dbo.C AS t\nUSING s.C AS s ON t.Id = s.Id\nWHEN MATCHED THEN\n    UPDATE SET t.N = s.N\n"
                 "WHEN NOT MATCHED THEN\n    INSERT (Id, N)\n    VALUES (s.Id, s.N);")
    assert format_sql("EXEC dbo.P @a = ?, @b = ? OUTPUT")[0] == "EXEC dbo.P @a = ?, @b = ? OUTPUT"


def test_strings_identifiers_and_comments_are_untouched():
    sql = "select 'from x where' as [select], n'ü' from t -- keep this  "
    f, why = format_sql(sql)
    assert not why and "'from x where'" in f and "[select]" in f and "n'ü'" in f and "-- keep this" in f


# --------------------------------------------------------------- sites in a .dtsx --

FIXTURES = Path(__file__).parent / "fixtures"


def test_sites_round_trip_exactly_like_ssis_writes_them():
    sql = 'SELECT c.Id,\n\tc.Name -- the "name" <b> & co\nFROM dbo.Customer AS c\nWHERE c.Id > ?;'
    # What SQL Server 2019's object model wrote for this statement (lab, Execute SQL task):
    stored = (b'SELECT c.Id,&#xA;&#x9;c.Name -- the &quot;name&quot; &lt;b&gt; &amp; co&#xA;FROM dbo.Customer AS c'
              b'&#xA;WHERE c.Id &gt; ?;')
    assert encode_attribute(sql) == stored and decode_attribute(stored) == sql
    assert encode_attribute(sql.replace("\n", "\r\n")) == stored          # carriage returns are dropped, as SSIS does
    assert b"\n" in encode_text(sql, literal_newlines=True) and b"&#xA;" in encode_text(sql, literal_newlines=False)


def test_replacing_one_statement_changes_only_its_bytes():
    data = (FIXTURES / "Load_Prices.dtsx").read_bytes()
    found = sites(data)
    assert [s.task for s in found] == ["Check rows", "Load", "Log done", "Truncate staging"]
    new = replace(data, {1: format_sql(found[1].sql)[0]}, found)
    after = sites(new)
    assert after[1].sql == format_sql(found[1].sql)[0] and [s.sql for s in after][::2] == [s.sql for s in found][::2]
    assert new[:found[1].start] == data[:found[1].start] and new[-(len(data) - found[1].end):] == data[found[1].end:]
