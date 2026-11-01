"""ssisbliss build against Visual Studio (devenv) and SSISBuild.exe output for the same projects.

tests/fixtures/build/<variant>/: src/ is the Visual Studio project, devenv-<config>.ispac and
ssisbuild-<config>.ispac what the two Microsoft builds produced from it in the lab
(lab/tools/build-corpus.ps1).
"""
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from ssisbliss.ssis.build import BuildError, build
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.ispac import Ispac
from ssisbliss.ssis.outline import outline

FIX = Path(__file__).parent / "fixtures" / "build"
BUILT = ["basic", "configs", "drift", "pkgparams"]


def ref(variant: str, tool: str, config: str = "Development") -> Ispac:
    return Ispac((FIX / variant / f"{tool}-{config}.ispac").read_bytes())


def ours(variant: str, config: str | None = None) -> tuple[Ispac, dict]:
    data, report = build(FIX / variant / "src" / "Corpus.dtproj", config)
    return Ispac(data), report


def param_values(ispac: Ispac) -> dict[str, str]:
    text = ispac.parts["Project.params"][1].decode("utf-8-sig")
    return dict(re.findall(r'SSIS:Name="(\w+)">.*?SSIS:Name="Value"[^>]*>([^<]*)<', text, re.S))


def package_meta(ispac: Ispac, name: str) -> dict:
    m = ispac.manifest
    ns = {"SSIS": "www.microsoft.com/SqlServer/SSIS"}
    meta = next(x for x in m.findall("SSIS:DeploymentInfo/SSIS:PackageInfo/SSIS:PackageMetaData", ns)
                if x.get("{www.microsoft.com/SqlServer/SSIS}Name") == name)
    props = {p.get("{www.microsoft.com/SqlServer/SSIS}Name"): (p.text or "").strip()
             for p in meta.findall("SSIS:Properties/SSIS:Property", ns)}
    params = sorted(p.get("{www.microsoft.com/SqlServer/SSIS}Name") for p in meta.findall("SSIS:Parameters/SSIS:Parameter", ns)
                    if not p.get("{www.microsoft.com/SqlServer/SSIS}Name", "").startswith("CM."))
    return {**{k: props.get(k) for k in ("Name", "VersionMajor", "VersionMinor", "VersionBuild", "Description",
                                         "ProtectionLevel", "PackageFormatVersion")}, "parameters": params}


def entry_points(ispac: Ispac) -> dict[str, str]:
    ns = {"SSIS": "www.microsoft.com/SqlServer/SSIS"}
    return {p.get("{www.microsoft.com/SqlServer/SSIS}Name"): p.get("{www.microsoft.com/SqlServer/SSIS}EntryPoint")
            for p in ispac.manifest.findall("SSIS:Packages/SSIS:Package", ns)}


@pytest.mark.parametrize("variant", BUILT)
def test_same_parts_as_both_microsoft_builds(variant):
    mine, _ = ours(variant)
    with zipfile.ZipFile(FIX / variant / "devenv-Development.ispac") as z:
        theirs = sorted(unquote(i.filename) for i in z.infolist())
    assert sorted(mine.parts) == theirs == sorted(ref(variant, "ssisbuild").parts)
    assert mine.parts["@Project.manifest"][1].startswith(b"<SSIS:Project")   # no XML declaration, like both


@pytest.mark.parametrize("variant", BUILT)
def test_packages_are_included_as_they_are_like_ssisbuild(variant):
    mine, _ = ours(variant)
    sb = ref(variant, "ssisbuild")
    for name in mine.package_names:
        assert mine.package(name) == (FIX / variant / "src" / name).read_bytes()
        # SSISBuild re-serializes package parameters when it applies a configuration, adding the
        # default DTS:Sensitive="False"; the content is otherwise the same.
        theirs = sb.package(name).replace(b'\n      DTS:Sensitive="False"', b"")
        assert package_sha256(mine.package(name)) == package_sha256(theirs)


@pytest.mark.parametrize("variant", BUILT)
def test_packages_do_the_same_as_visual_studios_resaved_copies(variant):
    # Visual Studio re-saves packages with its own runtime (newer version stamps, extra connection
    # properties); what the packages do is the same.
    mine, _ = ours(variant)
    vs = ref(variant, "devenv")
    for name in mine.package_names:
        strip = lambda o: [(t["name"], t["kind"], t.get("sql")) for t in o["tasks"]]  # noqa: E731
        assert strip(outline(mine.package(name))) == strip(outline(vs.package(name)))


