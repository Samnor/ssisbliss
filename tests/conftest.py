import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Base records live in the user's state folder; keep tests out of the real one."""
    monkeypatch.setenv("SSISBLISS_STATE_DIR", str(tmp_path / "ssisbliss-state"))
    monkeypatch.setenv("SSISBLISS_CONFIG", str(tmp_path / "no-user-config.toml"))
