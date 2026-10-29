"""The tools on real packages from the field, kept only on this machine.

Put .dtsx, .ispac and .dtproj files (any folder depth) in private/fixtures/, or point
$SSISBLISS_PRIVATE_DIR at a folder with a fixtures/ subfolder. Both stay off GitHub (see
docs/private-data.md). Without them every test here is skipped, as in CI. Test ids use
numbers, not file names, so names do not end up in test reports either.
"""
import os
from pathlib import Path

import pytest

from ssisbliss.ssis.build import build
from ssisbliss.ssis.dtsx import package_sha256, read_package
from ssisbliss.ssis.fake import _restored_package, stored_like_ssisdb
from ssisbliss.ssis.ispac import Ispac
from ssisbliss.ssis.outline import flatten, outline

ROOT = Path(os.environ.get("SSISBLISS_PRIVATE_DIR") or Path(__file__).resolve().parents[1] / "private") / "fixtures"


def found(suffix: str) -> list[Path]:
    return sorted(ROOT.rglob(f"*{suffix}")) if ROOT.is_dir() else []


def cases(suffix: str):
    files = found(suffix)
    if not files:
        return [pytest.param(None, marks=pytest.mark.skip(reason=f"no private {suffix} fixtures"))]
    return [pytest.param(f, id=f"{suffix[1:]}-{i}") for i, f in enumerate(files)]


@pytest.mark.parametrize("path", cases(".dtsx"))
def test_package_reads_outlines_and_survives_the_server_resave(path):
    data = path.read_bytes()
    info = read_package(path.name, data)
    assert info.protection_level is not None
    tasks = flatten(outline(data)["tasks"])
    assert isinstance(tasks, list)
    # Overwrite protection relies on this: SSISDB's re-save must not look like someone's change.
    assert package_sha256(_restored_package(data)) == package_sha256(data)


@pytest.mark.parametrize("path", cases(".ispac"))
def test_ispac_packages_hash_the_same_after_the_server_stores_them(path):
    data = path.read_bytes()
    assert Ispac(stored_like_ssisdb(data)).package_hashes() == Ispac(data).package_hashes()


@pytest.mark.parametrize("path", cases(".dtproj"))
def test_dtproj_builds_or_refuses_with_a_reason(path):
    try:
        data, report = build(path)
    except ValueError as e:          # e.g. EncryptSensitiveWithUserKey: refused with what to do instead
        assert str(e)
        return
    assert set(Ispac(data).package_names) == set(report["packages"])
