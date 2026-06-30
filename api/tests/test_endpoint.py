"""Integration tests for the async job contract (run_pipeline mocked).

POST /jobs schedules a FastAPI BackgroundTask; under the sync TestClient that
task runs to completion before the POST call returns, so after POST the job is
already `done` (or `failed`) and observable via GET. Transient states
(`queued`/`processing`, `not_ready`, `too_busy`) are driven by seeding the
registry directly, which needs no background timing.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RENDER_API_KEY", "test-key-test-key-test-key")

from shared.models import JobParams  # noqa: E402
from src import job_registry  # noqa: E402
from src.ffmpeg_pipeline import PipelineResult, _FriendlyError  # noqa: E402
from src.main import app  # noqa: E402


API_KEY = os.environ["RENDER_API_KEY"]
client = TestClient(app)
_FAKE_MP4 = b"\x00\x00\x00\x20ftypmp42" + b"\xde\xad\xbe\xef" * 32


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def _multipart_files() -> dict:
    return {
        "clip_hook": ("hook.mp4", b"hook-bytes", "video/mp4"),
        "clip_cuerpo": ("cuerpo.mp4", b"cuerpo-bytes", "video/mp4"),
        "clip_cta": ("cta.mp4", b"cta-bytes", "video/mp4"),
        "music": ("music.mp3", b"music-bytes", "audio/mpeg"),
    }


def _params(**overrides) -> str:
    base = {
        "orientation": "vertical", "music_volume": 0.3, "fade_in": 2.0,
        "fade_out": 2.0, "output_name": "ad_2026_05_test",
    }
    base.update(overrides)
    return json.dumps(base)


def _fake_pipeline(*, output: Path, **_kw) -> PipelineResult:
    output.write_bytes(_FAKE_MP4)
    return PipelineResult(duration_seconds=42.5, concat_strategy="fast")


def _post(files=None, params=None):
    return client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": params or _params()},
        files=_multipart_files() if files is None else files,
    )


# ---- POST /jobs ----------------------------------------------------------

def test_post_returns_202_with_job_id():
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        resp = _post()
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    uuid.UUID(body["job_id"])
    assert resp.headers["x-status-code"] == "ok"
    assert resp.headers["x-job-id"] == body["job_id"]


def test_post_422_when_no_clips_uploaded():
    resp = client.post("/jobs", headers={"X-API-Key": API_KEY}, data={"params": _params()})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_params"


def test_post_422_on_invalid_params_json():
    resp = _post(params="{not json")
    assert resp.status_code == 422
    assert resp.headers["x-status-code"] == "invalid_params"
    assert resp.json()["job_id"] is None


def test_post_rejects_missing_api_key():
    resp = client.post("/jobs", data={"params": _params()}, files=_multipart_files())
    assert resp.status_code == 401


def test_post_429_when_queue_full():
    # Seed max_pending queued jobs so the next POST is rejected.
    from src.settings import get_settings
    for i in range(get_settings().max_pending_jobs):
        wd = Path(f"/tmp/seed_{i}")
        job_registry.create(
            f"seed{i}", output_name="x", workdir=wd, output_path=wd / "x.mp4",
            clip_paths=[], music_path=None,
            params=JobParams(orientation="vertical", output_name="x"),
            retention_seconds=1800,
        )
    resp = _post()
    assert resp.status_code == 429
    assert resp.json()["code"] == "too_busy"
    assert resp.headers["x-status-code"] == "too_busy"


# ---- GET /jobs/{id} ------------------------------------------------------

def test_status_then_result_happy_path():
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        job_id = _post().json()["job_id"]

    s = client.get(f"/jobs/{job_id}", headers={"X-API-Key": API_KEY})
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "done"
    assert body["code"] == "ok"
    assert body["duration_seconds"] == 42.5
    assert body["concat_strategy"] == "fast"

    r = client.get(f"/jobs/{job_id}/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["x-status-code"] == "ok"
    assert r.headers["x-concat-strategy"] == "fast"
    assert 'filename="ad_2026_05_test.mp4"' in r.headers["content-disposition"]
    assert r.content == _FAKE_MP4


def test_status_unknown_job_404():
    s = client.get("/jobs/does-not-exist", headers={"X-API-Key": API_KEY})
    assert s.status_code == 404
    assert s.json()["code"] == "not_found"


def test_status_requires_api_key():
    assert client.get("/jobs/whatever").status_code == 401


def test_failed_render_status_and_result():
    def _boom(**_kw):
        raise _FriendlyError("clip corrupto", code="clip_unreadable")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        job_id = _post().json()["job_id"]

    s = client.get(f"/jobs/{job_id}", headers={"X-API-Key": API_KEY})
    assert s.status_code == 200
    assert s.json()["status"] == "failed"
    assert s.json()["code"] == "clip_unreadable"
    assert s.headers["x-status-code"] == "clip_unreadable"

    r = client.get(f"/jobs/{job_id}/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 422  # clip_unreadable → 422
    assert r.json()["code"] == "clip_unreadable"


# ---- GET /jobs/{id}/result transient states -----------------------------

def test_result_409_while_processing():
    # Seed a job that never runs: still queued → /result not ready.
    wd = Path("/tmp/seed_pending")
    job_registry.create(
        "pending1", output_name="x", workdir=wd, output_path=wd / "x.mp4",
        clip_paths=[], music_path=None,
        params=JobParams(orientation="vertical", output_name="x"),
        retention_seconds=1800,
    )
    r = client.get("/jobs/pending1/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 409
    assert r.json()["code"] == "not_ready"


def test_result_unknown_job_404():
    r = client.get("/jobs/nope/result", headers={"X-API-Key": API_KEY})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_health_ok_no_auth_required():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
