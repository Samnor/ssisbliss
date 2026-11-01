import json
import re

import pytest

from ssisbliss.ssis import base, cli
from ssisbliss.ssis.deploy import (EXIT_OK, EXIT_PLAN_MISMATCH, EXIT_REFUSED, apply, plan_packages,
                                     plan_project, plan_rollback, resolve_only)
from ssisbliss.ssis.dtsx import package_sha256, read_package
from ssisbliss.ssis.ispac import Ispac

from ssisbliss.ssis.fake import FakeCatalog, dtsx, ispac

F, P = "STAGE", "STAGE"


@pytest.fixture
def cat():
    c = FakeCatalog()
    c.pkgs = {n: dtsx(n, body=f"<!-- {n} -->") for n in ("Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx")}
    c.add_project(F, P, ispac(P, c.pkgs, parameters=["SourceDb"], connection_managers=["Stage"]),
                  bindings=[{"object_type": 20, "object_name": P, "parameter_name": "SourceDb",
                             "value_type": "R", "referenced_variable_name": "SourceDb", "sensitive": False,
                             "value": None}],
                  env_refs=[{"reference_id": 1, "reference_type": "R", "environment_folder_name": None,
                             "environment_name": "Default"}])
    return c


def check(plan, prefix):
    return [c for c in plan.checks if c["check"].startswith(prefix)]


def statuses(plan, prefix):
    return {c["status"] for c in check(plan, prefix)}


def base_of(cat, *names):
    return {n: package_sha256(cat.pkgs[n]) for n in names}


# ------------------------------------------------------------------- dtsx --

def test_read_package_fields():
    data = dtsx("X.dtsx", body='<x expr="@[$Project::SourceDb]" ref="Project.ConnectionManagers[Stage]"/>')
    pkg = read_package("X.dtsx", data)
    assert pkg.object_name == "X"
    assert pkg.protection_level == 0
    assert pkg.package_format_version == 8
    assert pkg.project_parameters == {"SourceDb"}
    assert pkg.project_connection_managers == {"Stage"}
    assert pkg.max_assembly_major == 15


def test_missing_protection_level_means_user_key():
    assert read_package("X.dtsx", dtsx("X.dtsx", protection=None)).protection_level == 1


# ------------------------------------------------------------------ ispac --

def test_merge_replaces_only_named_packages(cat):
    server = Ispac(cat.get_project(F, P))
    new_b = dtsx("Load_B.dtsx", revision=2)
    source = Ispac(ispac(P, {"Load_B.dtsx": new_b, "Other.dtsx": dtsx("Other.dtsx")}))
    merged = Ispac(server.merged(source, ["Load_B.dtsx"], project_items=False))
    assert sorted(merged.package_names) == ["Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx"]
    assert package_sha256(merged.package("Load_B.dtsx")) == package_sha256(new_b)
    assert package_sha256(merged.package("Load_A.dtsx")) == package_sha256(cat.pkgs["Load_A.dtsx"])
    assert merged.parameter_names() == {"SourceDb"}


# ------------------------------------------------------- deploy-packages --

def test_unchanged_package_is_skipped(cat):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", cat.pkgs["Load_A.dtsx"])], bases=base_of(cat, "Load_A.dtsx"))
    assert plan.changes[0]["change"] == "unchanged"
    assert not plan.can_apply


def test_update_from_known_base_is_allowed(cat):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))],
                         bases=base_of(cat, "Load_A.dtsx"))
    assert not plan.errors, plan.checks
    assert plan.can_apply
    assert plan.retained == ["Load_B.dtsx", "Load_C.dtsx"]


def test_no_base_refuses_to_overwrite(cat):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases={})
    assert statuses(plan, "overwrite:") == {"error"}
    assert "pull" in check(plan, "overwrite:")[0]["detail"]


def test_someone_else_deployed_since_my_base(cat):
    my_base = base_of(cat, "Load_A.dtsx")
    cat.someone_else_deploys(F, P, "Load_A.dtsx", dtsx("Load_A.dtsx", revision=5))
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases=my_base)
    detail = check(plan, "overwrite:")[0]
    assert detail["status"] == "error"
    assert "LAB\\bob" in detail["detail"]


def test_explicit_overwrite_is_a_warning(cat):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases={},
                         overwrite={"Load_A.dtsx"})
    assert statuses(plan, "overwrite:") == {"warn"}
    assert plan.can_apply


def test_file_name_case_differs_updates_the_deployed_package(cat):
    plan = plan_packages(cat, F, P, [("load_a.dtsx", dtsx("load_a.dtsx", revision=2))],
                         bases={"load_a.dtsx": package_sha256(cat.pkgs["Load_A.dtsx"])})
    assert plan.can_apply, plan.checks
    assert [(c["package"], c["change"]) for c in plan.changes] == [("Load_A.dtsx", "update")]
    assert statuses(plan, "overwrite:") == {"ok"}
    assert statuses(plan, "name:") == {"warn"} and "git mv" in check(plan, "name:")[0]["detail"]
    assert [n for n, _ in plan.payload] == ["Load_A.dtsx"]
    assert plan.extra["renamed"] == {"load_a.dtsx": "Load_A.dtsx"}


def test_file_name_case_differs_still_checks_overwrite(cat):
    plan = plan_packages(cat, F, P, [("load_a.dtsx", dtsx("load_a.dtsx", revision=2))], bases={})
    assert statuses(plan, "overwrite:") == {"error"} and not plan.can_apply


def test_two_files_that_are_the_same_package_are_refused(cat):
    plan = plan_packages(cat, F, P, [("load_a.dtsx", dtsx("load_a.dtsx", revision=2)),
                                     ("Load_A.dtsx", dtsx("Load_A.dtsx", revision=3))],
                         bases=base_of(cat, "Load_A.dtsx"))
    assert "differ only in case" in check(plan, "input")[0]["detail"] and not plan.can_apply


