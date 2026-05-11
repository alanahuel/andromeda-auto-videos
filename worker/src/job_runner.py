"""End-to-end orchestrator for one render job. RQ invokes `run_job`.

Order is strict — see DEPLOY.md / spec:
1. workdir
2. download clips + music
3. probe clips
4. concat (fast or reencoded)
5. mix music
6. upload to Drive
7. callback (success)
On any step's failure, callback(failed) and re-raise so RQ marks the job
failed. The workdir is removed in `finally` either way.
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any

import structlog
from pydantic import ValidationError

from shared.models import CallbackPayload, JobRequest

from . import ffmpeg_runner
from .callback import send_callback
from .drive_client import DriveError, download_file, upload_file
from .ffmpeg_runner import FfmpegError
from .logging_setup import configure_logging
from .settings import get_settings

configure_logging()
log = structlog.get_logger("render-worker")


def run_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Entry point invoked by RQ.

    Returns a small result dict on success (stored by RQ in finished registry);
    raises on failure so RQ marks the job failed and exc_info is captured.
    """
    configure_logging()
    bind_log = structlog.contextvars.bind_contextvars
    structlog.contextvars.clear_contextvars()
    bind_log(job_id=job_id)
    log.info("job_started")

    settings = get_settings()
    workdir = os.path.join(settings.workdir_base, job_id)
    callback_url: str | None = None
    metadata: dict[str, Any] = {}
    started = time.monotonic()

    try:
        # ---- Validate payload (defence in depth — API already validated) ----
        try:
            req = JobRequest.model_validate(payload)
        except ValidationError as exc:
            raise _FriendlyError(
                "Payload del job inválido. Avisa al equipo técnico — esto no "
                "debería pasar si la API validó correctamente."
            ) from exc

        callback_url = str(req.callback_url)
        metadata = dict(req.metadata)

        # ---- 1. Workdir ----
        try:
            os.makedirs(workdir, exist_ok=False)
        except OSError as exc:
            raise _FriendlyError(
                f"No pude crear el directorio de trabajo {workdir}. ¿Disco lleno?"
            ) from exc

        # ---- 2. Download clips + music ----
        log.info("downloading_clips")
        clips_by_role: dict[str, str] = {}
        for clip in req.clips:
            try:
                res = download_file(
                    drive_id=clip.drive_id,
                    dest_dir=workdir,
                    base_name=clip.role,
                    fallback_ext=".mp4",
                )
            except DriveError as exc:
                raise _FriendlyError(
                    f"No pude descargar el clip {clip.role.capitalize()} desde Drive — {exc}"
                ) from exc
            clips_by_role[clip.role] = res.local_path

        try:
            music_res = download_file(
                drive_id=req.music.drive_id,
                dest_dir=workdir,
                base_name="music",
                fallback_ext=".mp3",
            )
        except DriveError as exc:
            raise _FriendlyError(f"No pude descargar la música desde Drive — {exc}") from exc
        music_path = music_res.local_path

        # ---- 3. Probe clips in role order: hook → cuerpo → cta ----
        ordered_paths = [clips_by_role["hook"], clips_by_role["cuerpo"], clips_by_role["cta"]]
        try:
            infos = [ffmpeg_runner.probe(p) for p in ordered_paths]
        except FfmpegError as exc:
            raise _FriendlyError(str(exc)) from exc

        # ---- 4. Concat ----
        concat_path = os.path.join(workdir, "concat.mp4")
        orientation = req.output.orientation

        if ffmpeg_runner.can_concat_without_reencode(infos, orientation):
            log.info("concat_fast_path")
            list_path = ffmpeg_runner.build_concat_list_file(workdir, ordered_paths)
            cmd = ffmpeg_runner.build_concat_copy_cmd(list_path, concat_path)
            try:
                ffmpeg_runner.run(cmd, friendly_action="concatenar los clips")
            except FfmpegError as exc:
                raise _FriendlyError(str(exc)) from exc
        else:
            log.info("concat_reencode_path", strategy=settings.orientation_strategy)
            cmd = ffmpeg_runner.build_concat_reencode_cmd(
                infos,
                output_path=concat_path,
                target_orientation=orientation,
                strategy=settings.orientation_strategy,
            )
            try:
                ffmpeg_runner.run(cmd, friendly_action="normalizar y concatenar los clips")
            except FfmpegError as exc:
                raise _FriendlyError(str(exc)) from exc

        # ---- 5. Mix music ----
        try:
            concat_info = ffmpeg_runner.probe(concat_path)
        except FfmpegError as exc:
            raise _FriendlyError(
                f"No pude leer el vídeo concatenado para calcular su duración — {exc}"
            ) from exc
        duration = concat_info.duration
        if duration <= 0:
            raise _FriendlyError(
                "El vídeo concatenado tiene duración 0 — uno de los clips está vacío."
            )

        output_path = os.path.join(workdir, "output.mp4")
        cmd = ffmpeg_runner.build_music_mix_cmd(
            concat_path=concat_path,
            music_path=music_path,
            output_path=output_path,
            duration=duration,
            volume=req.music.volume,
            fade_in=req.music.fade_in,
            fade_out=req.music.fade_out,
            concat_has_audio=concat_info.has_audio,
        )
        try:
            ffmpeg_runner.run(cmd, friendly_action="mezclar la música con el vídeo")
        except FfmpegError as exc:
            raise _FriendlyError(str(exc)) from exc

        # ---- 6. Upload to Drive ----
        target_name = f"{req.output.name}_{req.output.orientation}.mp4"
        log.info("uploading_output", target_name=target_name)
        try:
            uploaded = upload_file(
                local_path=output_path,
                folder_drive_id=req.output.folder_drive_id,
                target_name=target_name,
            )
        except DriveError as exc:
            raise _FriendlyError(f"No pude subir el output a Drive — {exc}") from exc

        elapsed = time.monotonic() - started
        log.info("job_done", duration_seconds=round(elapsed, 2), output_drive_id=uploaded.drive_id)

        # ---- 7. Callback success ----
        if callback_url:
            send_callback(
                callback_url,
                CallbackPayload(
                    job_id=job_id,
                    status="done",
                    output_drive_id=uploaded.drive_id,
                    output_url=uploaded.web_view_link,
                    duration_seconds=round(elapsed, 2),
                    error=None,
                    metadata=metadata,
                ),
            )
        return {
            "output_drive_id": uploaded.drive_id,
            "output_url": uploaded.web_view_link,
            "duration_seconds": round(elapsed, 2),
        }

    except _FriendlyError as exc:
        log.error("job_failed", error=str(exc))
        _safe_failure_callback(callback_url, job_id, str(exc), metadata)
        raise
    except Exception as exc:
        # Unexpected — surface a generic message but log the type for debugging.
        log.exception("job_failed_unexpected", error_type=type(exc).__name__)
        message = "Error inesperado en el render — revisa los logs del worker."
        _safe_failure_callback(callback_url, job_id, message, metadata)
        raise
    finally:
        _cleanup(workdir)
        structlog.contextvars.clear_contextvars()


class _FriendlyError(RuntimeError):
    """Internal sentinel — the message is safe to surface to Make/Airtable."""


def _safe_failure_callback(
    url: str | None,
    job_id: str,
    message: str,
    metadata: dict[str, Any],
) -> None:
    if not url:
        return
    try:
        send_callback(
            url,
            CallbackPayload(
                job_id=job_id,
                status="failed",
                output_drive_id=None,
                output_url=None,
                duration_seconds=None,
                error=message,
                metadata=metadata,
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception("failure_callback_crashed")


def _cleanup(workdir: str) -> None:
    if not os.path.isdir(workdir):
        return
    try:
        shutil.rmtree(workdir, ignore_errors=True)
        log.debug("workdir_cleaned", workdir=workdir)
    except Exception:  # noqa: BLE001
        # Never raise from cleanup — disk-full is the worst that can happen and
        # it's already surfaced via monitoring, not via the job result.
        log.exception("workdir_cleanup_failed", workdir=workdir)
