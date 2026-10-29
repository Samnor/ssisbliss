"""A deterministic T-SQL layout for SQL in SSIS packages, made for reading changes in a pull request.

Only whitespace and the case of reserved keywords change. The SQL is split into tokens that are
pieces of the original text; the layout puts each major clause on its own line, one select column
or SET assignment per line, AND/OR conditions on their own lines, and indents BEGIN...END, TRY/CATCH
and subqueries. Spacing within a line follows the original: tokens that touched still touch, tokens
that were apart get one space. Before returning, `format_sql` checks that the result has exactly the
original's tokens in order (keywords compared without case); if not, it returns the SQL unchanged
with the reason. Formatting twice gives the same result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

INDENT = "    "

# Reserved keywords (Transact-SQL reserved keywords, SQL Server 2019) that are uppercased.
RESERVED = set("""
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE CHECK
CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS CONTAINSTABLE CONTINUE
CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DBCC
DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT DISTRIBUTED DOUBLE DROP DUMP ELSE END ERRLVL ESCAPE
EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL
FUNCTION GOTO GRANT GROUP HAVING HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT
INTERSECT INTO IS JOIN KEY KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL NULLIF OF
OFF OFFSETS ON OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN
PRECISION PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES REPLICATION
RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE SCHEMA SECURITYAUDIT SELECT
SEMANTICKEYPHRASETABLE SEMANTICSIMILARITYDETAILSTABLE SEMANTICSIMILARITYTABLE SESSION_USER SET SETUSER
SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE
TRY_CONVERT TSEQUAL UNION UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE
WHILE WITH WITHIN WRITETEXT
""".split()) | {"TRY", "CATCH", "OUTPUT", "MATCHED", "USING", "THROW", "APPLY", "NOLOCK", "OFFSET", "ROWS", "ONLY",
                "NEXT", "PARTITION", "TARGET", "SOURCE"}

CLAUSES = {"SELECT", "FROM", "WHERE", "HAVING", "VALUES", "OUTPUT", "OPTION", "USING", "UNION", "EXCEPT", "INTERSECT"}
STATEMENTS = {"INSERT", "UPDATE", "DELETE", "MERGE", "DECLARE", "EXEC", "EXECUTE", "IF", "ELSE", "WHILE", "RETURN",
              "THROW", "PRINT", "RAISERROR", "TRUNCATE", "DROP", "CREATE", "ALTER", "COMMIT", "ROLLBACK", "SAVE",
              "GOTO", "BREAK", "CONTINUE", "USE", "WAITFOR", "OPEN", "CLOSE", "FETCH", "DEALLOCATE", "WITH"}
JOIN_WORDS = {"INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "JOIN"}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line>--[^\n]*)
  | (?P<block>/\*)
  | (?P<string>N?'(?:[^']|'')*'?)
  | (?P<qident>\[(?:[^\]]|\]\])*\]?|"(?:[^"]|"")*"?)
  | (?P<number>0x[0-9a-fA-F]*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<word>[A-Za-z_@#$\u0080-￿][\w@#$\u0080-￿]*)
  | (?P<op><>|!=|!<|!>|<=|>=|\+=|-=|\*=|/=|%=|&=|\^=|\|=|::|\|\|)
  | (?P<char>.)
""", re.X | re.S)


@dataclass
class Token:
    kind: str     # ws, line, block, string, qident, number, word, op, char
    text: str
    space_before: bool = False      # whitespace (or start of text) before it in the source
    newline_before: bool = False    # a line break before it in the source
    start: int = 0                  # offset in the source

    @property
    def upper(self) -> str:
        return self.text.upper()

    @property
    def keyword(self) -> bool:
        return self.kind == "word" and self.upper in RESERVED


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos, space, newline = 0, True, True
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        kind = m.lastgroup
        if kind == "block":                       # nested /* */ comments, as T-SQL allows
            depth, i = 1, m.end()
            while i < len(sql) and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            text, end = sql[pos:i], i
        else:
            text, end = m.group(0), m.end()
        if kind == "ws":
            space, newline = True, newline or "\n" in text
        else:
            tokens.append(Token(kind, text, space, newline, pos))
            space, newline = kind == "line", kind == "line"
        pos = end
    return tokens


def _signature(tokens: list[Token]) -> list[str]:
    """What must not change: every token, keywords without case, comments without trailing spaces."""
    return [t.upper if t.keyword else t.text.rstrip() if t.kind in ("line", "block") else t.text for t in tokens]


