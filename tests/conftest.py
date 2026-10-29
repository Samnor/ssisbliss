import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Base records live in the user's state folder; keep tests out of the real one."""
    monkeypatch.setenv("ONPREM_STATE_DIR", str(tmp_path / "onprem-state"))
    monkeypatch.setenv("ONPREM_SSIS_CONFIG", str(tmp_path / "no-user-config.toml"))
