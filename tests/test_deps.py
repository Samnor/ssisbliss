"""ssisbliss deps: the graph of packages around one, from your files or the deployed project."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

CLIMB = Path(__file__).resolve().parents[1] / "climb"
if not CLIMB.is_dir():   # the source archive ships the package's tests, not the climb
    pytest.skip("climb/ is only in the repository", allow_module_level=True)
sys.path.insert(0, str(CLIMB))
from team import sql_world  # noqa: E402
from world import ME, task_package  # noqa: E402

from ssisbliss.ssis import deps  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def cli(cwd, *args, root=None):
    env = {"PATH": f"{REPO / '.venv' / 'bin'}:/usr/bin:/bin", "SSISBLISS_FAKE_USER": ME, "HOME": str(root or cwd)}
    if root is not None:
        env |= {"SSISBLISS_FAKE_ROOT": str(root.root / "servers"), "SSISBLISS_STATE_DIR": str(root.state_dir),
                "HOME": str(root.root)}
    p = subprocess.run([str(REPO / ".venv" / "bin" / "ssisbliss"), *args], cwd=cwd, env=env,
                       capture_output=True, text=True)
    return p.returncode, (json.loads(p.stdout) if p.stdout.startswith("{") else p.stdout)


def chain(tmp_path, extra: dict[str, bytes] | None = None) -> Path:
    """Extract -> Clean -> Report by tables, Master runs Extract and Clean, Other stands apart, Audit reads
    a table Report writes and SQL built at run time."""
    pkgs = {
        "Extract.dtsx": task_package("Extract.dtsx", sql=["INSERT INTO stg.raw (a) SELECT a FROM src.orders"]),
        "Clean.dtsx": task_package("Clean.dtsx", sql=["INSERT INTO stg.clean (a) SELECT a FROM stg.raw"]),
        "Report.dtsx": task_package("Report.dtsx", sql=["INSERT INTO rpt.daily (a) SELECT a FROM stg.clean",
                                                       "INSERT INTO rpt.weekly (a) SELECT a FROM stg.clean"]),
        "Master.dtsx": task_package("Master.dtsx", calls=["Extract.dtsx", "Clean.dtsx"]),
        "Other.dtsx": task_package("Other.dtsx", sql=["INSERT INTO x.y (a) SELECT a FROM x.z"]),
        **(extra or {}),
    }
    d = tmp_path / "proj"
    d.mkdir()
    for n, data in pkgs.items():
        (d / n).write_bytes(data)
    return d


def test_graph_follows_links_both_ways_and_leaves_unrelated_packages_out(tmp_path):
    d = chain(tmp_path)
    code, doc = cli(tmp_path, "deps", str(d / "Clean.dtsx"))
    assert code == 0 and doc["ok"] and doc["package"] == "Clean.dtsx"
    roles = {n["package"]: n["role"] for n in doc["nodes"]}
    assert roles == {"Extract.dtsx": "upstream", "Master.dtsx": "upstream", "Clean.dtsx": "this",
                     "Report.dtsx": "downstream"}                                       # Other is not linked
    assert [n["package"] for n in doc["nodes"]][-2:] == ["Clean.dtsx", "Report.dtsx"]   # upstream first
    edges = {(e["from"], e["to"], e["kind"]) for e in doc["edges"]}
    assert edges == {("Extract.dtsx", "Clean.dtsx", "tables"), ("Clean.dtsx", "Report.dtsx", "tables"),
                     ("Master.dtsx", "Extract.dtsx", "runs"), ("Master.dtsx", "Clean.dtsx", "runs")}
    report = next(e for e in doc["edges"] if e["to"] == "Report.dtsx")
    assert report["tables"] == ["stg.clean"] and report["via"] == "Report.dtsx reads stg.clean, which Clean.dtsx writes"
    assert doc["order"].index("Master.dtsx") < doc["order"].index("Extract.dtsx") < doc["order"].index("Clean.dtsx")
    assert doc["mermaid"].startswith("flowchart LR") and "|\"stg.clean\"|" in doc["mermaid"]


def test_direction_and_depth_limit_the_graph(tmp_path):
    d = chain(tmp_path)
    _, down = cli(d, "deps", "Extract", "--direction", "down")
    assert {n["package"] for n in down["nodes"]} == {"Extract.dtsx", "Clean.dtsx", "Report.dtsx"}
    _, near = cli(d, "deps", "Report.dtsx", "--depth", "1")
    assert {n["package"] for n in near["nodes"]} == {"Clean.dtsx", "Report.dtsx"}
    _, up = cli(d, "deps", "Report.dtsx", "--direction", "up")
    assert {n["package"]: n["steps"] for n in up["nodes"]} == {
        "Master.dtsx": 2, "Extract.dtsx": 2, "Clean.dtsx": 1, "Report.dtsx": 0}


def test_mermaid_and_dot_print_the_graph(tmp_path):
    d = chain(tmp_path)
    code, text = cli(d, "deps", "Clean.dtsx", "--format", "mermaid")
    assert code == 0 and text.startswith("flowchart LR\n") and "==>|\"runs\"|" in text and "class n" in text
    code, text = cli(d, "deps", "Clean.dtsx", "--format", "dot")
    assert code == 0 and text.startswith("digraph ssis {") and '"Master.dtsx" -> "Extract.dtsx"' in text


def test_sql_built_at_run_time_and_missing_children_are_shown_not_hidden(tmp_path):
    # its first task's SQL comes from a variable; the second reads what Report writes
    dynamic = task_package("Audit.dtsx", sql=["SELECT 1", "INSERT INTO a.log (a) SELECT a FROM rpt.daily"]).replace(
        b"SQLTask:SqlStatementSource=", b'SQLTask:SqlStmtSourceType="Variable" SQLTask:SqlStatementSource=', 1)
    d = chain(tmp_path, {"Audit.dtsx": dynamic,
                         "Parent.dtsx": task_package("Parent.dtsx", calls=["Report.dtsx", "Gone.dtsx"])})
    _, doc = cli(d, "deps", "Report.dtsx", "--direction", "down")
    by = {n["package"]: n for n in doc["nodes"]}
    assert "unknown_sql" in by["Audit.dtsx"] and "Audit.dtsx" in doc["note"]
    _, doc = cli(d, "deps", "Parent.dtsx", "--direction", "down", "--depth", "1")
    assert {n["package"]: n.get("missing") is not None for n in doc["nodes"]} == {
        "Parent.dtsx": False, "Report.dtsx": False, "Gone.dtsx": True}


def test_packages_that_feed_each_other_are_cycles_not_an_order(tmp_path):
    loop = {"Ping.dtsx": task_package("Ping.dtsx", sql=["INSERT INTO a.ping (a) SELECT a FROM a.pong"]),
            "Pong.dtsx": task_package("Pong.dtsx", sql=["INSERT INTO a.pong (a) SELECT a FROM a.ping"])}
    d = chain(tmp_path, loop)
    _, doc = cli(d, "deps", "Ping.dtsx")
    assert "order" not in doc and doc["cycles"] == ["Ping.dtsx", "Pong.dtsx"]


def test_a_package_that_is_not_there_is_refused_with_the_list(tmp_path):
    d = chain(tmp_path)
    code, doc = cli(d, "deps", "Nope.dtsx")
    assert code == 2 and doc["error_kind"] == "refused" and "Clean.dtsx" in doc["error"]
    code, doc = cli(tmp_path, "deps", "Clean.dtsx")       # no packages here
    assert code == 2 and "no .dtsx files" in doc["error"]


def test_deps_reads_the_deployed_project(tmp_path):
    w = sql_world(tmp_path / "w")
    code, doc = cli(w.work, "deps", "Stage_Customer", "--env", "dev", "--folder", "STAGE", "--project", "STAGE", root=w)
    assert code == 0 and doc["source"] == "dev: STAGE/STAGE (deployed)"
    assert [(e["from"], e["to"]) for e in doc["edges"]] == [("Stage_Customer.dtsx", "Stage_SalesOrder.dtsx")]


def test_links_scale_with_tables_not_package_pairs():
    n = 2000
    pkgs = {f"P{i:04d}.dtsx": deps.Facts(f"P{i:04d}.dtsx", reads={f"t.t{i - 1}", "dim.date"}, writes={f"t.t{i}"})
            for i in range(n)}
    t = time.perf_counter()
    g = deps.graph("P1000.dtsx", pkgs)
    assert time.perf_counter() - t < 2.0
    assert len(g["nodes"]) == n and g["order"][0] == "P0000.dtsx" and len(g["edges"]) == n - 1


# ------------------------------------------------------------ medallion: a database per layer --

CM = ('<DTS:ConnectionManagers><DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Target]" '
      'DTS:ObjectName="Target" DTS:DTSID="{00000000-0000-0000-0000-000000000001}" DTS:CreationName="OLEDB">'
      '{expression}<DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Data Source=localhost;'
      'Initial Catalog={static};Provider=MSOLEDBSQL;Integrated Security=SSPI;" /></DTS:ObjectData>'
      '</DTS:ConnectionManager></DTS:ConnectionManagers>')
BY_PARAMETER = ('<DTS:PropertyExpression DTS:Name="ConnectionString">"Data Source=localhost;Initial Catalog=" + '
                '@[$Project::TargetDb] + ";Provider=MSOLEDBSQL;Integrated Security=SSPI;"</DTS:PropertyExpression>')


def layer_package(name: str, sql: list[str], expression: str = BY_PARAMETER, static: str = "DESIGN") -> bytes:
    """A package whose connection's database comes from $Project::TargetDb (the static string says DESIGN)."""
    data = task_package(name, sql=sql).decode()
    cm = CM.replace("{expression}", expression).replace("{static}", static)
    return data.replace("<DTS:Executables>", cm + "<DTS:Executables>", 1).encode()