def test_cli_deploy_file_with_wrong_case_records_base_under_package_name(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    (tmp_path / "Load_A.dtsx").unlink()
    local = tmp_path / "load_a.dtsx"
    local.write_bytes(dtsx("load_a.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local))
    assert code == 0 and plan["can_apply"], plan["checks"]
    code, out = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local), "--apply",
                        "--plan-id", plan["plan_id"], "--backup-dir", str(tmp_path / "bk"))
    assert code == 0 and out["status"] == "verified", out
    assert sorted(Ispac(cat.get_project(F, P)).package_names) == ["Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx"]
    key = base.target_key(cat.info.server_name, F, P)
    names = [tmp_path / n for n in ("Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx")]
    assert set(base.lookup(names, key)) == {"Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx"}
    assert not (tmp_path / base.FILE).exists()   # nothing is written into the developer's folder


def test_new_package_needs_allow_new(cat):
    data = dtsx("Load_D.dtsx")
    assert plan_packages(cat, F, P, [("Load_D.dtsx", data)], bases={}).errors
    plan = plan_packages(cat, F, P, [("Load_D.dtsx", data)], bases={}, allow_new=True)
    assert not plan.errors and plan.changes[0]["change"] == "add"


@pytest.mark.parametrize("kwargs, fragment", [
    ({"protection": 1}, "EncryptSensitiveWithUserKey"),
    ({"protection": 2}, "EncryptSensitiveWithPassword"),
    ({"assembly_major": 16}, "SSIS 16.0"),
    ({"fmt": 9}, "PackageFormatVersion 9"),
    ({"body": '<x v="@[$Project::Missing]"/>'}, "Missing"),
    ({"body": '<x v="Project.ConnectionManagers[Gone]"/>'}, "Gone"),
])
def test_package_problems_are_errors(cat, kwargs, fragment):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", **kwargs))],
                         bases=base_of(cat, "Load_A.dtsx"))
    assert any(fragment in c["detail"] for c in plan.errors), plan.checks


def test_running_execution_blocks_unless_allowed(cat):
    cat.running = 1
    args = (cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))])
    assert statuses(plan_packages(*args, bases=base_of(cat, "Load_A.dtsx")), "running") == {"error"}
    assert statuses(plan_packages(*args, bases=base_of(cat, "Load_A.dtsx"), allow_running=True), "running") == {"warn"}


def test_wrong_server_major_flags_newer_build():
    c = FakeCatalog(major=13)
    c.add_project(F, P, ispac(P, {"A.dtsx": dtsx("A.dtsx", assembly_major=13)}))
    plan = plan_packages(c, F, P, [("A.dtsx", dtsx("A.dtsx", assembly_major=15))], overwrite={"A.dtsx"})
    assert any("SSIS 15.0" in e["detail"] for e in plan.errors)


# ------------------------------------------------------------------ apply --

def test_apply_requires_matching_plan_id(cat, tmp_path):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))],
                         bases=base_of(cat, "Load_A.dtsx"))
    result, code = apply(cat, plan, "0000000000000000", tmp_path)
    assert code == EXIT_PLAN_MISMATCH and cat.calls == []


def test_apply_refuses_plan_with_errors(cat, tmp_path):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases={})
    result, code = apply(cat, plan, plan.plan_id, tmp_path)
    assert code == EXIT_REFUSED and cat.calls == []


def test_apply_deploys_backs_up_and_verifies(cat, tmp_path):
    new_a = dtsx("Load_A.dtsx", revision=2)
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", new_a)], bases=base_of(cat, "Load_A.dtsx"))
    result, code = apply(cat, plan, plan.plan_id, tmp_path)
    assert code == EXIT_OK, result
    assert result["status"] == "verified"
    assert cat.calls == ["deploy_packages"]
    deployed = Ispac(cat.get_project(F, P))
    assert package_sha256(deployed.package("Load_A.dtsx")) == package_sha256(new_a)
    assert package_sha256(deployed.package("Load_B.dtsx")) == package_sha256(cat.pkgs["Load_B.dtsx"])
    backup = Ispac.from_file(result["backup"])
    assert package_sha256(backup.package("Load_A.dtsx")) == package_sha256(cat.pkgs["Load_A.dtsx"])
    assert f"--to-lsn {result['previous_lsn']}" in result["rollback"]


def test_plan_id_changes_when_server_moves(cat):
    files = [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))]
    before = plan_packages(cat, F, P, files, overwrite={"Load_A.dtsx"}).plan_id
    cat.someone_else_deploys(F, P, "Load_C.dtsx", dtsx("Load_C.dtsx", revision=9))
    assert plan_packages(cat, F, P, files, overwrite={"Load_A.dtsx"}).plan_id != before


# -------------------------------------------------------- deploy-project --

def test_project_subset_keeps_everything_else(cat, tmp_path):
    # Developer's build has an old Load_C and a new Load_B; only Load_B is taken.
    built = Ispac(ispac(P, {"Load_A.dtsx": cat.pkgs["Load_A.dtsx"], "Load_B.dtsx": dtsx("Load_B.dtsx", revision=3),
                            "Load_C.dtsx": dtsx("Load_C.dtsx", revision=0)},
                        parameters=["SourceDb"], connection_managers=["Stage"]))
    plan = plan_project(cat, F, P, built, ["Load_B.dtsx"], bases=base_of(cat, "Load_B.dtsx"))
    assert plan.method == "deploy_packages"
    assert not plan.errors, plan.checks
    result, code = apply(cat, plan, plan.plan_id, tmp_path)
    assert code == EXIT_OK, result
    deployed = Ispac(cat.get_project(F, P))
    assert package_sha256(deployed.package("Load_C.dtsx")) == package_sha256(cat.pkgs["Load_C.dtsx"])
    assert package_sha256(deployed.package("Load_B.dtsx")) == package_sha256(built.package("Load_B.dtsx"))


def test_project_requires_only(cat):
    built = Ispac(ispac(P, cat.pkgs))
    assert plan_project(cat, F, P, built, []).errors