class _Layout:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cur = ""
        self.indent = 0

    def newline(self, indent: int | None = None) -> None:
        if self.cur.strip():
            self.lines.append(self.cur.rstrip())
        self.cur = ""
        if indent is not None:
            self.indent = max(indent, 0)

    def blank(self) -> None:
        self.newline()
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def put(self, text: str, space: bool) -> None:
        if not self.cur:
            self.cur = INDENT * self.indent + text
        else:
            self.cur += (" " if space else "") + text

    def text(self) -> str:
        self.newline()
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        return "\n".join(self.lines)


def _list_items(tokens: list[Token], i: int, stop: set[str]) -> int:
    """Top-level commas after tokens[i] until a token in stop (at the same paren depth) or the end."""
    depth, commas = 0, 0
    for t in tokens[i + 1:]:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0 and (t.text == ";" or t.keyword and t.upper in stop):
            break
        elif depth == 0 and t.text == ",":
            commas += 1
    return commas


def layout(tokens: list[Token]) -> str:
    out = _Layout()
    base = 0                    # indent of the current statement's clauses
    blocks: list[int] = []      # BEGIN...END and TRY/CATCH: the base to return to
    parens: list[tuple[bool, int]] = []   # (is a subquery, base before it)
    case_depth = 0
    listing = False             # inside a multi-item SELECT list or UPDATE SET list (top level)
    list_depth = 0
    between = False
    stmt = ""                   # first keyword of the current statement (INSERT, MERGE, EXEC, ...)
    merge_cond = False          # between WHEN and THEN in a MERGE
    merge_action = False        # after THEN in a MERGE: the action keeps UPDATE SET / INSERT ... together
    body_pending = False        # after IF ... / ELSE: the next statement is the body
    body_base: int | None = None
    stop_list = CLAUSES | STATEMENTS | JOIN_WORDS | {"INTO", "SET", "GROUP", "ORDER", "WHEN", "ON", "END"}
    prev: Token | None = None
    for i, t in enumerate(tokens):
        u = t.upper if t.kind == "word" else t.text
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        text = t.upper if t.keyword else (t.text.rstrip() if t.kind in ("line", "block") else t.text)
        inline = parens and not parens[-1][0]          # inside function call / column list parens
        depth0 = not parens or parens[-1][0]

        if t.kind in ("line", "block"):
            if t.newline_before and prev is not None:
                out.newline()
            out.put(text, prev is not None and t.space_before)
            if t.kind == "line":
                out.newline()
            prev = t
            continue
        if t.kind == "word" and u == "GO" and t.newline_before and (nxt is None or nxt.newline_before):
            out.blank()
            out.put("GO", False)
            out.blank()
            base, out.indent, listing = 0, 0, False
            prev = t
            continue

        brk = None                                     # indent to break the line at, if any
        if t.keyword and not inline and case_depth == 0 and merge_action and u in ("UPDATE", "INSERT", "DELETE", "SET"):
            pass                                       # THEN UPDATE SET ... / THEN INSERT (...): on the action line
        elif t.keyword and not inline and case_depth == 0 and merge_action and u == "VALUES":
            brk = base + 1
        elif t.keyword and not inline and case_depth == 0 and merge_cond and u in ("AND", "OR"):
            pass                                       # WHEN MATCHED AND ... THEN stays on one line
        elif t.keyword and not inline and case_depth == 0 and u == "OUTPUT" and stmt not in ("INSERT", "UPDATE", "DELETE", "MERGE"):
            pass                                       # EXEC ... @p = ? OUTPUT is a parameter, not a clause
        elif t.keyword and not inline and case_depth == 0 and u == "FROM" and prev is not None and prev.upper == "DELETE":
            pass                                       # DELETE FROM stays together
        elif t.keyword and not inline and case_depth == 0:
            if u == "END" and blocks:
                base = blocks.pop()
                brk = base
                listing = False
            elif u in ("BEGIN",):
                brk = base
            elif u in ("ELSE",):
                brk = base
            elif u in STATEMENTS and not (u == "WITH" and prev is not None and prev.text not in (";",) and prev.upper not in ("BEGIN", "END")):
                if not (prev is not None and prev.upper in ("INSERT", "DELETE") and u == "INTO"):
                    brk = base
                    listing = False
            elif u in CLAUSES or u == "INTO" and prev is not None and prev.upper != "INSERT" and prev.upper != "MERGE":
                brk = base
                listing = False
            elif u in ("SET",) and prev is not None and prev.upper not in ("UPDATE",):
                brk = base
                listing = False
            elif u == "SET":
                brk = base
            elif u in ("GROUP", "ORDER") and nxt is not None and nxt.upper == "BY":
                brk = base
                listing = False
            elif u in JOIN_WORDS and not (prev is not None and prev.upper in JOIN_WORDS | {"CROSS", "OUTER"}):
                brk = base
            elif u == "APPLY" and prev is not None and prev.upper in ("CROSS", "OUTER"):
                pass
            elif u == "WHEN":
                brk = base
            elif u in ("AND", "OR") and not between:
                brk = base + 1
            if u == "BETWEEN":
                between = True
            elif u == "AND" and between:
                between = False
        if t.text == ";" or t.text == ")" and parens and parens[-1][0]:
            listing = False
        if brk is not None and not parens and body_base is not None and u not in ("ELSE",) and brk <= body_base \
                and not body_pending:
            base = body_base                           # the IF/ELSE body statement ended without a semicolon
            body_base = None
            brk = min(brk, base)
        if u == "ELSE" and body_base is not None:
            base, body_base = body_base, None
            brk = base
        if brk is not None and body_pending and not parens and u != "BEGIN":
            body_base, body_pending = base, False      # IF cond / ELSE: the single statement is indented
            base += 1
            brk = base
        elif brk is not None and body_pending and u == "BEGIN":
            body_pending = False
        if brk is not None and depth0 and (u in STATEMENTS or u in ("SELECT", "SET")) and not merge_action:
            stmt = u

        if t.text == ")" and parens:
            sub, saved = parens.pop()
            if sub:
                out.newline(saved)
                base = saved
                out.put(")", False)
                prev = t
                continue
        if brk is not None:
            out.newline(brk)
        elif listing and t.text != "," and prev is not None and prev.text == "," and depth0 and len(parens) == list_depth:
            out.newline(base + 1)
        out.put(text, prev is not None and (t.space_before or out.cur == "") and t.text not in (",", ";", ")")
                and not (prev.text == "(" or prev.text == "." or t.text == "."))

        # after the token
        if t.keyword and not inline and case_depth == 0:
            if u == "BEGIN" and not (nxt is not None and nxt.upper in ("TRAN", "TRANSACTION", "DISTRIBUTED")):
                blocks.append(base)
                if nxt is not None and nxt.upper in ("TRY", "CATCH"):
                    pass
                else:
                    base += 1
                    out.newline(base)
            if u in ("TRY", "CATCH") and prev is not None and prev.upper == "BEGIN":
                base += 1
                out.newline(base)
            if u in ("SELECT", "SET") and _list_items(tokens, i, stop_list) > 0:
                listing, list_depth = True, len(parens)
                j = i + 1                                  # keep DISTINCT / TOP (n) on the SELECT line
                while j < len(tokens) and tokens[j].upper in ("DISTINCT", "ALL", "TOP", "PERCENT", "WITH", "TIES"):
                    j += 1
                if u == "SELECT" and j < len(tokens) and tokens[j - 1].upper == "TOP" and tokens[j].text == "(":
                    pass
                elif j == i + 1:
                    out.newline(base + 1)
        if t.kind == "word" and t.upper == "CASE":
            case_depth += 1
        elif t.kind == "word" and t.upper == "END" and case_depth:
            case_depth -= 1
        if t.text == "(":
            sub = nxt is not None and nxt.upper in ("SELECT", "WITH")
            parens.append((sub, base))
            if sub:
                base += 1
                out.newline(base)
        if t.keyword and depth0 and case_depth == 0:
            if u == "MERGE":
                stmt = "MERGE"
            if stmt == "MERGE" and u == "WHEN":
                merge_cond, merge_action = True, False
            elif stmt == "MERGE" and u == "THEN" and merge_cond:
                merge_cond, merge_action = False, True
                out.newline(base + 1)
            if u in ("IF", "WHILE") and stmt == u or u == "ELSE":
                body_pending = True
        if t.text == ";":
            if body_base is not None:
                base, body_base = body_base, None
            merge_cond = merge_action = False
            stmt = ""
            out.newline(base)
        prev = t
    return out.text()


def format_sql(sql: str) -> tuple[str, str]:
    """(formatted, "") or (sql unchanged, reason)."""
    if not sql.strip():
        return sql, ""
    try:
        tokens = tokenize(sql)
        if any(t.kind == "char" and t.text in ("'", "[", '"') for t in tokens):
            return sql, "an unterminated string or identifier"
        result = layout(tokens)
    except Exception as e:   # noqa: BLE001 - never break a package over formatting
        return sql, f"could not lay out: {type(e).__name__}"
    if _signature(tokenize(result)) != _signature(tokens):
        return sql, "the layout would change more than whitespace and keyword case"
    return result, ""