@pytest.mark.parametrize("variant", BUILT)
def test_manifest_matches_visual_studio(variant):
    mine, _ = ours(variant)
    vs = ref(variant, "devenv")
    assert entry_points(mine) == entry_points(vs)
    assert mine.protection_level == vs.protection_level
    for name in mine.package_names:
        assert package_meta(mine, name) == package_meta(vs, name), name


@pytest.mark.parametrize("variant,config", [("basic", "Development"), ("configs", "Development"), ("configs", "Release")])
def test_configuration_values_go_into_project_params(variant, config):
    mine, report = ours(variant, config)
    assert param_values(mine) == param_values(ref(variant, "ssisbuild", config))
    if config == "Development":
        assert param_values(mine) == param_values(ref(variant, "devenv", config))
    if variant == "configs":
        assert report["applied_configuration"]["TargetDb"] == {"Development": "DEV_STAGE", "Release": "PROD_STAGE"}[config]


def test_drift_refreshes_metadata_like_visual_studio_not_ssisbuild():
    mine, report = ours("drift")
    assert package_meta(mine, "Load_B.dtsx")["Description"] == "edited outside Visual Studio"
    assert package_meta(ref("drift", "ssisbuild"), "Load_B.dtsx")["Description"] == ""   # SSISBuild ships it stale
    assert any(c.startswith("Description") for c in report["refreshed_metadata"]["Load_B.dtsx"])


def test_user_key_projects_are_refused_with_a_way_out():
    with pytest.raises(BuildError, match="EncryptSensitiveWithUserKey.*DontSaveSensitive"):
        ours("userkey")


def test_unknown_configuration_is_refused():
    with pytest.raises(BuildError, match="no configuration 'Nope'.*Development"):
        ours("configs", "Nope")


# ------------------------------------------------------- deploy from a .dtproj --

def _project_copy(tmp_path, variant="basic"):
    import shutil
    dest = tmp_path / "Corpus"
    shutil.copytree(FIX / variant / "src", dest)
    return dest


def _cli(monkeypatch, capsys, cat, *argv):
    import json
    from ssisbliss.ssis import cli
    from ssisbliss.ssis.commands import common, deploys, read
    for module in (common, deploys, read):   # each command module imports _catalog
        monkeypatch.setattr(module, "_catalog", lambda env, a: cat)
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _server_with(project_dir):
    from ssisbliss.ssis.fake import FakeCatalog
    cat = FakeCatalog()
    data, _ = build(project_dir / "Corpus.dtproj")
    cat.add_project("CORPUS", "Corpus", data)
    return cat


def _edit(path, note):
    text = path.read_text("utf-8-sig")
    path.write_text(text.replace('DTS:ObjectName="', f'DTS:Description="{note}"\n  DTS:ObjectName="', 1))


T = ["--server", "x", "--folder", "CORPUS", "--project", "Corpus"]


def test_deploy_project_from_dtproj_only_one_package(tmp_path, monkeypatch, capsys):
    proj = _project_copy(tmp_path)
    cat = _server_with(proj)
    assert _cli(monkeypatch, capsys, cat, "pull", *T, "--out", str(proj), "--packages", "Load_B.dtsx")[0] == 0
    (proj / "Load_B.server.dtsx").unlink(missing_ok=True)
    _edit(proj / "Load_B.dtsx", "my change")
    code, plan = _cli(monkeypatch, capsys, cat, "deploy-project", *T, "--dtproj", str(proj / "Corpus.dtproj"),
                      "--only", "Load_B")
    assert code == 0, plan
    assert [c["package"] for c in plan["changes"] if c["change"] == "update"] == ["Load_B.dtsx"]
    assert plan["build"]["refreshed_metadata"]["Load_B.dtsx"]


def test_deploy_project_all_checks_every_package_for_others_changes(tmp_path, monkeypatch, capsys):
    proj = _project_copy(tmp_path)
    cat = _server_with(proj)
    code, out = _cli(monkeypatch, capsys, cat, "pull", *T, "--out", str(proj))
    assert code == 0
    for f in proj.glob("*.server.dtsx"):
        f.unlink()
    _edit(proj / "Load_A.dtsx", "mine")
    # A colleague changes Load_B on the server after the pull: --all must refuse to overwrite it.
    colleague = (proj / "Load_B.dtsx").read_bytes().replace(b'DTS:ObjectName="Load_B"',
                                                             b'DTS:Description="theirs"\n  DTS:ObjectName="Load_B"', 1)
    cat.someone_else_deploys("CORPUS", "Corpus", "Load_B.dtsx", colleague)
    _edit(proj / "Load_B.dtsx", "my stale copy")
    code, plan = _cli(monkeypatch, capsys, cat, "deploy-project", *T, "--dtproj", str(proj / "Corpus.dtproj"), "--all")
    assert code == 2 and not plan["can_apply"]
    errors = {c["check"] for c in plan["checks"] if c["status"] == "error"}
    assert errors == {"overwrite:Load_B.dtsx"}, plan["checks"]
    assert "only on the server are kept" in plan["all"]