def test_only_accepts_name_without_extension(cat):
    built = Ispac(ispac(P, cat.pkgs))
    assert resolve_only(built, ["Load_A", "Load_B.dtsx"]) == ["Load_A.dtsx", "Load_B.dtsx"]
    # Case is never guessed: the wrong case stays wrong and the error lists the real names.
    assert resolve_only(built, ["load_a"]) == ["load_a"]
    plan = plan_project(cat, F, P, built, ["load_a"])
    assert plan.errors and "Load_A.dtsx" in plan.errors[0]["detail"]


def test_project_items_differ_warns_on_keep(cat):
    built = Ispac(ispac(P, cat.pkgs, parameters=["SourceDb", "NewParam"], connection_managers=["Stage"]))
    plan = plan_project(cat, F, P, built, ["Load_A.dtsx"], bases=base_of(cat, "Load_A.dtsx"))
    assert statuses(plan, "project_items") == {"warn"}


def test_project_items_update_refuses_to_drop_bound_parameter(cat):
    built = Ispac(ispac(P, cat.pkgs, parameters=["Other"], connection_managers=["Stage"]))
    plan = plan_project(cat, F, P, built, ["Load_A.dtsx"], project_items="update", bases=base_of(cat, "Load_A.dtsx"))
    assert any("SourceDb" in e["detail"] for e in plan.errors)


def test_project_items_update_refuses_when_retained_package_has_secrets():
    c = FakeCatalog()
    pkgs = {"A.dtsx": dtsx("A.dtsx"), "Secret.dtsx": dtsx("Secret.dtsx", sensitive=True)}
    c.add_project(F, P, ispac(P, pkgs))
    built = Ispac(ispac(P, {"A.dtsx": dtsx("A.dtsx", revision=2)}, parameters=["X"]))
    plan = plan_project(c, F, P, built, ["A.dtsx"], project_items="update", overwrite={"A.dtsx"})
    assert statuses(plan, "sensitive_values") == {"error"}


def test_project_items_update_uses_merged_full_deploy(cat, tmp_path):
    built = Ispac(ispac(P, cat.pkgs | {"Load_A.dtsx": dtsx("Load_A.dtsx", revision=2)},
                        parameters=["SourceDb", "NewParam"], connection_managers=["Stage"]))
    plan = plan_project(cat, F, P, built, ["Load_A.dtsx"], project_items="update", bases=base_of(cat, "Load_A.dtsx"))
    assert plan.method == "deploy_project" and not plan.errors, plan.checks
    result, code = apply(cat, plan, plan.plan_id, tmp_path)
    assert code == EXIT_OK, result
    assert Ispac(cat.get_project(F, P)).parameter_names() == {"SourceDb", "NewParam"}


# --------------------------------------------------------------- rollback --

def test_rollback_restores_previous_version(cat, tmp_path):
    first = cat.project(F, P).object_version_lsn
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], overwrite={"Load_A.dtsx"})
    apply(cat, plan, plan.plan_id, tmp_path)
    rb = plan_rollback(cat, F, P, first)
    assert not rb.errors
    result, code = apply(cat, rb, rb.plan_id, tmp_path)
    assert code == EXIT_OK, result
    assert package_sha256(Ispac(cat.get_project(F, P)).package("Load_A.dtsx")) == package_sha256(cat.pkgs["Load_A.dtsx"])


def _version_detail(plan):
    return next(c["detail"] for c in plan.checks if c["check"] == "version")


def test_rollback_plan_says_when_it_undoes_only_your_own_deploys(cat):
    first = cat.project(F, P).object_version_lsn
    cat.deploy_packages(F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))])
    latest = cat.project(F, P).object_version_lsn
    rb = plan_rollback(cat, F, P, first)
    assert "undoes only your own deploys" in _version_detail(rb) and f"lsn {latest}" in _version_detail(rb)
    assert rb.can_apply and not rb.changes


def test_rollback_plan_never_calls_a_colleagues_or_unknown_deploy_yours(cat, monkeypatch):
    first = cat.project(F, P).object_version_lsn
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=5))
    assert _version_detail(plan_rollback(cat, F, P, first, undo_others=True)) == f"restore lsn {first}"
    versions = cat.versions(cat.project(F, P).project_id)
    monkeypatch.setattr(cat, "versions", lambda _: [{**v, "created_by": None} for v in versions])
    assert _version_detail(plan_rollback(cat, F, P, first)) == f"restore lsn {first}"


def test_rollback_to_unknown_version_is_refused(cat):
    assert plan_rollback(cat, F, P, 1).errors


# -------------------------------------------------------------------- cli --

def run_cli(monkeypatch, capsys, cat, *argv):
    from ssisbliss.ssis.commands import common, deploys, read
    for module in (common, deploys, read):   # each command module imports _catalog
        monkeypatch.setattr(module, "_catalog", lambda env, a: cat)
    code = cli.main(["--help"] if not argv else list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_help_recipes_are_valid_commands():
    help_text = cli.parser().format_help()
    assert "Visual Studio equivalents" in help_text and "--plan-id" in help_text
    t = ["--env", "dev", "--folder", F, "--project", P]
    commands = re.findall(r"deploy-(?:project|packages) T [^()\n]*", cli.RECIPES)
    assert len(commands) >= 4
    for c in commands:
        argv = [x for w in c.split() for x in (t if w == "T" else [w])]
        a = cli.parser().parse_args(argv)
        assert a.env == "dev" and a.project == P and not a.apply


def test_cli_pull_edit_deploy_cycle(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    code, out = run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    assert code == 0 and {p["result"] for p in out["packages"]} == {"written"}

    local = tmp_path / "Load_A.dtsx"
    local.write_bytes(dtsx("Load_A.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local))
    assert code == 0 and plan["can_apply"], plan["checks"]

    code, out = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local), "--apply")
    assert code == EXIT_REFUSED and "plan-id" in out["reason"]

    code, out = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local), "--apply",
                        "--plan-id", plan["plan_id"], "--backup-dir", str(tmp_path / "bk"))
    assert code == 0 and out["status"] == "verified", out

    # Base was advanced: iterating again on my own deploy is not a conflict.
    local.write_bytes(dtsx("Load_A.dtsx", revision=3))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local))
    assert plan["can_apply"], plan["checks"]

    # A colleague deploys over it: now I am stopped.
    cat.someone_else_deploys(F, P, "Load_A.dtsx", dtsx("Load_A.dtsx", revision=7))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(local))
    assert code == EXIT_REFUSED and not plan["can_apply"]


