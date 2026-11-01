"""The output contract: one JSON document, `ok` exactly when the exit code is 0, `error_kind`, `next`."""
import json

from ssisbliss.ssis import cli
from ssisbliss.ssis.contract import EXIT_KINDS, EXIT_USAGE


def run(capsys, *argv):
    code = cli.main(list(argv))
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is (code == 0) and (code == 0 or doc["error_kind"] == EXIT_KINDS[code][0])
    return code, doc


def test_usage_errors_are_json_and_point_at_help(capsys):
    code, doc = run(capsys, "deploy", "--env", "dev")
    assert code == EXIT_USAGE and "did you mean: deploy-project, deploy-packages" in doc["error"]
    assert doc["next"][:2] == ["ssisbliss deploy-project --help", "ssisbliss deploy-packages --help"]
    code, doc = run(capsys, "rollback", "--env", "dev")
    assert code == EXIT_USAGE and "--to-lsn" in doc["error"] and doc["next"] == ["ssisbliss rollback --help"]
    code, doc = run(capsys, "lint", "--bogus")
    assert code == EXIT_USAGE and doc["next"] == ["ssisbliss lint --help"]
    code, doc = run(capsys)
    assert code == EXIT_USAGE and doc["next"] == ["ssisbliss guide", "ssisbliss commands"]


def test_refused_input_is_kind_refused(capsys, tmp_path):
    code, doc = run(capsys, "lint", str(tmp_path / "missing"))
    assert code == 2 and doc["error_kind"] == "refused" and "does not exist" in doc["error"]


def test_commands_lists_every_command_and_what_it_may_change(capsys):
    code, doc = run(capsys, "commands")
    sub = next(a for a in cli.parser()._actions if a.dest == "cmd")
    assert code == 0 and set(doc["commands"]) == set(sub.choices)
    assert doc["commands"]["deploy-packages"]["changes_server"] and not doc["commands"]["history"]["changes_server"]
    assert doc["commands"]["pull"]["writes_files"] and set(doc["exit_codes"]) == {"0", "1", "2", "3", "4", "5"}
    code, doc = run(capsys, "commands", "deploy-project")
    args = {a["name"]: a for a in doc["arguments"]}
    assert args["only"]["type"] == "array" and args["project_items"]["choices"] == ["keep", "update"]
    assert args["allow_new"]["type"] == "boolean" and doc["usage"].startswith("usage: ssisbliss deploy-project")
    code, doc = run(capsys, "commands", "deploy")
    assert code == 2 and "no command 'deploy'" in doc["error"]


def test_guide_has_the_skill_by_topic(capsys):
    code, doc = run(capsys, "guide")
    assert code == 0 and "## Rules" in doc["guide"] and "name: ssisbliss" not in doc["guide"]
    code, doc = run(capsys, "guide", "rules")
    assert doc["guide"].startswith("## Rules") and "## Deploying" not in doc["guide"]


def test_every_way_in_says_live_is_prod_and_a_stage_folder_is_not_the_stage_environment(capsys):
    from ssisbliss.ssis.mcp import instructions
    _, doc = run(capsys, "guide", "rules")
    for text in (doc["guide"], instructions()):
        assert '"live"' in text and "`--env prod`" in text and "it is not the stage environment" in text
    assert '"live" or "production" means --env prod' in cli.RECIPES
    assert "a folder called STAGE is not the stage" in cli.RECIPES


def test_next_commands_from_nested_hints_get_the_same_server(tmp_path, capsys):
    from test_agent import HISTORY, SSIS_STEP, job, step
    jobs = [job("1 A", "2026-10-29T07:00:00"), job("2 B", "2026-10-29T07:00:05", status=0)]
    steps = [step("1 A", 1, "EXEC msdb.dbo.sp_start_job @job_name = N'2 B';"), step("2 B", 1, SSIS_STEP, "SSIS")]
    state = tmp_path / "msdb.json"
    state.write_text(json.dumps({"server_name": "LAB\\SQL01", "jobs": jobs, "steps": steps,
                                 "history": [{**h, "job": "2 B"} for h in HISTORY]}))
    code, doc = run(capsys, "jobs", "--server", f"fake://{state}", "--failed", "--brief")
    assert doc["next"] == [f'ssisbliss history --server fake://{state} "2 B" --runs 1']
    code, doc = run(capsys, "chain", "--server", f"fake://{state}")
    assert doc["next"] == [f'ssisbliss history --server "fake://{state}" "2 B" --runs 1']