def test_deploy_project_needs_only_or_all(tmp_path, monkeypatch, capsys):
    proj = _project_copy(tmp_path)
    cat = _server_with(proj)
    code, out = _cli(monkeypatch, capsys, cat, "deploy-project", *T, "--dtproj", str(proj / "Corpus.dtproj"))
    assert code == 2 and "--all" in out["error"]


# ------------------------------------- what the server does on deploy_packages --

def test_package_identity_ignores_what_the_server_rewrites():
    # Visual Studio's re-saved package vs the source: same package once the server re-saves it.
    vs = ref("configs", "devenv").package("Load_B.dtsx")
    src = (FIX / "configs" / "src" / "Load_B.dtsx").read_bytes()
    assert b'LastModifiedProductVersion="17.0' in vs and b"ConnectRetryCount" in vs
    assert b"Initial Catalog=DEV_STAGE" in vs and b"Initial Catalog=STAGE" in src   # expression evaluated by VS
    assert package_sha256(vs) == package_sha256(src)


def test_package_identity_still_sees_real_changes():
    src = (FIX / "basic" / "src" / "Load_B.dtsx").read_bytes()
    assert package_sha256(src.replace(b"step 1", b"step one")) != package_sha256(src)            # SQL changed
    assert package_sha256(src.replace(b"@[$Project::TargetDb]", b"@[$Project::LoadMode]")) != package_sha256(src)  # expression


def test_rebuild_then_deploy_package_over_a_visual_studio_deployed_project(tmp_path, monkeypatch, capsys):
    # The server holds Visual Studio's build; the developer edits Load_B, rebuilds, deploys only Load_B.
    # On deploy_packages the server re-saves every package (dropping VS's newer properties), which
    # must not count as a change to Load_A.
    from ssisbliss.ssis.fake import FakeCatalog
    proj = _project_copy(tmp_path, "configs")
    cat = FakeCatalog()
    cat.add_project("CORPUS", "Corpus", (FIX / "configs" / "devenv-Development.ispac").read_bytes())
    assert _cli(monkeypatch, capsys, cat, "pull", *T, "--out", str(proj), "--packages", "Load_B.dtsx")[0] == 0
    for f in proj.glob("*.server.dtsx"):
        f.unlink()
    _edit(proj / "Load_B.dtsx", "developer change")
    args = ["deploy-packages", *T, str(proj / "Load_B.dtsx"), "--backup-dir", str(tmp_path / "bk")]
    code, plan = _cli(monkeypatch, capsys, cat, *args)
    assert code == 0, plan
    code, out = _cli(monkeypatch, capsys, cat, *args, "--apply", "--plan-id", plan["plan_id"])
    assert out["status"] == "verified", out