def test_cli_deploy_project_finds_base_in_ssdt_project_folder(cat, tmp_path, monkeypatch, capsys):
    # SSDT builds to <project>/bin/<Configuration>/x.ispac; pull wrote the base into <project>.
    t = ["--server", "x", "--folder", F, "--project", P]
    project_dir = tmp_path / P
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(project_dir), "--packages", "Load_A.dtsx")
    edited = dtsx("Load_A.dtsx", revision=2)
    (project_dir / "Load_A.dtsx").write_bytes(edited)    # the developer's change, then a build of it
    built = project_dir / "bin" / "Development" / f"{P}.ispac"
    built.parent.mkdir(parents=True)
    built.write_bytes(ispac(P, {**cat.pkgs, "Load_A.dtsx": edited},
                            parameters=["SourceDb"], connection_managers=["Stage"]))
    deploy = ["deploy-project", *t, "--ispac", str(built), "--only", "Load_A.dtsx"]

    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    overwrite = lambda plan: [c["status"] for c in plan["checks"] if c["check"].startswith("overwrite:")]
    assert code == EXIT_OK and overwrite(plan) == ["ok"], plan["checks"]
    assert plan["base_dir"] == str(project_dir)
    code, out = run_cli(monkeypatch, capsys, cat, *deploy, "--apply", "--plan-id", plan["plan_id"],
                        "--backup-dir", str(tmp_path / "bk"))
    assert out["status"] == "verified", out
    assert not (built.parent / base.FILE).exists()

    # The base advanced in the project folder, so a colleague's later deploy still stops me.
    cat.someone_else_deploys(F, P, "Load_A.dtsx", dtsx("Load_A.dtsx", revision=7))
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    assert code == EXIT_REFUSED and overwrite(plan) == ["error"], plan["checks"]


def test_cli_pull_never_overwrites_local_work(cat, tmp_path, monkeypatch, capsys):
    local = tmp_path / "Load_A.dtsx"
    local.write_bytes(b"my work in progress")
    code, out = run_cli(monkeypatch, capsys, cat, "pull", "--server", "x", "--folder", F, "--project", P,
                        "--out", str(tmp_path), "--packages", "Load_A.dtsx")
    assert out["packages"][0]["result"] == "differs"
    assert local.read_bytes() == b"my work in progress"
    assert package_sha256((tmp_path / "Load_A.server.dtsx").read_bytes()) == package_sha256(cat.pkgs["Load_A.dtsx"])
    key = base.target_key(cat.info.server_name, F, P)
    assert base.lookup([local], key) == {}


def test_cli_env_server_name_mismatch(cat, tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "ssisbliss.toml"
    cfg.write_text('[environments.prod]\nserver = "prod"\nserver_name = "PRODSQL\\\\SQL01"\n')
    f = tmp_path / "Load_A.dtsx"
    f.write_bytes(dtsx("Load_A.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", "--env", "prod", "--config", str(cfg),
                         "--folder", F, "--project", P, str(f), "--overwrite", "Load_A.dtsx")
    assert code == EXIT_REFUSED
    assert any(c["check"] == "environment" and c["status"] == "error" for c in plan["checks"])


def test_cli_prod_uncommitted_tells_agent_not_to_commit(cat, tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "ssisbliss.toml"
    cfg.write_text('[environments.prod]\nserver = "prod"\nrequire_git_committed = true\n')
    f = tmp_path / "Load_A.dtsx"
    f.write_bytes(dtsx("Load_A.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", "--env", "prod", "--config", str(cfg),
                         "--folder", F, "--project", P, str(f), "--overwrite", "Load_A.dtsx")
    assert code == EXIT_REFUSED and not plan["can_apply"]
    git = [c for c in plan["checks"] if c["check"] == "git" and c["status"] == "error"]
    assert git and "do not commit on their behalf" in json.dumps(git)


# ------------------------------------- what the real catalog does (lab, 15.0.4470) --

def test_deploy_packages_table_names_have_no_extension():
    # The server appends ".dtsx": sending "X.dtsx" created a second package "X.dtsx.dtsx".
    from ssisbliss.ssis.catalog import package_table_name
    assert package_table_name("Load_A.dtsx") == "Load_A"
    assert package_table_name("Load_A.DTSX") == "Load_A"
    assert package_table_name("Load_A") == "Load_A"


def test_package_hash_survives_the_catalog_rewrite():
    from ssisbliss.ssis.fake import stored_like_ssisdb
    data = dtsx("Load_A.dtsx", revision=2)
    stored = Ispac(stored_like_ssisdb(ispac(P, {"Load_A.dtsx": data}))).package("Load_A.dtsx")
    assert stored != data and b'DTS:ProtectionLevel="5"' in stored
    assert package_sha256(stored) == package_sha256(data)
    assert package_sha256(stored) != package_sha256(dtsx("Load_A.dtsx", revision=3))


def test_redeploying_your_own_change_is_not_an_overwrite(cat, tmp_path, monkeypatch, capsys):
    # After a deploy the server holds a rewritten copy; the next deploy must still see it as yours.
    f = tmp_path / "Load_A.dtsx"
    for revision in (2, 3):
        f.write_bytes(dtsx("Load_A.dtsx", revision=revision))
        args = ["deploy-packages", "--server", "x", "--folder", F, "--project", P, str(f),
                "--backup-dir", str(tmp_path / "bk")] + (["--overwrite", "Load_A.dtsx"] if revision == 2 else [])
        code, plan = run_cli(monkeypatch, capsys, cat, *args)
        assert code == EXIT_OK, plan
        code, out = run_cli(monkeypatch, capsys, cat, *args, "--apply", "--plan-id", plan["plan_id"])
        assert out["status"] == "verified", out


def test_pull_writes_packages_that_fit_a_visual_studio_project(cat, tmp_path, monkeypatch, capsys):
    code, _ = run_cli(monkeypatch, capsys, cat, "pull", "--server", "x", "--folder", F, "--project", P,
                        "--out", str(tmp_path), "--packages", "Load_A.dtsx")
    assert code == EXIT_OK
    pulled = read_package("Load_A.dtsx", (tmp_path / "Load_A.dtsx").read_bytes())
    assert pulled.protection_level == 0


def test_merged_manifest_has_no_xml_declaration(cat):
    # SSISDB 15.0 rejects any declaration: "'Project' element for Project is missing in the project manifest".
    server = Ispac(cat.get_project(F, P))
    source = Ispac(ispac(P, {"Load_B.dtsx": dtsx("Load_B.dtsx", revision=2)}))
    merged = Ispac(server.merged(source, ["Load_B.dtsx"], project_items=True))
    assert merged.parts["@Project.manifest"][1].startswith(b"<SSIS:Project")
    cat.deploy_project(F, P, merged.data)
    with pytest.raises(RuntimeError, match="'Project' element"):
        cat.deploy_project(F, P, _with_manifest_prefix(merged.data, b'<?xml version="1.0"?>'))


def _with_manifest_prefix(data: bytes, prefix: bytes) -> bytes:
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            part = zin.read(item)
            zout.writestr(item, prefix + part if item.filename == "@Project.manifest" else part)
    return out.getvalue()


def test_folder_permissions_are_enough_without_ssis_admin(cat):
    # Lab, 15.0.4470: a developer with folder MODIFY_OBJECTS (not ssis_admin) can deploy.
    from dataclasses import replace
    cat.info = replace(cat.info, is_admin=False)
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], overwrite={"Load_A.dtsx"})
    assert ("permission", "ok") in [(c["check"], c["status"]) for c in plan.checks], plan.checks
    cat.read_only_users.add(cat.user)
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], overwrite={"Load_A.dtsx"})
    assert any(c["check"] == "permission" and c["status"] == "error" and "MODIFY_OBJECTS" in c["detail"]
               for c in plan.checks)


