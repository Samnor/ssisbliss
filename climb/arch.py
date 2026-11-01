#!/usr/bin/env python3
"""Hill climb on the architecture: does a design change make the code cheaper to change?

Clean import graphs prove nothing on their own, so the score is change cost: small coding agents get a fresh
clone of the repository and a small feature request, and tests they never see grade the result through the
CLI (so a module may move without a test noticing). Structure is measured too, as a second opinion.

    uv run python climb/arch.py structure                   # the structure numbers of this checkout
    uv run python climb/arch.py evaluate --label base       # change cost at HEAD, in climb/runs/arch/<label>/
    uv run python climb/arch.py compare new --best base     # the keep/revert verdict

A run scores 100 when the hidden tests and the regression tests pass, minus one point per tool call (at most
100): a change that lets agents get there in fewer steps counts, a broken result does not. The rule, fixed
before the first measurement (its thresholds never changed):
- guards, outside this script: all tests, the model checker, the benchmarks, and `commands` and `guide`
  byte-identical (a design change must not change what users see);
- kept when agents do better: gain >= MIN_GAIN points per run and z >= Z, paired by agent and task;
- kept on structure alone when agents are no worse (z >= -NOT_WORSE) and structure is clearly better;
  the report says so, because that is no measured value;
- reverted otherwise.

Amended after the first comparison, with both sides measured again: the step cap was 50, which hid a run
going from 76 steps to 28; and new-command's prompt did not say `package` is the file name, so a run that
wrote Load_Customer failed a test expecting Load_Customer.dtsx.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))

from climb import paired  # noqa: E402
from run import parse_codex  # noqa: E402

AGENTS = ["claude:haiku", "codex:gpt-6-luna"]
MIN_GAIN, Z, NOT_WORSE = 5.0, 1.64, 1.0
STEP_CAP = 100
REGRESSION = ["tests/test_contract.py", "tests/test_lint.py", "tests/test_design.py", "tests/test_deps.py",
              "tests/test_model.py", "tests/test_outline_executions.py", "tests/test_columns.py"]
SYSTEM = """You are working on the ssisbliss repository in the current folder: a Python CLI for SSIS catalogs and
SQL Agent. Its virtualenv is ready: run tests with `.venv/bin/python -m pytest -q <files>` and the tool with
`.venv/bin/ssisbliss`. Make the change the developer asks for, with tests. Do not commit. You cannot ask questions."""
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(.venv/bin/python:*)", "Bash(.venv/bin/ssisbliss:*)", "Bash(git:*)",
         "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(sed:*)", "Bash(head:*)", "Bash(find:*)", "Bash(wc:*)"]

HEADER = '''import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "climb"))
import depscases  # noqa: E402
from world import ME  # noqa: E402


def cli(w, *args, cwd=None):
    env = {"PATH": "/usr/bin:/bin", "SSISBLISS_FAKE_USER": ME, "HOME": str(cwd or "/tmp")}
    if w is not None:
        env |= {"SSISBLISS_FAKE_ROOT": str(w.root / "servers"), "SSISBLISS_STATE_DIR": str(w.state_dir), "HOME": str(w.root)}
    p = subprocess.run([str(REPO / ".venv" / "bin" / "ssisbliss"), *args], cwd=cwd or w.work, env=env,
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)
'''

TASKS = {
    "lint-rule": ("Add a lint rule SSB522 named `copy-onto-itself`, severity warn: a data flow whose OLE DB source and "
                  "destination use the same connection manager and the same table (it reads and writes one table). "
                  "`ssisbliss lint --rules` must list it.", '''
def same_connection(data):
    for attr in (b"connectionManagerID", b"connectionManagerRefId"):
        data = data.replace(attr + b'="Package.ConnectionManagers[Source]"', attr + b'="Package.ConnectionManagers[Target]"')
    return data


def rules(tmp_path, data):
    f = tmp_path / "P.dtsx"
    f.write_bytes(data)
    _, doc = cli(None, "lint", str(f), cwd=tmp_path)
    return [x["rule"] for x in doc["findings"]]


def test_reading_and_writing_one_table_on_one_connection(tmp_path):
    flow = depscases.layer_package("P.dtsx", flow=("[dbo].[Customer]", "[dbo].[Customer]"))
    assert "SSB522" in rules(tmp_path, same_connection(flow))


def test_other_tables_or_other_connections_are_fine(tmp_path):
    two = depscases.layer_package("P.dtsx", flow=("[dbo].[Customer]", "[dbo].[Customer]"))
    assert "SSB522" not in rules(tmp_path, two)
    other = depscases.layer_package("P.dtsx", flow=("[dbo].[Customer]", "[dbo].[DimCustomer]"))
    assert "SSB522" not in rules(tmp_path, same_connection(other))


def test_it_is_listed(tmp_path):
    assert "SSB522" in json.dumps(cli(None, "lint", "--rules", cwd=tmp_path)[1])
'''),
    "new-command": ("Add a read-only command `ssisbliss connections`: for every package in a project, its connection "
                    "managers with their database. From your files with `--dir FOLDER`, or from the deployed project "
                    "with --env/--folder/--project. JSON: `packages`, a list of {package, connections}, each connection "
                    "{name, type, database} (database null when it cannot be worked out), sorted by package; `package` is the "
                    "file name, e.g. Load_Customer.dtsx.", '''
def test_deployed_project_uses_the_deployed_values(tmp_path):
    w = depscases.medallion_world(tmp_path / "w")
    code, doc = cli(w, "connections", "--env", "prod", "--folder", "SILVER", "--project", "SILVER")
    assert code == 0 and doc["ok"] and [p["package"] for p in doc["packages"]] == ["Load_Customer.dtsx", "Load_Order.dtsx"]
    conns = {c["name"]: c for c in doc["packages"][0]["connections"]}
    assert conns["Source"]["database"] == "bronze_prod" and conns["Target"]["database"] == "silver_prod"
    assert conns["Source"]["type"] == "OLEDB"


def test_your_files(tmp_path):
    w = depscases.medallion_world(tmp_path / "w")
    code, doc = cli(w, "connections", "--dir", "GOLD")
    assert code == 0 and [p["package"] for p in doc["packages"]] == ["Dim_Customer.dtsx", "Load_Customer.dtsx", "Report_Customer.dtsx"]
    assert {c["database"] for p in doc["packages"] for c in p["connections"]} == {"dw_local"}


def test_it_is_a_listed_command(tmp_path):
    assert "connections" in json.dumps(cli(None, "commands", cwd=tmp_path)[1])
'''),
    "catalog-query": ("`ssisbliss inspect --env ENV` lists projects. Add `folders` to its output when it has no --folder: "
                      "every catalog folder as {name, projects}, the number of projects in it, sorted by name, including "
                      "folders without projects. It has to work on real servers (SSISDB) as well as the fake catalog.", '''
import re  # noqa: E402

from ssisbliss.ssis.fake import FakeCatalog  # noqa: E402


def test_folders_with_their_project_counts_also_empty_ones(tmp_path):
    w = depscases.medallion_world(tmp_path / "w")
    c = FakeCatalog.load(w.server_dir("dev"))
    c.folders.add("ARCHIVE")
    c.save()
    code, doc = cli(w, "inspect", "--env", "dev")
    assert code == 0 and doc["folders"] == [{"name": "ARCHIVE", "projects": 0}, {"name": "BRONZE", "projects": 1},
                                            {"name": "GOLD", "projects": 1}, {"name": "SILVER", "projects": 1},
                                            {"name": "STAGE", "projects": 1}]


def test_a_real_server_is_asked_too():
    text = "".join(f.read_text() for f in (REPO / "src").rglob("*.py") if f.name != "fake.py")
    assert re.search(r"(?i)from\\s+catalog\\.folders", text)
'''),
    "contract-field": ("Every command's JSON document should say which command made it: a top-level `command` with the "
                       "subcommand's name, e.g. \"deps\", on success and on refusals, from the CLI and over the MCP server. "
                       "Commands that print plain text stay as they are.", '''
def test_cli_success_and_refusal(tmp_path):
    w = depscases.medallion_world(tmp_path / "w")
    code, doc = cli(w, "deps", "SILVER/Load_Order.dtsx")
    assert code == 0 and doc["command"] == "deps"
    code, doc = cli(w, "deps", "SILVER/Nope.dtsx")
    assert code == 2 and doc["command"] == "deps"
    code, doc = cli(w, "versions", "--env", "dev", "--folder", "STAGE", "--project", "STAGE")
    assert code == 0 and doc["command"] == "versions"


def test_over_mcp(tmp_path):
    w = depscases.medallion_world(tmp_path / "w")
    calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                           "clientInfo": {"name": "t", "version": "1"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "deps", "arguments": {"package": "SILVER/Load_Order.dtsx"}}},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "versions", "arguments": {"env": "dev", "folder": "STAGE", "project": "STAGE"}}}]
    env = {"PATH": "/usr/bin:/bin", "SSISBLISS_FAKE_USER": ME, "HOME": str(w.root), "SSISBLISS_FAKE_ROOT": str(w.root / "servers"),
           "SSISBLISS_STATE_DIR": str(w.state_dir)}
    p = subprocess.run([str(REPO / ".venv" / "bin" / "ssisbliss"), "mcp"], cwd=w.work, env=env, capture_output=True, text=True,
                       input="".join(json.dumps(c) + "\\n" for c in calls))
    replies = {r["id"]: r for r in map(json.loads, p.stdout.splitlines()) if "id" in r}
    assert json.loads(replies[2]["result"]["content"][0]["text"])["command"] == "deps"
    assert json.loads(replies[3]["result"]["content"][0]["text"])["command"] == "versions"
'''),
}


# Written after AGENTS.md was, in parts of the code it gives no recipe for: does the map help beyond the tasks
# it was written next to? Measured with --held-out, never used to change anything.
HELD_OUT = {
    "deps-event-handlers": ("`ssisbliss deps` ignores the SQL in event handlers: a package whose OnError handler writes a table "
                            "that another package reads is not linked to it. Make deps read Execute SQL tasks in event "
                            "handlers too.", '''
HANDLER = (\'<DTS:EventHandlers><DTS:EventHandler DTS:refId="Package.EventHandlers[OnError]" DTS:EventName="OnError">\'
           \'<DTS:Executables><DTS:Executable DTS:refId="Package.EventHandlers[OnError]\\\\Log" \'
           \'DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="Log"><DTS:ObjectData>\'
           \'<SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" \'
           \'SQLTask:Connection="{00000000-0000-0000-0000-000000000001}" \'
           \'SQLTask:SqlStatementSource="INSERT INTO dbo.ErrorLog (a) VALUES (1)" /></DTS:ObjectData>\'
           \'</DTS:Executable></DTS:Executables></DTS:EventHandler></DTS:EventHandlers>\')


def test_a_table_written_by_an_event_handler_links_its_reader(tmp_path):
    d = tmp_path / "P"
    d.mkdir()
    writer = depscases.layer_package("Writer.dtsx", ["SELECT 1"]).decode()
    writer = writer.replace("</DTS:Executables>\\n</DTS:Executable>", "</DTS:Executables>" + HANDLER + "\\n</DTS:Executable>")
    assert HANDLER in writer
    (d / "Writer.dtsx").write_text(writer)
    (d / "Reader.dtsx").write_bytes(depscases.layer_package("Reader.dtsx", ["INSERT INTO dbo.Report (a) SELECT a FROM dbo.ErrorLog"]))
    code, doc = cli(None, "deps", str(d / "Reader.dtsx"), cwd=tmp_path)
    assert code == 0 and [(e["from"], e["to"]) for e in doc["edges"]] == [("Writer.dtsx", "Reader.dtsx")]
'''),
    "versions-limit": ("`ssisbliss versions` lists every version of a project. Add `--limit N`: only the newest N, and the "
                       "document says how many there are in all, as `total`.", '''
def test_limit_keeps_the_newest_and_says_the_total(tmp_path):
    from world import COLLEAGUE, World, package
    w = World.build(tmp_path / "w")
    for rev in (2, 3, 4):
        w.deploy_as("dev", COLLEAGUE, "Stage_Product.dtsx", package("Stage_Product.dtsx", rev))
    target = ["--env", "dev", "--folder", "STAGE", "--project", "STAGE"]
    _, every = cli(w, "versions", *target)
    code, doc = cli(w, "versions", *target, "--limit", "2")
    newest = sorted((v["object_version_lsn"] for v in every["versions"]), reverse=True)[:2]
    assert code == 0 and sorted((v["object_version_lsn"] for v in doc["versions"]), reverse=True) == newest
    assert doc["total"] == len(every["versions"]) >= 4
'''),
    "lint-summary": ("Add `ssisbliss lint --summary`: instead of the findings, `by_rule`: each rule found with its name, "
                     "severity and how many times, most frequent first. The exit code stays as without it.", '''
def test_summary_counts_rules_instead_of_listing_findings(tmp_path):
    f = tmp_path / "P.dtsx"
    f.write_bytes(depscases.layer_package("P.dtsx", ["DELETE FROM dbo.a", "DELETE FROM dbo.b", "SELECT * FROM dbo.c"]))
    plain_code, plain = cli(None, "lint", str(f), cwd=tmp_path)
    code, doc = cli(None, "lint", str(f), "--summary", cwd=tmp_path)
    assert code == plain_code and "findings" not in doc
    want = {}
    for x in plain["findings"]:
        want[x["rule"]] = want.get(x["rule"], 0) + 1
    assert {r["rule"]: r["count"] for r in doc["by_rule"]} == want
    assert [r["count"] for r in doc["by_rule"]] == sorted(want.values(), reverse=True)
    top = doc["by_rule"][0]
    assert top["rule"] == "SSB202" and top["severity"] == "warn" and top["name"] == "no-where"
'''),
}


# ----------------------------------------------------------------------------------- structure --

def _module(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return ".".join(["ssisbliss", *parts]).removesuffix(".__init__")


def _imports(tree: ast.Module, name: str, is_package: bool, known: set[str]) -> list[tuple[str, bool]]:
    """(module imported, at the top of the file?) for this package's modules."""
    out = []

    def add(target: str, top: bool) -> None:
        while target and target not in known:
            target = target.rpartition(".")[0]
        if target and target != name and target != "ssisbliss":
            out.append((target, top))

    def walk(node: ast.AST, top: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for a in child.names:
                    if a.name.startswith("ssisbliss"):
                        add(a.name, top)
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                if child.level:
                    here = name.split(".")[: None if is_package else -1]
                    here = here[: len(here) - (child.level - 1)]
                    base = ".".join([*here, *([child.module] if child.module else [])])
                if base.startswith("ssisbliss"):
                    for a in child.names:
                        add(f"{base}.{a.name}", top)
            walk(child, top and not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))
    walk(tree, True)
    return out


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of more than one module (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    found: list[list[str]] = []

    def go(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                go(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            part = []
            while True:
                w = stack.pop()
                on.discard(w)
                part.append(w)
                if w == v:
                    break
            if len(part) > 1:
                found.append(sorted(part))
    for v in sorted(graph):
        if v not in index:
            go(v)
    return found


def structure(repo: Path = REPO) -> dict:
    root = repo / "src" / "ssisbliss"
    files = {_module(f, root): f for f in sorted(root.rglob("*.py"))}
    graph: dict[str, set[str]] = {m: set() for m in files}
    late, long_functions, longest, shared = 0, 0, 0, []
    for m, f in files.items():
        tree = ast.parse(f.read_text())
        for target, top in _imports(tree, m, f.name == "__init__.py", set(files)):
            graph[m].add(target)
            late += not top
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = node.end_lineno - node.lineno + 1
                long_functions += n > 50
                longest = max(longest, n)
        for node in tree.body:   # state one module keeps for the whole process
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                shared += [f"{m}.{t.id}" for t in targets if isinstance(t, ast.Name) and not t.id.lstrip("_").isupper()
                           and not (node.value.elts if isinstance(node.value, (ast.List, ast.Set)) else node.value.keys)]
    fan_in = {m: sum(m in t for t in graph.values()) for m in graph}
    fans = {m: fan_in[m] + len(graph[m]) for m in graph}
    hub = max(fans, key=fans.get)
    cycles = _cycles(graph)
    return {"modules": len(files), "lines": sum(len(f.read_text().splitlines()) for f in files.values()),
            "modules_in_cycles": sum(map(len, cycles)), "cycles": cycles, "late_imports": late,
            "hub": hub.removeprefix("ssisbliss."), "hub_links": fans[hub], "mutable_globals": sorted(shared),
            "functions_over_50_lines": long_functions, "longest_function": longest}


def clearly_better(new: dict, best: dict) -> list[str]:
    """What is clearly better in `new`, or [] when something got clearly worse."""
    keys = {"modules_in_cycles": 2, "late_imports": 5, "hub_links": 3, "functions_over_50_lines": 2}
    worse = [k for k, step in keys.items() if new[k] >= best[k] + step]
    better = [f"{k} {best[k]} -> {new[k]}" for k, step in keys.items() if new[k] <= best[k] - step]
    if len(new["mutable_globals"]) < len(best["mutable_globals"]):
        better.append(f"mutable_globals {len(best['mutable_globals'])} -> {len(new['mutable_globals'])}")
    return [] if worse else better


# --------------------------------------------------------------------------------- change cost --

def _claude_stats(path: Path) -> dict:
    steps, cost, usage, final = 0, 0.0, 0, ""
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            steps += sum(p.get("type") == "tool_use" for p in ev.get("message", {}).get("content", []))
        elif ev.get("type") == "result":
            cost, final = ev.get("total_cost_usd") or 0.0, ev.get("result") or ""
            u = ev.get("usage") or {}
            usage = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens"))
    return {"steps": steps, "cost_usd": cost, "tokens": usage, "final": final}


def _codex_stats(path: Path) -> dict:
    t = parse_codex(path)
    edits = sum('"file_change"' in x for x in path.read_text().splitlines())
    return {"steps": len(t["commands"]) + edits, "cost_usd": 0.0, "final": t["final"],
            "tokens": sum(v for v in (t.get("tokens") or {}).values() if isinstance(v, int))}


def run_one(agent: str, task: str, i: int, commit: str, out: Path, budget: float) -> dict:
    d = out / agent.replace(":", "-") / f"{task}-{i}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    repo = d / "repo"
    subprocess.run(["git", "clone", "-q", str(REPO), str(repo)], check=True)
    subprocess.run(["git", "checkout", "-q", commit], cwd=repo, check=True)
    subprocess.run(["uv", "sync", "-q", "--locked"], cwd=repo, check=True)
    prompt, hidden = TASKS[task]
    backend, _, model = agent.partition(":")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "VIRTUAL_ENV")}
    if backend == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", model,
               "--append-system-prompt", SYSTEM, "--setting-sources", "project", "--max-budget-usd", str(budget),
               "--allowedTools", *TOOLS]
    else:
        cmd = ["codex", "exec", "-m", model, "--json", "--ephemeral", "--skip-git-repo-check", "-s", "workspace-write",
               "-c", 'shell_environment_policy.inherit="all"', f"{SYSTEM}\n\nTask: {prompt}"]
    started = time.time()
    try:
        with open(d / "transcript.jsonl", "w") as f:
            subprocess.run(cmd, cwd=repo, env=env, stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=1200)
    except subprocess.TimeoutExpired:
        pass
    took = round(time.time() - started)
    stats = (_claude_stats if backend == "claude" else _codex_stats)(d / "transcript.jsonl")
    diff = subprocess.run("git add -A -- src tests docs climb README.md CHANGELOG.md && git diff --cached --numstat",
                          shell=True, cwd=repo, capture_output=True, text=True).stdout
    numstat = [x.split("\t") for x in diff.splitlines() if x.strip()]
    (repo / "tests" / "test_hidden_task.py").write_text(HEADER + hidden)
    pyt = str(repo / ".venv" / "bin" / "python")
    h = subprocess.run([pyt, "-m", "pytest", "-q", "-x", "tests/test_hidden_task.py"], cwd=repo, capture_output=True, text=True)
    r = subprocess.run([pyt, "-m", "pytest", "-q", "-x", *REGRESSION], cwd=repo, capture_output=True, text=True)
    outcome = h.returncode == 0 and r.returncode == 0
    (d / "hidden.txt").write_text(h.stdout[-3000:] + "\n---\n" + r.stdout[-1500:])
    result = {"task": task, "agent": agent, "outcome": outcome, "hidden_ok": h.returncode == 0, "regression_ok": r.returncode == 0,
              "score": (100 if outcome else 0) - min(STEP_CAP, stats["steps"]), "steps": stats["steps"],
              "tokens": stats["tokens"], "cost_usd": stats["cost_usd"], "seconds": took, "files": len(numstat),
              "lines": sum(int(a) + int(b) for a, b, _ in numstat if a.isdigit() and b.isdigit()), "final": stats["final"][-800:]}
    (d / "grade.json").write_text(json.dumps(result, indent=1))
    shutil.rmtree(repo / ".venv", ignore_errors=True)
    return result


