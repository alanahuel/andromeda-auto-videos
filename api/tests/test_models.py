from __future__ import annotations

from shared.models import ErrorResponse, JobStatusResponse


def test_job_status_response_defaults_optional_fields_to_none():
    r = JobStatusResponse(job_id="j1", status="queued", code="ok")
    assert r.status == "queued"
    assert r.code == "ok"
    assert r.duration_seconds is None
    assert r.concat_strategy is None
    assert r.error is None


def test_job_status_response_carries_done_metadata():
    r = JobStatusResponse(
        job_id="j2", status="done", code="ok",
        duration_seconds=42.5, concat_strategy="fast",
    )
    assert r.duration_seconds == 42.5
    assert r.concat_strategy == "fast"


def test_error_response_accepts_new_codes():
    for code in ("not_found", "not_ready", "too_busy"):
        assert ErrorResponse(error="x", code=code, job_id=None).code == code
