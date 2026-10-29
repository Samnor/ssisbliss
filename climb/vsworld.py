"""Visual Studio project scenarios: the developer works in a real .dtproj project (tests/fixtures/build)
and asks for what they would do in Visual Studio (Rebuild + Deploy Package, Deploy). The fake servers
hold the project as Visual Studio's build deployed it, re-saved like SQL Server 2019 does."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.fake import FakeCatalog
from world import COLLEAGUE, SERVERS, World, git

BUILDS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "build"


class VsWorld(World):
    folder = "CORPUS"
    project = "Corpus"
    project_folder = "Corpus"
    packages = ["Load_A.dtsx", "Load_B.dtsx"]

    @classmethod
    def build_vs(cls, root: Path, variant: str = "basic") -> "VsWorld":
        w = cls(root)
        deployed = (BUILDS / variant / "devenv-Development.ispac").read_bytes()
        for env, server_name in SERVERS.items():
            c = FakeCatalog(server_name, major=15, user="LAB\\alice")
            c.save(w.server_dir(env))
            c.add_project(cls.folder, cls.project, deployed, deployed_by="LAB\\alice",
                          bindings=[{"object_type": 20, "object_name": cls.project, "parameter_name": "TargetDb",
                                     "value_type": "R", "referenced_variable_name": "TargetDb",
                                     "sensitive": False, "value": None}],
                          env_refs=[{"reference_id": 1, "reference_type": "R", "environment_folder_name": None,
                                     "environment_name": "Default"}])
            c.save()
        w.work.mkdir(parents=True)
        shutil.copytree(BUILDS / variant / "src", w.project_dir)
        prod_rules = 'require_git_committed = true\nrequire_git_pushed = true\nconfirm = true\n'
        (w.work / "onprem-ssis.toml").write_text("".join(
            f'[environments.{env}]\nserver = "fake://{env}"\n'
            f'server_name = "{name.replace(chr(92), chr(92) * 2)}"\n{prod_rules if env == "prod" else ""}\n'
            for env, name in SERVERS.items()))
        (w.work / ".gitignore").write_text(".ssis-base.json\n*.server.dtsx\nbin/\nobj/\n")
        git(w.work, "init", "-q", "-b", "main")
        git(w.work, "add", "-A")
        git(w.work, "commit", "-q", "-m", "Corpus project as released")
        w.add_team_remote()
        return w

    def edit(self, name: str, note: str) -> bytes:
        """A change made in Visual Studio's designer: here, the package description."""
        path = self.project_dir / name
        text = re.sub(r'\s+DTS:Description="[^"]*"', "", path.read_text("utf-8-sig"), count=1)
        text = text.replace('DTS:ObjectName="', f'DTS:Description="{note}"\n  DTS:ObjectName="', 1)
        path.write_text(text, "utf-8")
        return path.read_bytes()

    def local_is_deployed(self, env: str, name: str) -> bool:
        return self.hashes(env).get(name) == package_sha256((self.project_dir / name).read_bytes())

    def add_package(self, name: str, like: str = "Load_A.dtsx") -> None:
        """A package added to the project in Visual Studio: the .dtsx plus its entry in the .dtproj."""
        text = (self.project_dir / like).read_text("utf-8-sig")
        text = re.sub(r'DTS:DTSID="\{[^}]+\}"', f'DTS:DTSID="{{{str(uuid.uuid4()).upper()}}}"', text, count=1)
        text = text.replace(f'DTS:ObjectName="{like[:-5]}"', f'DTS:ObjectName="{name[:-5]}"', 1)
        (self.project_dir / name).write_text(text, "utf-8")
        dtproj = self.project_dir / "Corpus.dtproj"
        p = dtproj.read_text("utf-8-sig")
        anchor = f'<SSIS:Package SSIS:Name="{like}" SSIS:EntryPoint="1" />'
        assert anchor in p
        dtproj.write_text(p.replace(anchor, anchor + f'\n    <SSIS:Package SSIS:Name="{name}" SSIS:EntryPoint="1" />', 1),
                          "utf-8")


def colleague_change(w: VsWorld, env: str, name: str) -> None:
    data = (w.project_dir / name).read_bytes().replace(b'DTS:ObjectName="', b'DTS:Description="Bob\'s fix"\n  DTS:ObjectName="', 1)
    w.deploy_as(env, COLLEAGUE, name, data)