def evaluate(label: str, agents: list[str], repeats: int, jobs: int, budget: float, commit: str | None) -> dict:
    commit = commit or subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    out = CLIMB / "runs" / "arch" / label
    out.mkdir(parents=True, exist_ok=True)
    work = [(ag, t, i) for ag in agents for t in TASKS for i in range(repeats)]
    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(lambda x: run_one(*x, commit, out, budget), work))
    cards = {}
    for ag in agents:
        mine = [r for r in results if r["agent"] == ag]
        cards[ag] = {"by_scenario": {f"{r['task']}-{i}": r for i, r in enumerate(mine)},
                     "score": sum(r["score"] for r in mine), "outcomes": sum(r["outcome"] for r in mine), "runs": len(mine),
                     "steps": sum(r["steps"] for r in mine), "tokens": sum(r["tokens"] for r in mine),
                     "cost_usd": round(sum(r["cost_usd"] for r in mine), 2)}
    doc = {"label": label, "commit": commit, "cards": cards, "structure": structure_at(commit)}
    (out / "card.json").write_text(json.dumps(doc, indent=1))
    return doc


def structure_at(commit: str) -> dict:
    tmp = CLIMB / "runs" / "arch" / f".tree-{commit[:12]}"
    if not tmp.exists():
        tmp.mkdir(parents=True)
        tar = subprocess.run(["git", "archive", commit, "src"], cwd=REPO, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=tar, check=True)
    return structure(tmp)