def params(**values) -> str:
    items = "".join(f'<SSIS:Parameter SSIS:Name="{k}"><SSIS:Properties><SSIS:Property SSIS:Name="Sensitive">0'
                    f'</SSIS:Property><SSIS:Property SSIS:Name="Value">{v}</SSIS:Property></SSIS:Properties>'
                    f'</SSIS:Parameter>' for k, v in values.items())
    return f'<?xml version="1.0"?><SSIS:Parameters xmlns:SSIS="www.microsoft.com/SqlServer/SSIS">{items}</SSIS:Parameters>'


def medallion(tmp_path) -> dict[str, Path]:
    layers = {
        "BRONZE": {"Load_Customer.dtsx": ["INSERT INTO dbo.Customer (Id) SELECT Id FROM ext.CustomerFeed"],
                   "Check_Customer.dtsx": ["INSERT INTO dbo.Checks (Id) SELECT Id FROM dbo.Customer"]},
        "SILVER": {"Load_Customer.dtsx": ["INSERT INTO dbo.Customer (Id) SELECT Id FROM BRONZE.dbo.Customer"],
                   "Load_Order.dtsx": ["INSERT INTO dbo.[Order] (Id) SELECT o.Id FROM BRONZE.dbo.[Order] o "
                                       "JOIN dbo.Customer c ON c.Id = o.CustomerId"]},
        "GOLD": {"Dim_Customer.dtsx": ["INSERT INTO dbo.DimCustomer (Id) SELECT Id FROM SILVER.dbo.Customer"],
                 "Load_Customer.dtsx": ["INSERT INTO dbo.Customer (Id) SELECT Id FROM dbo.DimCustomer"]},
    }
    out = {}
    for layer, pkgs in layers.items():
        d = tmp_path / layer
        d.mkdir()
        (d / "Project.params").write_text(params(TargetDb=layer))
        (d / f"{layer}.dtproj").write_text("<Project />")
        for n, sql in pkgs.items():
            (d / n).write_bytes(layer_package(n, sql))
        out[layer] = d
    return out


