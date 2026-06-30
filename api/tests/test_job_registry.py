from __future__ import annotations

from pathlib import Path

import pytest

from shared.models import JobParams
from src import job_registry


def _params() -> JobParams:
    return JobParams(orientation="vertical", output_name="ad_test")


def _create(job_id="j1", *, retention=1800.0, now=0.0) -> None:
    wd = Path(f"/tmp/render_{job_id}")
    job_registry.create(
        job_id,
        output_name="ad_test",
        workdir=wd,
        output_path=wd / "ad_test.mp4",
        clip_paths=[wd / "hook.mp4"],
        music_path=None,
        params=_params(),
        retention_seconds=retention,
        now=now,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    job_registry.reset()
    yield
    job_registry.reset()


def test_create_registers_a_queued_job():
    _create()
    rec = job_registry.get("j1")
    assert rec is not None
    assert rec.status == "queued"
    assert rec.code == "ok"
    assert rec.expires_at == 1800.0  # now(0) + retention(1800)


def test_get_unknown_returns_none():
    assert job_registry.get("nope") is None


def test_status_transitions():
    _create()
    job_registry.mark_processing("j1")
    assert job_registry.get("j1").status == "processing"
    job_registry.mark_done("j1", duration_seconds=42.5, concat_strategy="fast")
    rec = job_registry.get("j1")
    assert rec.status == "done"
    assert rec.code == "ok"
    assert rec.duration_seconds == 42.5
    assert rec.concat_strategy == "fast"


def test_mark_failed_records_code_and_message():
    _create()
    job_registry.mark_failed("j1", code="render_failed", error="petó")
    rec = job_registry.get("j1")
    assert rec.status == "failed"
    assert rec.code == "render_failed"
    assert rec.error == "petó"


def test_pending_count_counts_queued_and_processing_only():
    _create("a")
    _create("b")
    _create("c")
    job_registry.mark_processing("b")
    job_registry.mark_done("c", duration_seconds=1.0, concat_strategy="fast")
    assert job_registry.pending_count() == 2  # a (queued) + b (processing)


def test_sweep_expired_drops_and_returns_expired_workdirs():
    _create("old", retention=100.0, now=0.0)   # expires_at = 100
    _create("new", retention=100.0, now=0.0)
    job_registry.mark_processing("new")
    job_registry.mark_done("new", duration_seconds=1.0, concat_strategy="fast")
    # bump 'new' far into the future so only 'old' expires
    job_registry.get("new").expires_at = 10_000.0

    dropped = job_registry.sweep_expired(now=150.0)

    assert [p.name for p in dropped] == ["render_old"]
    assert job_registry.get("old") is None
    assert job_registry.get("new") is not None


def test_mark_downloaded_shortens_ttl_to_grace_window():
    _create(retention=1800.0, now=0.0)         # expires_at = 1800
    job_registry.mark_downloaded("j1", grace_seconds=60.0, now=100.0)
    assert job_registry.get("j1").expires_at == 160.0  # now(100) + grace(60)


def test_mark_downloaded_never_extends_ttl():
    _create(retention=50.0, now=0.0)           # expires_at = 50
    job_registry.mark_downloaded("j1", grace_seconds=600.0, now=0.0)
    assert job_registry.get("j1").expires_at == 50.0   # min(50, 600) keeps 50
