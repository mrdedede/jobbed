"""paths.py resolves its locations from the environment."""

import importlib

from job_scraper import paths


def test_data_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBBED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBBED_BOARDS_CSV", str(tmp_path / "b.csv"))
    try:
        importlib.reload(paths)
        assert paths.TEMP == tmp_path / "data"
        assert paths.JOBS_CSV == tmp_path / "data" / "jobs.csv"
        assert paths.BOARDS_CSV == tmp_path / "b.csv"
        assert not paths.TEMP.exists()  # no import-time side effects
    finally:
        monkeypatch.undo()
        importlib.reload(paths)


def test_credentials_file_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANCE_TRAVAIL_CREDENTIALS_FILE", str(tmp_path / "c.json"))
    try:
        importlib.reload(paths)
        assert paths.FRANCE_TRAVAIL_CREDENTIALS == tmp_path / "c.json"
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
