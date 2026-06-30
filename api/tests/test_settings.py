from __future__ import annotations

from src.settings import Settings


def test_async_job_settings_have_sane_defaults():
    s = Settings(render_api_key="test-key-test-key")
    assert s.job_retention_seconds == 1800
    assert s.reaper_interval_seconds == 60
    assert s.max_pending_jobs == 20


def test_async_job_settings_are_overridable(monkeypatch):
    monkeypatch.setenv("JOB_RETENTION_SECONDS", "300")
    monkeypatch.setenv("MAX_PENDING_JOBS", "5")
    s = Settings(render_api_key="test-key-test-key")
    assert s.job_retention_seconds == 300
    assert s.max_pending_jobs == 5
