from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import UploadFile

from shared.models import JobParams
from src import job_registry, render_orchestrator
from src.ffmpeg_pipeline import PipelineResult, _FriendlyError


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _params(output_name="ad_test") -> JobParams:
    return JobParams(orientation="vertical", output_name=output_name)


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def test_enqueue_persists_uploads_and_registers_queued(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / prefix))
    Path(str(tmp_path / "render_")).mkdir(parents=True, exist_ok=True)

    job_id = asyncio.run(
        render_orchestrator.enqueue_job(
            clips=[("hook", _upload("hook.mp4", b"hook-bytes"))],
            music=_upload("music.mp3", b"music-bytes"),
            params=_params(),
            retention_seconds=1800,
            max_pending=20,
        )
    )

    rec = job_registry.get(job_id)
    assert rec is not None
    assert rec.status == "queued"
    assert rec.clip_paths[0].read_bytes() == b"hook-bytes"
    assert rec.music_path.read_bytes() == b"music-bytes"
    assert rec.output_path.name == "ad_test.mp4"


def test_enqueue_raises_too_busy_when_queue_full(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path / prefix))
    Path(str(tmp_path / "render_")).mkdir(parents=True, exist_ok=True)
    # Fill the registry with 2 queued jobs, bound = 2.
    for jid in ("a", "b"):
        wd = tmp_path / jid
        job_registry.create(
            jid, output_name="x", workdir=wd, output_path=wd / "x.mp4",
            clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
        )

    with pytest.raises(render_orchestrator.RenderError) as ei:
        asyncio.run(
            render_orchestrator.enqueue_job(
                clips=[("hook", _upload("hook.mp4", b"x"))],
                music=None, params=_params(), retention_seconds=1800, max_pending=2,
            )
        )
    assert ei.value.code == "too_busy"
    assert ei.value.http_status == 429
    assert ei.value.job_id is None


def _fake_pipeline(*, output: Path, **_kw) -> PipelineResult:
    output.write_bytes(b"MP4")
    return PipelineResult(duration_seconds=42.5, concat_strategy="fast")


def test_execute_marks_done_and_writes_output(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[wd / "hook.mp4"], music_path=None, params=_params(),
        retention_seconds=1800,
    )
    with patch("src.render_orchestrator.run_pipeline", side_effect=_fake_pipeline):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "done"
    assert rec.duration_seconds == 42.5
    assert rec.concat_strategy == "fast"
    assert rec.output_path.read_bytes() == b"MP4"


def test_execute_marks_failed_with_friendly_code(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
    )

    def _boom(**_kw):
        raise _FriendlyError("clip corrupto", code="clip_unreadable")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "clip_unreadable"
    assert "clip corrupto" in rec.error


def test_execute_hides_unexpected_error_detail(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    job_registry.create(
        "j1", output_name="ad", workdir=wd, output_path=wd / "ad.mp4",
        clip_paths=[], music_path=None, params=_params(), retention_seconds=1800,
    )

    def _boom(**_kw):
        raise RuntimeError("disk full, internal detail")

    with patch("src.render_orchestrator.run_pipeline", side_effect=_boom):
        asyncio.run(render_orchestrator.execute_job("j1"))

    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "internal_error"
    assert "disk full" not in rec.error


def test_reap_once_deletes_expired_workdirs(tmp_path):
    wd = tmp_path / "render_old"
    wd.mkdir()
    job_registry.create(
        "old", output_name="x", workdir=wd, output_path=wd / "x.mp4",
        clip_paths=[], music_path=None, params=_params(),
        retention_seconds=100.0, now=0.0,
    )
    count = render_orchestrator.reap_once(now=200.0)
    assert count == 1
    assert not wd.exists()
    assert job_registry.get("old") is None