# ------------------------------------------------------------ prod: pushed --

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev",
                        "GIT_COMMITTER_EMAIL": "dev@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(cwd)})


def test_require_git_pushed(cat, tmp_path, monkeypatch, capsys):
    from ssisbliss.ssis.config import git_unpushed
    work = tmp_path / "work"
    work.mkdir()
    f = work / "Load_A.dtsx"
    f.write_bytes(dtsx("Load_A.dtsx", revision=2))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "change")
    assert "no remote" in git_unpushed([f])[0]
    _git(tmp_path, "init", "-q", "-b", "main", "--bare", "origin.git")
    _git(work, "remote", "add", "origin", str(tmp_path / "origin.git"))
    assert "not pushed" in git_unpushed([f])[0]
    _git(work, "push", "-q", "-u", "origin", "HEAD")
    assert git_unpushed([f]) == []
    f.write_bytes(dtsx("Load_A.dtsx", revision=3))
    _git(work, "commit", "-q", "-am", "another change")
    assert "not pushed" in git_unpushed([f])[0]

    cfg = work / "ssisbliss.toml"
    cfg.write_text('[environments.prod]\nserver = "prod"\nrequire_git_pushed = true\n')
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", "--env", "prod", "--config", str(cfg),
                         "--folder", F, "--project", P, str(f), "--overwrite", "Load_A.dtsx")
    git = next(c for c in plan["checks"] if c["check"] == "git")
    assert code == EXIT_REFUSED and git["status"] == "error" and "do not push" in git["detail"]



def test_ispac_must_match_the_project_folders_package(cat, tmp_path, monkeypatch, capsys):
    # A build made from other files (a stale build, or files changed to get past a check and put back).
    t = ["--server", "x", "--folder", F, "--project", P]
    project_dir = tmp_path / P
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(project_dir), "--packages", "Load_A.dtsx")
    (project_dir / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2, protection=None))   # user key (default)
    built = project_dir / "bin" / "Development" / f"{P}.ispac"
    built.parent.mkdir(parents=True)
    built.write_bytes(ispac(P, {**cat.pkgs, "Load_A.dtsx": dtsx("Load_A.dtsx", revision=2)},
                            parameters=["SourceDb"], connection_managers=["Stage"]))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-project", *t, "--ispac", str(built), "--only", "Load_A")
    src = next(c for c in plan["checks"] if c["check"] == "source:Load_A.dtsx")
    assert code == EXIT_REFUSED and "protection level" in src["detail"]


def test_plan_mismatch_says_what_changed_and_how_to_continue(cat, tmp_path):
    plan = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases=base_of(cat, "Load_A.dtsx"))
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=9))
    fresh = plan_packages(cat, F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))], bases=base_of(cat, "Load_A.dtsx"))
    out, code = apply(cat, fresh, plan.plan_id, tmp_path)
    assert code == EXIT_PLAN_MISMATCH
    assert "LAB\\bob" in out["reason"] and f"--plan-id {fresh.plan_id}" in out["reason"]



# ------------------------------------------------ base records per repository --

def test_worktrees_of_one_repository_share_base_records(tmp_path):
    main, other = tmp_path / "main", tmp_path / "pr-branch"
    main.mkdir()
    (main / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx"))
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "x")
    _git(main, "worktree", "add", "-q", "-b", "pr", str(other))
    key = "SRV/F/P"
    base.record(main, key, 7, {"Load_A.dtsx": "abc"})
    assert base.lookup([other / "Load_A.dtsx"], key) == {"Load_A.dtsx": "abc"}     # the other worktree knows
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "main")
    assert base.lookup([elsewhere / "Load_A.dtsx"], key) == {}                      # another repository does not


