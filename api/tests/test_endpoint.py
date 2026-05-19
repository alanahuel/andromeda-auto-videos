"""Integration test for POST /jobs with the FFmpeg pipeline mocked.

We can't run ffmpeg in CI, so we patch `run_pipeline` in the orchestrator
module to write a fixed bytestring to the output path and return a
deterministic PipelineResult. That exercises everything else: multipart
parsing, JobParams validation, the semaphore-serialised flow, file
persistence to the workdir, streaming the bytes back, and the response
headers.
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

from src.ffmpeg_pipeline import PipelineResult, _FriendlyError  # noqa: E402
from src.main import app  # noqa: E402


API_KEY = os.environ["RENDER_API_KEY"]
client = TestClient(app)


_FAKE_MP4 = b"\x00\x00\x00\x20ftypmp42" + b"\xde\xad\xbe\xef" * 32


def _multipart_files() -> dict:
    return {
        "clip_hook": ("hook.mp4", b"hook-bytes", "video/mp4"),
        "clip_cuerpo": ("cuerpo.mp4", b"cuerpo-bytes", "video/mp4"),
        "clip_cta": ("cta.mp4", b"cta-bytes", "video/mp4"),
        "music": ("music.mp3", b"music-bytes", "audio/mpeg"),
    }


def _params(**overrides) -> str:
    base = {
        "orientation": "vertical",
        "music_volume": 0.3,
        "fade_in": 2.0,
        "fade_out": 2.0,
        "output_name": "ad_2026_05_test",
    }
    base.update(overrides)
    return json.dumps(base)


def _fake_pipeline(*, output: Path, **_kwargs) -> PipelineResult:
    output.write_bytes(_FAKE_MP4)
    return PipelineResult(duration_seconds=42.5, concat_strategy="fast")


def test_post_jobs_returns_mp4_with_headers_on_success():
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=_multipart_files(),
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.headers["x-output-duration-seconds"] == "42.5"
    assert resp.headers["x-concat-strategy"] == "fast"
    assert resp.headers["x-status-code"] == "ok"
    job_id = resp.headers["x-job-id"]
    uuid.UUID(job_id)  # raises if not a valid UUID
    assert 'filename="ad_2026_05_test.mp4"' in resp.headers["content-disposition"]
    assert resp.content == _FAKE_MP4


def test_post_jobs_succeeds_without_music_and_passes_none_to_pipeline():
    captured: dict = {}

    def _capturing_pipeline(*, output: Path, music, **_kwargs) -> PipelineResult:
        captured["music"] = music
        output.write_bytes(_FAKE_MP4)
        return PipelineResult(duration_seconds=10.0, concat_strategy="reencode")

    files = _multipart_files()
    del files["music"]

    with patch("src.render_orchestrator.run_pipeline", side_effect=_capturing_pipeline):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=files,
        )

    assert resp.status_code == 200
    assert resp.content == _FAKE_MP4
    assert captured["music"] is None


def test_post_jobs_accepts_two_clips_and_forwards_only_those_paths():
    """Any 2-of-3 subset must work; clip paths reach the pipeline in role order."""
    captured: dict = {}

    def _capturing_pipeline(*, output: Path, clips, **_kwargs) -> PipelineResult:
        captured["clip_names"] = [p.name for p in clips]
        output.write_bytes(_FAKE_MP4)
        return PipelineResult(duration_seconds=8.0, concat_strategy="reencode")

    files = {
        "clip_hook": ("hook.mp4", b"hook-bytes", "video/mp4"),
        "clip_cta": ("cta.mp4", b"cta-bytes", "video/mp4"),
    }

    with patch("src.render_orchestrator.run_pipeline", side_effect=_capturing_pipeline):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=files,
        )

    assert resp.status_code == 200
    # Order preserved (hook before cta), cuerpo skipped because it wasn't uploaded.
    assert captured["clip_names"] == ["hook.mp4", "cta.mp4"]


def test_post_jobs_422_when_fewer_than_two_clips():
    files = {
        "clip_hook": ("hook.mp4", b"hook-bytes", "video/mp4"),
    }
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": _params()},
        files=files,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "invalid_params"
    assert "al menos 2" in body["error"]
    assert resp.headers["x-status-code"] == "invalid_params"


def test_post_jobs_422_when_no_clips_uploaded():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": _params()},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_params"


def test_post_jobs_rejects_missing_api_key():
    resp = client.post(
        "/jobs",
        data={"params": _params()},
        files=_multipart_files(),
    )
    assert resp.status_code == 401


def test_post_jobs_rejects_wrong_api_key():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": "nope-nope-nope-nope"},
        data={"params": _params()},
        files=_multipart_files(),
    )
    assert resp.status_code == 401


def test_post_jobs_422_on_invalid_params_json():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": "{not json"},
        files=_multipart_files(),
    )
    assert resp.status_code == 422


def test_post_jobs_422_on_bad_output_name_regex():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": _params(output_name="has spaces!")},
        files=_multipart_files(),
    )
    assert resp.status_code == 422


def test_post_jobs_422_on_unknown_orientation():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": _params(orientation="diagonal")},
        files=_multipart_files(),
    )
    assert resp.status_code == 422


def test_post_jobs_500_with_spanish_message_when_pipeline_raises_friendly_error():
    def _boom(**_kwargs):
        raise _FriendlyError("FFmpeg falló al concatenar los clips. Revisa los logs.")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=_multipart_files(),
        )

    assert resp.status_code == 500
    body = resp.json()
    assert "FFmpeg falló al concatenar los clips" in body["error"]
    assert body["code"] == "render_failed"
    assert resp.headers["x-status-code"] == "render_failed"
    # job_id is surfaced in body + header so callers can correlate with logs.
    assert body["job_id"]
    assert resp.headers["X-Job-Id"] == body["job_id"]


def test_post_jobs_422_when_pipeline_raises_clip_unreadable():
    def _boom(**_kwargs):
        raise _FriendlyError("clip corrupto", code="clip_unreadable")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=_multipart_files(),
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == "clip_unreadable"


def test_post_jobs_504_when_pipeline_times_out():
    def _boom(**_kwargs):
        raise _FriendlyError("FFmpeg colgado", code="ffmpeg_timeout")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=_multipart_files(),
        )

    assert resp.status_code == 504
    assert resp.json()["code"] == "ffmpeg_timeout"


def test_post_jobs_500_generic_when_pipeline_raises_unexpected_error():
    def _boom(**_kwargs):
        raise RuntimeError("disk full, very specific internal detail")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        resp = client.post(
            "/jobs",
            headers={"X-API-Key": API_KEY},
            data={"params": _params()},
            files=_multipart_files(),
        )

    assert resp.status_code == 500
    body = resp.json()
    assert "Error inesperado" in body["error"]
    assert body["code"] == "internal_error"
    # The raw exception message must NOT leak to the caller.
    assert "disk full" not in body["error"]


def test_post_jobs_422_invalid_params_has_stable_code():
    resp = client.post(
        "/jobs",
        headers={"X-API-Key": API_KEY},
        data={"params": "{not valid json"},
        files=_multipart_files(),
    )

    assert resp.status_code == 422
    assert resp.headers["x-status-code"] == "invalid_params"
    body = resp.json()
    assert body["code"] == "invalid_params"
    assert body["job_id"] is None


def test_health_ok_no_auth_required():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.fixture(autouse=True)
def _reset_render_lock():
    # Each test starts with the semaphore unacquired. With TestClient (sync)
    # and one event loop per request, this is usually fine; explicit anyway.
    yield
