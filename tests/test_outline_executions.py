"""Package outlines and failure reports, using packages built by the lab seed (tests/fixtures)."""
from pathlib import Path

from ssisbliss.ssis.executions import report
from ssisbliss.ssis.fake import FakeCatalog, ispac
from ssisbliss.ssis.outline import outline

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIX / f"{name}.dtsx").read_bytes()


def test_outline_follows_precedence_not_document_order():
    tasks = outline(fixture("Fetch_Prices"))["tasks"]
    assert [t["name"] for t in tasks] == ["Log start", "Call price API", "Log rows"]
    assert tasks[1]["runs_after"] == [{"after": "Log start", "on": "success"}]


def test_outline_describes_sql_python_and_powershell_tasks():
    fetch = outline(fixture("Fetch_Prices"))
    assert fetch["tasks"][0]["kind"] == "sql" and fetch["tasks"][0]["connection"] == "Target"
    assert "INSERT dbo.LoadLog" in fetch["tasks"][0]["sql"]
    api = fetch["tasks"][1]
    assert (api["kind"], api["runs"], api["script"]) == ("process", "python", r"C:\lab\scripts\fetch_prices.py")
    archive = outline(fixture("Archive_Files"))["tasks"][0]
    assert (archive["runs"], archive["script"]) == ("powershell", r"C:\lab\scripts\archive_files.ps1")
    assert [t["name"] for t in outline(fixture("Load_Prices"))["tasks"]] == [
        "Truncate staging", "Load", "Check rows", "Log done"]
    assert "$Project::TargetDb" in fetch["connections"][0]["expression"]


def _catalog_with_failures() -> FakeCatalog:
    cat = FakeCatalog()
    cat.add_project("BRONZE_API", "IMPORTAPI", ispac("IMPORTAPI", {
        "Fetch_Prices.dtsx": fixture("Fetch_Prices"), "Load_Prices.dtsx": fixture("Load_Prices")},
        parameters=["TargetServer", "TargetDb", "LoadMode"]))
    base = {"folder_name": "BRONZE_API", "project_name": "IMPORTAPI", "status": 4,
            "environment_folder_name": None, "environment_name": "Default", "caller_name": "NT SERVICE\\SQLAgent",
            "executed_as_name": "NT SERVICE\\SQLAgent", "start_time": "2099-01-01T07:17:45.0+00:00",
            "end_time": "2099-01-01T07:17:46.5+00:00", "duration_s": 1, "server_name": "LAB\\SQL01",
            "machine_name": "LABHOST"}
    cat.execution_rows = [
        {**base, "execution_id": 68, "package_name": "Fetch_Prices.dtsx"},
        {**base, "execution_id": 70, "package_name": "Load_Prices.dtsx"},
        {**base, "execution_id": 71, "package_name": "Load_Prices.dtsx", "status": 7}]
    ev = {"event_message_id": 1, "message_time": "", "message_type": 120, "message_source_type": 40,
          "subcomponent_name": None, "execution_path": ""}
    cat.event_rows = {
        68: [{**ev, "message_source_name": "Call price API", "package_name": "Fetch_Prices.dtsx",
              "package_path": "\\Package\\Call price API",
              "message": 'Call price API:Error: In Executing "C:\\Python312\\python.exe" ... exit code was "2"'},
             {**ev, "message_type": 130, "message_source_name": "Call price API", "package_name": "Fetch_Prices.dtsx",
              "package_path": "\\Package\\Call price API", "message": "Call price API:The task, 'Call price API', failed."}],
        70: [{**ev, "message_source_name": "Load", "package_name": "Load_Prices.dtsx", "package_path": "\\Package\\Load",
              "message": 'Load:Error: ... Failed to update database "GOLD_ARCHIVE" because the database is read-only.'}]}
    cat.parameter_rows = {70: [
        {"object_type": 20, "parameter_name": "TargetDb", "parameter_value": "GOLD_ARCHIVE", "sensitive": False,
         "runtime_override": True},
        {"object_type": 20, "parameter_name": "TargetServer", "parameter_value": "LAB\\SQL01", "sensitive": False,
         "runtime_override": False},
        {"object_type": 20, "parameter_name": "LoadMode", "parameter_value": "Full", "sensitive": False,
         "runtime_override": False}]}
    return cat


def test_failed_process_task_names_the_command_and_how_to_see_its_error():
    cat = _catalog_with_failures()
    [e] = report(cat, cat.executions(execution_id=68))
    [t] = e["failed_tasks"]
    assert (t["task"], t["runs"], t["script"]) == ("Call price API", "python", r"C:\lab\scripts\fetch_prices.py")
    assert len(t["messages"]) == 1 and "exit code" in t["messages"][0]   # the generic "task failed" line is dropped
    assert "LABHOST" in t["hint"] and "fetch_prices.py" in t["hint"]


def test_failed_sql_task_shows_statement_and_the_parameters_its_connection_used():
    cat = _catalog_with_failures()
    [e] = report(cat, cat.executions(execution_id=70))
    [t] = e["failed_tasks"]
    assert t["kind"] == "sql" and t["sql"].startswith("INSERT dbo.LoadLog")
    assert t["connection_parameters"] == {"TargetDb": "GOLD_ARCHIVE (overridden for this run)", "TargetServer": "LAB\\SQL01"}
    assert t["database"] == "gold_archive"      # its connection's database with this run's values
    # says where the override lives, so the fix goes to the job step and not the package
    note = t["override_note"]
    assert note.startswith("TargetDb came from the caller (NT SERVICE\\SQLAgent)")
    assert '/Par "$Project::TargetDb"' in note and "job step" in note and "ssisbliss jobs" in note
    assert "TargetServer" not in note


def test_no_override_note_when_the_connection_uses_deployed_values():
    cat = _catalog_with_failures()
    for p in cat.parameter_rows[70]:
        p["runtime_override"] = False
    [e] = report(cat, cat.executions(execution_id=70))
    assert "override_note" not in e["failed_tasks"][0]


def test_succeeded_executions_have_no_failure_details_and_failed_filter_works():
    cat = _catalog_with_failures()
    rows = report(cat, cat.executions())
    assert [r["status"] for r in rows] == ["succeeded", "failed", "failed"]
    assert "failed_tasks" not in rows[0]
    assert [r["execution_id"] for r in report(cat, cat.executions(statuses=[4, 6]))] == [70, 68]


def test_failed_execution_without_messages_says_why(tmp_path):
    cat = _catalog_with_failures()
    cat.event_rows = {}
    [e] = report(cat, cat.executions(execution_id=68))
    assert e["failed_tasks"] == [] and "logging level" in e["note"]