def test_layers_in_separate_databases_link_through_their_real_tables_only(tmp_path):
    m = medallion(tmp_path)
    code, doc = cli(tmp_path, "deps", str(m["SILVER"] / "Load_Customer.dtsx"), "--dir", str(m["BRONZE"]),
                    "--dir", str(m["GOLD"]))
    assert code == 0 and doc["package"] == "SILVER/Load_Customer.dtsx" and doc["projects"] == ["BRONZE", "GOLD", "SILVER"]
    edges = {(e["from"], e["to"]): e["tables"] for e in doc["edges"]}
    # bronze's dbo.Customer feeds silver's; silver's feeds the silver order load and gold; gold's own dbo.Customer
    # (written by GOLD/Load_Customer) and bronze's check of bronze.dbo.Customer are not silver's business
    assert edges == {("BRONZE/Load_Customer.dtsx", "SILVER/Load_Customer.dtsx"): ["bronze.dbo.customer"],
                     ("SILVER/Load_Customer.dtsx", "SILVER/Load_Order.dtsx"): ["silver.dbo.customer"],
                     ("SILVER/Load_Customer.dtsx", "GOLD/Dim_Customer.dtsx"): ["silver.dbo.customer"],
                     ("GOLD/Dim_Customer.dtsx", "GOLD/Load_Customer.dtsx"): ["gold.dbo.dimcustomer"]}
    assert not any(e.get("unsure") for e in doc["edges"]) and "unknown_database" not in str(doc["nodes"])
    assert {n["package"]: n["project"] for n in doc["nodes"]}["BRONZE/Load_Customer.dtsx"] == "BRONZE"
    assert doc["order"].index("BRONZE/Load_Customer.dtsx") < doc["order"].index("GOLD/Dim_Customer.dtsx")


def test_one_layer_alone_says_other_projects_are_not_read(tmp_path):
    m = medallion(tmp_path)
    _, doc = cli(tmp_path, "deps", str(m["SILVER"] / "Load_Customer.dtsx"))
    assert [(e["from"], e["to"]) for e in doc["edges"]] == [("Load_Customer.dtsx", "Load_Order.dtsx")]
    assert "--dir for each project folder" in doc["note"]


