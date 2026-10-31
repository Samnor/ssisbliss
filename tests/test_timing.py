"""`slow` (timing.py): which tasks made a run slower than usual; `history` marks slow steps."""
import json

from ssisbliss.agent.status import mark_slow
from ssisbliss.ssis import cli
from ssisbliss.ssis.fake import FakeCatalog, dtsx, ispac
from ssisbliss.ssis.timing import report


def run(execution_id, total_s, tasks, status=7, rows=None):
    """task_durations() rows: tasks {name: seconds} under \\Main\\Load (a container), plus the package row."""
    out = [{"execution_id": execution_id, "status": status, "start_time": f"2026-10-{execution_id % 28 + 1:02d}T02:00:00",
            "duration_s": total_s, "execution_path": "\\Main", "ms": total_s * 1000, "times": 1, "rows_sent": None}]
    for name, s in tasks.items():
        out.append({"execution_id": execution_id, "status": status, "start_time": out[0]["start_time"],
                    "duration_s": total_s, "execution_path": f"\\Main\\Load\\{name}", "ms": s * 1000, "times": 1,
                    "rows_sent": (rows or {}).get(name)})
    return out


USUAL = {"Truncate": 2, "Load Customer": 600, "Load Orders": 1200}


def history(last_customer_s, last_rows=None):
    rows = []
    for i in range(1, 6):
        rows += run(i, 1900 + i, dict(USUAL), rows={"Load Customer": 400_000})
    rows += run(6, 1900 - 600 + last_customer_s, {**USUAL, "Load Customer": last_customer_s},
                rows={"Load Customer": last_rows or 400_000})
    return rows


def test_the_task_that_took_the_extra_time_is_named():
    r = report(history(7800, last_rows=5_200_000))
    assert r["execution_id"] == 6 and r["usual"] == "31m43s" and r["compared_with"] == "median of 5 successful runs"
    first = r["slower_tasks"][0]
    assert first["task"] == "Load Customer" and first["took"] == "2h10m" and first["usual"] == "10m00s"
    assert first["why"] == "more rows than usual" and first["rows"] == "5,200,000 rows, usually 400,000"
    assert r["verdict"].startswith("2h31m (usually 31m43s): Load Customer took 2h10m, usually 10m00s (more rows")
    assert [x["task"] for x in r["slower_tasks"]] == ["Load Customer"]   # the \\Main and \\Main\\Load containers are not blamed


def test_a_usual_run_says_so():
    assert report(history(610))["verdict"].startswith("as usual")


def test_an_earlier_run_and_too_little_history():
    rows = history(7800)
    assert report(rows, 2)["verdict"].startswith("too few earlier successful runs")
    assert report(rows, 99)["verdict"] == "execution 99 is not a finished run of this package"
    assert report([])["verdict"].startswith("no finished runs")


def test_no_task_times_means_logging_level_none():
    rows = [r for r in history(7800) if r["execution_id"] != 6] + [
        {"execution_id": 6, "status": 7, "start_time": "x", "duration_s": 9000, "execution_path": None, "ms": None,
         "times": None, "rows_sent": None}]
    assert "logging level None" in report(rows)["verdict"]


def test_slow_command_by_execution_id(tmp_path, capsys):
    c = FakeCatalog()
    c.add_project("STAGE", "STAGE", ispac("STAGE", {"Main.dtsx": dtsx("Main.dtsx")}))   # deployed 2026-10-01
    c.execution_rows = [{"execution_id": 6, "folder_name": "STAGE", "project_name": "STAGE", "package_name": "Main.dtsx",
                         "status": 7}]
    c.task_rows = [{**r, "folder_name": "STAGE", "project_name": "STAGE", "package_name": "Main.dtsx"}
                   for r in history(7800)]
    c.save(tmp_path)
    assert cli.main(["slow", "--server", f"fake://{tmp_path}", "--id", "6"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["package"] == "STAGE\\STAGE\\Main.dtsx" and doc["slower_tasks"][0]["task"] == "Load Customer"
    assert doc["deployed_since_last_run"].startswith("no: STAGE was not deployed")
    assert cli.main(["slow", "--server", f"fake://{tmp_path}", "--id", "7"]) == 2
    capsys.readouterr()
    c.next_lsn = 129   # the fake's clock: lsn 130 is 2026-10-06T10:00, between run 5 and run 6
    c.someone_else_deploys("STAGE", "STAGE", "Main.dtsx", dtsx("Main.dtsx", revision=2))
    assert cli.main(["slow", "--server", f"fake://{tmp_path}", "--id", "6"]) == 0
    deploys = json.loads(capsys.readouterr().out)["deployed_since_last_run"]
    assert deploys == [{"lsn": 130, "by": "LAB\\bob", "at": "2026-10-06T10:00:00"}]


def test_history_marks_a_slow_step_and_points_at_slow():
    def job_run(step_s, execution_id):
        return {"start": "x", "outcome": "succeeded", "duration_s": step_s, "message": None,
                "steps": [{"step_id": 1, "name": "Load", "outcome": "succeeded", "start": "x", "duration_s": step_s,
                           "ssis_execution_id": execution_id}]}
    found = [job_run(9000, 6), job_run(1900, 5), job_run(1950, 4), job_run(1880, 3)]
    mark_slow(found)
    assert found[0]["steps"][0]["slower"] == "took 2h30m, usually 31m40s"
    assert found[0]["steps"][0]["next"] == "ssisbliss slow --id 6"
    assert all("slower" not in r["steps"][0] for r in found[1:])