def test_clones_with_the_same_origin_share_base_records(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main", "--bare", "origin.git")
    for clone in ("a", "b"):
        (tmp_path / clone).mkdir()
        _git(tmp_path / clone, "init", "-q", "-b", "main")
        _git(tmp_path / clone, "remote", "add", "origin", str(tmp_path / "origin.git"))
    base.record(tmp_path / "a", "SRV/F/P", 3, {"Load_B.dtsx": "def"})
    assert base.lookup([tmp_path / "b" / "Load_B.dtsx"], "SRV/F/P") == {"Load_B.dtsx": "def"}


def test_base_records_are_read_once_per_folder(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "sub").mkdir()
    names = [f"Load_{i:03d}.dtsx" for i in range(50)]
    base.record(tmp_path, "SRV/F/P", 3, {n: n for n in names})
    calls = []
    real = base._git
    monkeypatch.setattr(base, "_git", lambda *a: calls.append(a) or real(*a))
    found = base.lookup([tmp_path / n for n in names] + [tmp_path / "sub" / "Other.dtsx"], "SRV/F/P")
    assert found == {n: n for n in names}
    assert len(calls) == 4                           # two git calls for each of the two folders, not per package


def test_legacy_folder_records_are_still_read(tmp_path):
    (tmp_path / base.FILE).write_text(json.dumps({"SRV/F/P": {"packages": {"Load_A.dtsx": {"sha256": "old", "lsn": 1}}}}))
    assert base.lookup([tmp_path / "Load_A.dtsx"], "SRV/F/P") == {"Load_A.dtsx": "old"}


def test_user_level_environments_file(tmp_path, monkeypatch):
    from ssisbliss.ssis.config import load_environment
    user = tmp_path / "home" / "ssisbliss.toml"
    user.parent.mkdir()
    user.write_text('[environments.dev]\nserver = "devsql01"\n')
    monkeypatch.setenv("SSISBLISS_CONFIG", str(user))
    monkeypatch.chdir(tmp_path)                      # no ssisbliss.toml here or above
    assert load_environment("dev").server == "devsql01"
    (tmp_path / "ssisbliss.toml").write_text('[environments.dev]\nserver = "repo-dev"\n')
    assert load_environment("dev").server == "repo-dev"   # a file in the repository still wins


def test_status_shows_each_package_against_your_version(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=9))
    (tmp_path / "Load_C.dtsx").write_bytes(dtsx("Load_C.dtsx", revision=4))
    (tmp_path / "Load_D.dtsx").write_bytes(dtsx("Load_D.dtsx"))
    code, out = run_cli(monkeypatch, capsys, cat, "status", *t, "--dir", str(tmp_path))
    rows = {r["package"]: r for r in out["packages"]}
    assert rows["Load_A.dtsx"]["server"].startswith("as you left it") and rows["Load_A.dtsx"]["local"] == "same as the server"
    assert rows["Load_B.dtsx"]["server"].startswith("changed since your version") and "LAB\\bob" in rows["Load_B.dtsx"]["changed_by"]
    assert rows["Load_C.dtsx"]["local"] == "differs from the server"
    assert rows["Load_D.dtsx"]["server"] == "not deployed" and rows["Load_D.dtsx"]["local"].startswith("only here")
    assert "LAB\\bob" in out["deploys_since_your_oldest_version"]


def test_overwrite_error_lists_deploys_since_your_version(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=9), who="LAB\\carol")
    cat.someone_else_deploys(F, P, "Load_A.dtsx", dtsx("Load_A.dtsx", revision=8))
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(tmp_path / "Load_A.dtsx"))
    err = next(c for c in plan["checks"] if c["check"] == "overwrite:Load_A.dtsx")["detail"]
    assert code == EXIT_REFUSED and "deployed since your version" in err and "LAB\\bob" in err and "LAB\\carol" in err


# ------------------------------------------------------- remove, from-ref --

def _deploy(monkeypatch, capsys, cat, tmp_path, *args):
    code, plan = run_cli(monkeypatch, capsys, cat, *args)
    assert code == 0, plan.get("checks", plan)
    code, out = run_cli(monkeypatch, capsys, cat, *args, "--apply", "--plan-id", plan["plan_id"],
                        "--backup-dir", str(tmp_path / "bk"))
    assert out["status"] == "verified", out
    return out


