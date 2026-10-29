"""Before deploy-package.ps1: Visual Studio's build of the corpus project deployed to /SSISDB/PKGCMP/Corpus,
and the developer's copy of the project with Load_B edited. Needs lab/tools/build-corpus.ps1 first."""
import json, re, shutil, subprocess, time
from pathlib import Path
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.ispac import Ispac

CORPUS, PROJ = Path(r"C:\lab\work\corpus\configs"), Path(r"C:\lab\work\vsui\Corpus")
c = SqlCatalog(connection_string("localhost,14331", trust_server_certificate=True, app="onprem-compare"))
# Visual Studio from a previous run holds the project's files open; deploy-package.ps1 starts it again.
subprocess.run(["taskkill", "/IM", "devenv.exe", "/F"], capture_output=True)
for attempt in range(10):
    try:
        if PROJ.exists():
            shutil.rmtree(PROJ)
        break
    except PermissionError:
        time.sleep(3)
shutil.copytree(CORPUS / "src", PROJ)
c.conn.cursor().execute("IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'PKGCMP') EXEC catalog.create_folder @folder_name = N'PKGCMP'")
c.deploy_project("PKGCMP", "Corpus", (CORPUS / "build-devenv" / "bin" / "Development" / "Corpus.ispac").read_bytes())
text = re.sub(r'\s+DTS:Description="[^"]*"', "", (PROJ / "Load_B.dtsx").read_text("utf-8-sig"), count=1)
(PROJ / "Load_B.dtsx").write_text(text.replace('DTS:ObjectName="', 'DTS:Description="changed in Visual Studio"\n  DTS:ObjectName="', 1), "utf-8")
p = c.project("PKGCMP", "Corpus")
doc = {"baseline_lsn": p.object_version_lsn, "hashes": Ispac(c.get_project("PKGCMP", "Corpus")).package_hashes()}
(PROJ.parent / "baseline.json").write_text(json.dumps(doc))
print(json.dumps(doc))