def test_a_name_in_several_layers_needs_its_path_or_project(tmp_path):
    m = medallion(tmp_path)
    dirs = [a for layer in ("BRONZE", "SILVER", "GOLD") for a in ("--dir", str(m[layer]))]
    code, doc = cli(tmp_path, "deps", "Load_Customer", *dirs)
    assert code == 2 and "several projects" in doc["error"] and "GOLD/Load_Customer.dtsx" in doc["error"]
    code, doc = cli(tmp_path, "deps", "Load_Customer", *dirs, "--project", "GOLD")
    assert code == 0 and doc["package"] == "GOLD/Load_Customer.dtsx"


def test_a_database_that_cannot_be_worked_out_gives_an_unsure_link(tmp_path):
    m = medallion(tmp_path)
    blind = layer_package("Report.dtsx", ["INSERT INTO dbo.Report (Id) SELECT Id FROM dbo.DimCustomer"],
                          expression='<DTS:PropertyExpression DTS:Name="ConnectionString">@[User::Conn]'
                                     '</DTS:PropertyExpression>')
    (m["GOLD"] / "Report.dtsx").write_bytes(blind)
    _, doc = cli(tmp_path, "deps", str(m["GOLD"] / "Report.dtsx"), "--direction", "up", "--depth", "1")
    report = next(n for n in doc["nodes"] if n["package"] == "Report.dtsx")
    assert report["unknown_database"] and "unknown_database" in doc["note"]
    edge = next(e for e in doc["edges"] if e["to"] == "Report.dtsx")
    assert edge["from"] == "Dim_Customer.dtsx" and edge["unsure"] == ["dbo.dimcustomer"]
    _, text = cli(tmp_path, "deps", str(m["GOLD"] / "Report.dtsx"), "--format", "mermaid")
    assert "-.->" in text


def test_a_deployed_parameter_bound_to_an_environment_names_the_database():
    sql = ["INSERT INTO dbo.Customer (Id) SELECT Id FROM BRONZE.dbo.Customer"]
    pkg = layer_package("Load_Customer.dtsx", sql)
    from ssisbliss.ssis.fake import ispac
    project = ispac("SILVER", {"Load_Customer.dtsx": pkg}, parameters=["TargetDb"])
    from ssisbliss.ssis.ispac import Ispac
    bindings = [{"object_type": 20, "parameter_name": "TargetDb", "value_type": "R",
                 "referenced_variable_name": "SilverDb", "sensitive": False, "value": None}]
    info = deps.ProjectInfo.from_ispac(Ispac(project), "", bindings, {"silverdb": "SILVER_PROD"})
    assert deps.facts("Load_Customer.dtsx", pkg, info).writes == {"silver_prod.dbo.customer"}
    literal = [{**bindings[0], "value_type": "V", "value": "SILVER_TEST"}]
    assert deps.facts("Load_Customer.dtsx", pkg, deps.ProjectInfo.from_ispac(Ispac(project), "", literal)).writes == \
        {"silver_test.dbo.customer"}
    unknown = deps.ProjectInfo.from_ispac(Ispac(project), "", bindings, {})       # environment not readable
    assert deps.facts("Load_Customer.dtsx", pkg, unknown).writes == {"?.dbo.customer"}


def test_deps_reads_several_deployed_projects(tmp_path):
    from ssisbliss.ssis.fake import FakeCatalog, ispac
    m = medallion(tmp_path)
    w = sql_world(tmp_path / "w")
    cat = FakeCatalog.load(w.root / "servers" / "dev")
    for layer in ("BRONZE", "SILVER", "GOLD"):
        cat.add_project(layer, layer, ispac(layer, {f.name: f.read_bytes() for f in m[layer].glob("*.dtsx")},
                                            parameters=["TargetDb"]),
                        bindings=[{"object_type": 20, "object_name": layer, "parameter_name": "TargetDb",
                                   "value_type": "V", "sensitive": False, "value": layer,
                                   "referenced_variable_name": None}])
    cat.save()
    code, doc = cli(w.work, "deps", "Load_Customer", "--env", "dev", "--folder", "SILVER", "--project", "SILVER",
                    "--also", "BRONZE/BRONZE", "--also", "GOLD/GOLD", root=w)
    assert code == 0 and doc["package"] == "SILVER/Load_Customer.dtsx", doc
    assert ("BRONZE/Load_Customer.dtsx", "SILVER/Load_Customer.dtsx") in {(e["from"], e["to"]) for e in doc["edges"]}
    assert ("SILVER/Load_Customer.dtsx", "GOLD/Dim_Customer.dtsx") in {(e["from"], e["to"]) for e in doc["edges"]}
    code, doc = cli(w.work, "deps", "Load_Customer", "--env", "dev", "--folder", "SILVER", "--project", "SILVER",
                    "--all-projects", root=w)
    assert code == 0 and set(doc["projects"]) >= {"BRONZE", "SILVER", "GOLD", "STAGE"}