def test_remove_a_package_your_branch_added(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    new = tmp_path / "Load_D.dtsx"
    new.write_bytes(dtsx("Load_D.dtsx"))
    _deploy(monkeypatch, capsys, cat, tmp_path, "deploy-packages", *t, str(new), "--allow-new")
    assert "Load_D.dtsx" in Ispac(cat.get_project(F, P)).package_names
    _deploy(monkeypatch, capsys, cat, tmp_path, "remove-packages", *t, "Load_D", "--dir", str(tmp_path))
    assert sorted(Ispac(cat.get_project(F, P)).package_names) == ["Load_A.dtsx", "Load_B.dtsx", "Load_C.dtsx"]
    assert base.lookup([new], base.target_key(cat.info.server_name, F, P)) == {}


def test_remove_refuses_someone_elses_package_and_a_called_package(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    code, plan = run_cli(monkeypatch, capsys, cat, "remove-packages", *t, "Load_B", "--dir", str(tmp_path))
    assert code == EXIT_REFUSED and any(c["check"] == "overwrite:Load_B.dtsx" and c["status"] == "error" for c in plan["checks"])
    caller = dtsx("Load_A.dtsx", revision=5, body="<ExecutePackageTask><PackageName>Load_C.dtsx</PackageName></ExecutePackageTask>")
    cat.someone_else_deploys(F, P, "Load_A.dtsx", caller)
    code, plan = run_cli(monkeypatch, capsys, cat, "remove-packages", *t, "Load_C", "--overwrite", "Load_C.dtsx",
                         "--dir", str(tmp_path))
    assert code == EXIT_REFUSED and any(c["check"] == "reference:Load_A.dtsx" for c in plan["checks"])


def test_put_dev_back_to_main_after_a_pr_deploy(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    work = tmp_path / "repo"
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(work))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "*.dtsx")
    _git(work, "commit", "-q", "-m", "as released")
    _git(work, "checkout", "-q", "-b", "pr/load-a")
    (work / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    _deploy(monkeypatch, capsys, cat, tmp_path, "deploy-packages", *t, str(work / "Load_A.dtsx"))   # my PR on dev
    released = (work / "Load_A.dtsx").read_bytes()
    _git(work, "stash", "-q")
    released = (work / "Load_A.dtsx").read_bytes()
    _git(work, "stash", "pop", "-q")
    _deploy(monkeypatch, capsys, cat, tmp_path, "deploy-packages", *t, str(work / "Load_A.dtsx"), "--from-ref", "main")
    from ssisbliss.ssis.dtsx import package_sha256 as h
    assert h(Ispac(cat.get_project(F, P)).package("Load_A.dtsx")) == h(released)
    assert (work / "Load_A.dtsx").read_bytes() != released   # the working tree is untouched


# ----------------------------------------------- project items, one by one --

def _with_params(data: bytes, values: dict[str, str]) -> bytes:
    """An .ispac with Project.params set to these parameters and values."""
    import io
    import zipfile
    inner = "".join(f'<SSIS:Parameter SSIS:Name="{n}"><SSIS:Properties><SSIS:Property SSIS:Name="Sensitive">0'
                    f'</SSIS:Property><SSIS:Property SSIS:Name="Value">{v}</SSIS:Property></SSIS:Properties>'
                    f'</SSIS:Parameter>' for n, v in values.items())
    xml = f'<?xml version="1.0"?><SSIS:Parameters xmlns:SSIS="www.microsoft.com/SqlServer/SSIS">{inner}</SSIS:Parameters>'
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as z:
        for i in zin.infolist():
            z.writestr(i, xml.encode() if i.filename == "Project.params" else zin.read(i))
    return out.getvalue()


def _params(cat) -> dict[str, str]:
    import re
    text = Ispac(cat.get_project(F, P)).parts["Project.params"][1].decode()
    return dict(re.findall(r'SSIS:Name="(\w+)">.*?SSIS:Name="Value">([^<]*)<', text, re.S))


def _items_setup(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    proj = tmp_path / P
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(proj))
    cat.deploy_project(F, P, _with_params(cat.get_project(F, P), {"SourceDb": "STAGE"}))   # values, as deployed
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(proj))
    return t, proj


def test_item_deploys_your_parameter_and_keeps_a_colleagues(cat, tmp_path, monkeypatch, capsys):
    t, proj = _items_setup(cat, tmp_path, monkeypatch, capsys)
    cat.user = "LAB\\bob"
    cat.deploy_project(F, P, _with_params(cat.get_project(F, P), {"SourceDb": "STAGE", "Region": "EU"}))
    cat.user = "LAB\\me"
    built = proj / "bin" / "Development" / f"{P}.ispac"
    built.parent.mkdir(parents=True)
    built.write_bytes(_with_params(ispac(P, cat.pkgs, parameters=["SourceDb"], connection_managers=["Stage"]),
                                   {"SourceDb": "STAGE", "BatchSize": "500"}))
    _deploy(monkeypatch, capsys, cat, tmp_path, "deploy-project", *t, "--ispac", str(built), "--item", "BatchSize")
    assert _params(cat) == {"SourceDb": "STAGE", "Region": "EU", "BatchSize": "500"}


def test_item_refuses_a_parameter_a_colleague_changed(cat, tmp_path, monkeypatch, capsys):
    t, proj = _items_setup(cat, tmp_path, monkeypatch, capsys)
    cat.user = "LAB\\bob"
    cat.deploy_project(F, P, _with_params(cat.get_project(F, P), {"SourceDb": "STAGE_BOB"}))
    cat.user = "LAB\\me"
    built = proj / "bin" / "Development" / f"{P}.ispac"
    built.parent.mkdir(parents=True)
    built.write_bytes(_with_params(ispac(P, cat.pkgs, parameters=["SourceDb"], connection_managers=["Stage"]),
                                   {"SourceDb": "STAGE_MINE"}))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-project", *t, "--ispac", str(built), "--item", "SourceDb")
    err = next(c for c in plan["checks"] if c["check"] == "overwrite:param:SourceDb")
    assert code == EXIT_REFUSED and err["status"] == "error" and "LAB\\bob" in err["detail"]


def test_install_skill_user_level(tmp_path, monkeypatch, capsys):
    (tmp_path / ".claude").mkdir()
    code, out = run_cli(monkeypatch, capsys, None, "install-skill", "--home", str(tmp_path))
    assert code == 0 and out["installed"] == [str(tmp_path / ".claude" / "skills" / "ssisbliss" / "SKILL.md")]
    text = (tmp_path / ".claude" / "skills" / "ssisbliss" / "SKILL.md").read_text()
    assert text.startswith("---\nname: ssisbliss\n") and "--plan-id" in text
    code, out = run_cli(monkeypatch, capsys, None, "install-skill", "--home", str(tmp_path), "--codex")
    assert out["installed"] == [str(tmp_path / ".codex" / "skills" / "ssisbliss" / "SKILL.md")]


def test_rollback_refuses_to_undo_a_colleagues_later_deploy(cat, tmp_path):
    first = cat.project(F, P).object_version_lsn
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=5))
    rb = plan_rollback(cat, F, P, first)
    assert any(c["check"] == "others" and "LAB\\bob" in c["detail"] and "--from-ref" in c["detail"] for c in rb.errors)
    assert not plan_rollback(cat, F, P, first, undo_others=True).errors


def test_status_lists_your_changes_and_the_command_to_deploy_them(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=9))   # stale here, not my change
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=3))
    (tmp_path / "Load_D.dtsx").write_bytes(dtsx("Load_D.dtsx"))
    code, out = run_cli(monkeypatch, capsys, cat, "status", *t, "--dir", str(tmp_path))
    assert out["your_changes"] == ["Load_A.dtsx", "Load_D.dtsx"] and "changed_by_others_too" not in out
    cmd = out["deploy_your_changes"]
    assert "--allow-new" in cmd and cmd.endswith(f"{tmp_path / 'Load_A.dtsx'} {tmp_path / 'Load_D.dtsx'}")
    (tmp_path / "Load_B.dtsx").write_bytes(dtsx("Load_B.dtsx", revision=4))
    code, out = run_cli(monkeypatch, capsys, cat, "status", *t, "--dir", str(tmp_path))
    assert out["changed_by_others_too"] == ["Load_B.dtsx"] and "Load_B.dtsx" not in out["your_changes"]


