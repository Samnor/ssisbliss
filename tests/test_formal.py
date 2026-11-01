"""The model checker (formal/model_check.py): the core properties hold on every action sequence up to depth 3,
and the checker catches each property's violation when a safety check is taken out of the tool."""
import sys
from pathlib import Path

import pytest

FORMAL = Path(__file__).resolve().parents[1] / "formal"
if not FORMAL.is_dir():   # the source archive ships the package's tests, not formal/
    pytest.skip("formal/ is only in the repository", allow_module_level=True)
sys.path.insert(0, str(FORMAL))
import model_check  # noqa: E402

from ssisbliss.ssis import deploy  # noqa: E402
from ssisbliss.ssis.commands import deploys  # noqa: E402


def test_every_sequence_up_to_depth_3_keeps_the_properties(tmp_path):
    stats = model_check.check(3, tmp_path, log=lambda *_: None)
    assert stats["violations"] == [] and stats["states"] > 20


def found(tmp_path, depth):
    stats = model_check.check(depth, tmp_path, log=lambda *_: None, first=True)
    assert stats["violations"], "the checker missed a planted bug"
    return stats["violations"][0]


def test_catches_a_deploy_that_ignores_your_base(tmp_path, monkeypatch):
    real = deploy.plan_packages
    monkeypatch.setattr(deploys, "plan_packages",
                        lambda cat, f, p, pkgs, **kw: real(cat, f, p, pkgs, **{**kw, "overwrite": {n for n, _ in pkgs}}))
    v = found(tmp_path, 3)
    assert v["property"] == "no-lost-work" and v["trace"][-1].startswith("deploy")


def test_catches_a_rollback_over_someone_elses_deploy(tmp_path, monkeypatch):
    real = deploy.plan_rollback
    monkeypatch.setattr(deploys, "plan_rollback", lambda *a, **kw: real(*a, **{**kw, "undo_others": True}))
    v = found(tmp_path, 2)
    assert v["property"] == "no-lost-work" and v["trace"][-1].startswith("rollback")


def test_catches_a_plan_id_that_does_not_bind_the_server_version(tmp_path, monkeypatch):
    def plan_id(self):   # without the target's lsn: a plan stays valid after the server moved
        import hashlib
        return hashlib.sha256(repr(sorted((c["package"], c["sha256"]) for c in self.to_deploy)).encode()).hexdigest()[:16]
    monkeypatch.setattr(deploy.Plan, "plan_id", property(plan_id))
    v = found(tmp_path, 4)
    assert v["property"] == "plan-binding" and v["trace"][-1].startswith("apply_later")