def test_no_dtproj_uses_the_one_project_holding_the_packages_and_apply_must_name_it(tmp_path, monkeypatch, capsys):
    proj = _project_copy(tmp_path)
    cat = _server_with(proj)
    for f in proj.glob("*.dtproj"):
        f.unlink()
    (tmp_path / "ssisbliss.toml").write_text('[environments.dev]\nserver = "x"\n')
    monkeypatch.chdir(tmp_path)
    status = ["status", "--env", "dev", "--dir", str(proj)]
    code, out = _cli(monkeypatch, capsys, cat, *status)
    assert code == 0 and "only project on the server" in out["target_from"]["project"]
    assert out["target_from"]["folder"].startswith("CORPUS ")

    # A plan shows the target and its `next` names it; the same command with --apply is refused without one.
    _edit(proj / "Load_B.dtsx", "my change")
    deploy = ["deploy-packages", "--env", "dev", str(proj / "Load_B.dtsx"), "--overwrite", "Load_B.dtsx",
              "--backup-dir", str(tmp_path / "bk")]
    code, plan = _cli(monkeypatch, capsys, cat, *deploy)
    assert code == 0 and (plan["target"]["folder"], plan["target"]["project"]) == ("CORPUS", "Corpus"), plan
    assert "--folder CORPUS --project Corpus --apply --plan-id " + plan["plan_id"] in plan["next"][0]
    lsn = next(iter(cat.projects.values())).lsn
    code, out = _cli(monkeypatch, capsys, cat, *deploy, "--apply", "--plan-id", plan["plan_id"])
    assert code == 2 and out["error"].startswith("give --project and --folder: no Visual Studio project")
    assert out["error"].endswith("add --folder CORPUS --project Corpus") and "target_from" not in out
    assert next(iter(cat.projects.values())).lsn == lsn

    # A folder without packages of this project is not offered, and the error still refuses.
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = _cli(monkeypatch, capsys, cat, "status", "--env", "dev", "--dir", str(empty))
    assert code == 2 and "Projects on the server (folder/project): CORPUS/Corpus;" in out["error"]

    # Two projects hold the packages: both are named, none is picked.
    data = next(iter(cat.projects.values())).ispac
    cat.add_project("OTHER", "Corpus", data)
    code, out = _cli(monkeypatch, capsys, cat, *status)
    assert code == 2 and "CORPUS/Corpus, OTHER/Corpus" in out["error"] and "add --folder" not in out["error"]

    # The hint must not hide the error when the server cannot answer.
    monkeypatch.setattr(cat, "list_projects", lambda folder: 1 / 0)
    code, out = _cli(monkeypatch, capsys, cat, *status)
    assert code == 2 and out["error"].endswith("or above it")


def test_folder_and_project_default_like_the_visual_studio_wizard_answers(tmp_path, monkeypatch, capsys):
    # Visual Studio asks for the server and SSISDB path on every deploy and remembers neither; ssisbliss takes the
    # project from the .dtproj, and the folder from your last deploy or pull of it, else ssisbliss.toml.
    proj = _project_copy(tmp_path)
    cat = _server_with(proj)
    cat.add_project("OTHER", "Corpus", build(proj / "Corpus.dtproj")[0])
    deploy = ["deploy-project", "--env", "dev", "--dtproj", str(proj / "Corpus.dtproj"), "--only", "Load_B"]
    (tmp_path / "ssisbliss.toml").write_text('[environments.dev]\nserver = "x"\n')
    monkeypatch.chdir(tmp_path)
    code, out = _cli(monkeypatch, capsys, cat, *deploy)
    assert code == 2 and out["error"].startswith("give --folder") and "Corpus.dtproj" in out["target_from"]["project"]
    assert "CORPUS/Corpus, OTHER/Corpus" in out["error"]

    (tmp_path / "ssisbliss.toml").write_text('[environments.dev]\nserver = "x"\nfolder = "CORPUS"\n')
    _edit(proj / "Load_B.dtsx", "my change")
    code, plan = _cli(monkeypatch, capsys, cat, *deploy)
    assert (plan["target"]["folder"], plan["target"]["project"]) == ("CORPUS", "Corpus"), plan
    assert "ssisbliss.toml" in plan["target_from"]["folder"]

    # Your own pull or deploy wins over the file: that is the folder you work in.
    assert _cli(monkeypatch, capsys, cat, "pull", "--env", "dev", "--folder", "OTHER", "--out", str(proj),
                "--packages", "Load_A.dtsx")[0] == 0
    code, out = _cli(monkeypatch, capsys, cat, "status", "--env", "dev", "--dir", str(proj))
    assert code == 0 and out["target_from"]["folder"].startswith("OTHER (where you last deployed or pulled Corpus")
    mine = [*deploy, "--folder", "CORPUS", "--overwrite", "Load_B.dtsx"]
    code, plan = _cli(monkeypatch, capsys, cat, *mine)
    code, out = _cli(monkeypatch, capsys, cat, *mine, "--apply", "--plan-id", plan["plan_id"],
                     "--backup-dir", str(tmp_path / "bk"))
    assert out["status"] == "verified", out
    code, plan = _cli(monkeypatch, capsys, cat, *deploy)
    assert plan["target"]["folder"] == "CORPUS" and "also deployed to OTHER" in plan["target_from"]["folder"]

    # Given flags are used as they are, with nothing to explain.
    code, plan = _cli(monkeypatch, capsys, cat, *deploy, "--folder", "OTHER", "--project", "Corpus")
    assert "target_from" not in plan