def test_deploy_packages_warns_about_your_other_changes_not_listed(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=9))   # stale here: not mine
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=3))
    (tmp_path / "Load_C.dtsx").write_bytes(dtsx("Load_C.dtsx", revision=4))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(tmp_path / "Load_A.dtsx"))
    warn = next(c for c in plan["checks"] if c["check"] == "other_local_changes")
    assert code == 0 and "Load_C.dtsx" in warn["detail"] and "Load_B" not in warn["detail"]


def test_rollback_ignores_a_colleagues_deploy_that_a_restore_already_undid(cat, tmp_path):
    first = cat.project(F, P).object_version_lsn
    cat.someone_else_deploys(F, P, "Load_B.dtsx", dtsx("Load_B.dtsx", revision=5))
    cat.restore_project(F, P, first)                      # their deploy was rolled back
    cat.deploy_packages(F, P, [("Load_A.dtsx", dtsx("Load_A.dtsx", revision=2))])   # then mine
    assert not plan_rollback(cat, F, P, first).errors


def test_status_matches_a_file_named_in_another_case_to_the_deployed_package(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    (tmp_path / "Load_A.dtsx").unlink()
    (tmp_path / "load_a.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=3))
    code, out = run_cli(monkeypatch, capsys, cat, "status", *t, "--dir", str(tmp_path))
    row = next(r for r in out["packages"] if r["package"] == "Load_A.dtsx")
    assert row["file"] == "load_a.dtsx" and row["local"] == "differs from the server"
    assert not any(r["package"] == "load_a.dtsx" for r in out["packages"])
    assert out["your_changes"] == ["Load_A.dtsx"] and out["deploy_your_changes"].endswith("load_a.dtsx")


def test_a_refused_plan_starts_with_why(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    cat.someone_else_deploys(F, P, "Load_A.dtsx", dtsx("Load_A.dtsx", revision=8))
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    code, plan = run_cli(monkeypatch, capsys, cat, "deploy-packages", *t, str(tmp_path / "Load_A.dtsx"))
    assert code == EXIT_REFUSED and list(plan)[:3] == ["ok", "error_kind", "refused_because"]
    assert "LAB\\bob" in plan["refused_because"][0] and "next" not in plan


def test_a_plan_says_how_to_apply_it(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    deploy = ["deploy-packages", *t, str(tmp_path / "Load_A.dtsx")]
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    assert code == EXIT_OK and plan["ok"] and "asked for this change" in plan["apply_when"]
    assert plan["next"] == [f"ssisbliss deploy-packages --server x --folder {F} --project {P} "
                            f"{tmp_path / 'Load_A.dtsx'} --apply --plan-id {plan['plan_id']}"]
    code, out = run_cli(monkeypatch, capsys, cat, *deploy, "--apply")   # forgot the plan id
    assert code == EXIT_REFUSED and out["error_kind"] == "refused" and out["next"] == plan["next"]
    code, out = run_cli(monkeypatch, capsys, cat, *deploy, "--apply", "--plan-id", plan["plan_id"])
    assert code == EXIT_OK and out["ok"] and "next" not in out


def test_install_skill_for_copilot_and_vs_code(tmp_path, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, None, "install-skill", "--home", str(tmp_path), "--copilot")
    dest = tmp_path / ".copilot" / "skills" / "ssisbliss" / "SKILL.md"
    assert code == 0 and out["installed"] == [str(dest)]
    # VS Code's rules: name matches the folder, lowercase letters, digits and hyphens, at most 64;
    # description at most 1024 characters (a skill that breaks them silently does not load).
    import re as _re
    head = dest.read_text().split("---")[1]
    name = _re.search(r"^name: (.+)$", head, _re.M).group(1).strip()
    desc = _re.search(r"^description: (.+)$", head, _re.M).group(1).strip()
    assert name == dest.parent.name and _re.fullmatch(r"[a-z0-9-]{1,64}", name) and len(desc) <= 1024


def test_redeploy_after_rolling_back_your_own_deploy(cat, tmp_path, monkeypatch, capsys):
    # Found by formal/model_check.py at depth 5: your own rollback looked like someone else's deploy.
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    first = cat.project(F, P).object_version_lsn
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    deploy = ["deploy-packages", *t, str(tmp_path / "Load_A.dtsx")]
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    assert run_cli(monkeypatch, capsys, cat, *deploy, "--apply", "--plan-id", plan["plan_id"])[0] == EXIT_OK
    cat.restore_project(F, P, first)                       # the deploy broke something: I roll it back
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=3))   # and fix it
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    check = next(c for c in plan["checks"] if c["check"] == "overwrite:Load_A.dtsx")
    assert code == EXIT_OK and plan["can_apply"] and "you restored it yourself" in check["detail"]


def test_redeploy_after_someone_else_rolled_back_is_refused(cat, tmp_path, monkeypatch, capsys):
    t = ["--server", "x", "--folder", F, "--project", P]
    run_cli(monkeypatch, capsys, cat, "pull", *t, "--out", str(tmp_path))
    first = cat.project(F, P).object_version_lsn
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=2))
    deploy = ["deploy-packages", *t, str(tmp_path / "Load_A.dtsx")]
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    run_cli(monkeypatch, capsys, cat, *deploy, "--apply", "--plan-id", plan["plan_id"])
    cat.user = "LAB\\bob"
    cat.restore_project(F, P, first)                       # bob took my deploy off: that is his call to undo
    cat.user = "LAB\\me"
    (tmp_path / "Load_A.dtsx").write_bytes(dtsx("Load_A.dtsx", revision=3))
    code, plan = run_cli(monkeypatch, capsys, cat, *deploy)
    assert code == EXIT_REFUSED and any(c["check"] == "overwrite:Load_A.dtsx" for c in plan["checks"]
                                        if c["status"] == "error")