def load(label: str) -> dict:
    return json.loads((CLIMB / "runs" / "arch" / label / "card.json").read_text())


def table(doc: dict) -> str:
    tasks = list(TASKS)
    lines = [f"## {doc['label']} ({doc['commit'][:8]})", "",
             "| agent | score | right | steps | tokens | cost | " + " | ".join(tasks) + " |",
             "|---|---|---|---|---|---|" + "---|" * len(tasks)]
    for ag, c in doc["cards"].items():
        per = {t: [r for r in c["by_scenario"].values() if r["task"] == t] for t in tasks}
        cells = [f"{sum(r['outcome'] for r in rs)}/{len(rs)} · {sum(r['steps'] for r in rs) / max(1, len(rs)):.0f}"
                 for rs in per.values()]
        lines.append(f"| {ag} | {c['score']} | {c['outcomes']}/{c['runs']} | {c['steps']} | {c['tokens']} | ${c['cost_usd']} | "
                     + " | ".join(cells) + " |")
    s = {k: v for k, v in doc["structure"].items() if k != "cycles"}
    return "\n".join(lines) + f"\n\nstructure: {json.dumps(s)}\ncycles: {doc['structure']['cycles']}\n"


def verdict(new: dict, best: dict) -> dict:
    stats = paired({ag: [c] for ag, c in new["cards"].items()}, best["cards"])
    better = clearly_better(new["structure"], best["structure"])
    if stats["gain"] >= MIN_GAIN and stats["z"] >= Z:
        return {"keep": True, "why": "agents did better", **stats, "structure": better}
    if stats["z"] >= -NOT_WORSE and better:
        return {"keep": True, "why": "structure alone, agents no worse: no measured value", **stats, "structure": better}
    return {"keep": False, "why": "not enough better", **stats, "structure": better}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("structure")
    e = sub.add_parser("evaluate")
    e.add_argument("--label", required=True)
    e.add_argument("--agents", default=",".join(AGENTS))
    e.add_argument("--repeats", type=int, default=2)
    e.add_argument("--jobs", type=int, default=6)
    e.add_argument("--budget", type=float, default=1.5)
    e.add_argument("--commit")
    e.add_argument("--task", action="append", help="only these tasks")
    e.add_argument("--held-out", action="store_true", help="the tasks written after the map")
    c = sub.add_parser("compare")
    c.add_argument("new")
    c.add_argument("--best", required=True)
    a = p.parse_args()
    if a.cmd == "structure":
        print(json.dumps(structure(), indent=1))
    elif a.cmd == "evaluate":
        if a.held_out:
            TASKS.clear()
            TASKS.update(HELD_OUT)
        for t in set(TASKS) - set(a.task or TASKS):
            del TASKS[t]
        print(table(evaluate(a.label, a.agents.split(","), a.repeats, a.jobs, a.budget, a.commit)))
    else:
        new, best = load(a.new), load(a.best)
        if next(iter(next(iter(new["cards"].values()))["by_scenario"])).rsplit("-", 1)[0] in HELD_OUT:
            TASKS.clear()
            TASKS.update(HELD_OUT)
        print(table(best), table(new), json.dumps(verdict(new, best), indent=1), sep="\n")


if __name__ == "__main__":
    main()
