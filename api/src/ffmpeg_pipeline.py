"""FFmpeg pipeline: probe, concat (fast or re-encode), mix music.

Pure command-builders return argv lists; the runners (`probe`, `run`) are
the only functions that shell out — so the builders stay unit-testable
without spawning ffmpeg.

`run_pipeline()` is the synchronous entry point. It is intended to be
called from a worker thread (the API uses `asyncio.to_thread`). Known
failures raise `_FriendlyError` whose message is safe to surface to
Make/Airtable in Spanish.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from shared.models import JobParams

from .settings import get_settings

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 600


class _FriendlyError(RuntimeError):
    """Internal sentinel — message is safe to surface to Make/Airtable."""


class FfmpegError(RuntimeError):
    """Raised when ffmpeg/ffprobe fails. Message is user-readable."""


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str | None
    sample_rate: int | None
    duration: float

    @property
    def orientation(self) -> Literal["vertical", "horizontal"]:
        return "vertical" if self.height >= self.width else "horizontal"

    @property
    def has_audio(self) -> bool:
        return self.acodec is not None


def _parse_fps(rate: str) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def build_ffprobe_cmd(path: str) -> list[str]:
    return [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]


def probe(path: str) -> VideoInfo:
    cmd = build_ffprobe_cmd(path)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffprobe colgado leyendo {os.path.basename(path)}.") from exc
    except subprocess.CalledProcessError as exc:
        logger.error("ffprobe_failed", extra={"stderr": exc.stderr, "path": path})
        raise FfmpegError(
            f"ffprobe no pudo leer {os.path.basename(path)} — el archivo está "
            f"corrupto o el formato no es compatible."
        ) from exc

    data = json.loads(proc.stdout or "{}")
    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v_stream is None:
        raise FfmpegError(f"{os.path.basename(path)} no contiene stream de vídeo.")

    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or v_stream.get("duration") or 0.0)

    return VideoInfo(
        path=path,
        width=int(v_stream.get("width") or 0),
        height=int(v_stream.get("height") or 0),
        fps=_parse_fps(v_stream.get("r_frame_rate") or "0/0"),
        vcodec=v_stream.get("codec_name", "unknown"),
        acodec=(a_stream.get("codec_name") if a_stream else None),
        sample_rate=int(a_stream.get("sample_rate")) if a_stream and a_stream.get("sample_rate") else None,
        duration=duration,
    )


# ---------------------------------------------------------------------------
# Compatibility check for the fast-path
# ---------------------------------------------------------------------------


def can_concat_without_reencode(
    infos: list[VideoInfo],
    target_orientation: Literal["vertical", "horizontal"],
) -> bool:
    """Concat -c copy requires identical codec, w, h, fps, audio params and
    orientation matching the requested one."""
    if not infos:
        return False
    head = infos[0]
    if head.orientation != target_orientation:
        return False
    for info in infos[1:]:
        if (
            info.vcodec != head.vcodec
            or info.width != head.width
            or info.height != head.height
            or abs(info.fps - head.fps) > 0.01
            or info.acodec != head.acodec
            or info.sample_rate != head.sample_rate
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


_TARGET_DIMS = {
    "vertical": (1080, 1920),
    "horizontal": (1920, 1080),
}


def build_concat_list_file(workdir: str, clip_paths: list[str]) -> str:
    list_path = os.path.join(workdir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in clip_paths:
            escaped = p.replace("'", "'\\''")
            fh.write(f"file '{escaped}'\n")
    return list_path


def build_concat_copy_cmd(list_path: str, output_path: str) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]


def _per_clip_video_filter(
    target_orientation: Literal["vertical", "horizontal"],
    strategy: Literal["crop", "pad"],
) -> str:
    w, h = _TARGET_DIMS[target_orientation]
    if strategy == "crop":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30"
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30"
    )


def build_concat_reencode_cmd(
    infos: list[VideoInfo],
    output_path: str,
    target_orientation: Literal["vertical", "horizontal"],
    strategy: Literal["crop", "pad"],
) -> list[str]:
    """Normalised re-encode + concat. Per-clip silent lavfi audio is injected
    when a clip has no audio stream, so the `concat=` filter always sees 3
    video+audio pairs.
    """
    if len(infos) != 3:
        raise ValueError("expected exactly 3 clips for concat")

    cmd: list[str] = ["ffmpeg", "-y"]
    for info in infos:
        cmd += ["-i", info.path]

    silent_input_for: dict[int, int] = {}
    next_input_idx = len(infos)
    for i, info in enumerate(infos):
        if not info.has_audio:
            cmd += [
                "-f", "lavfi",
                "-t", f"{info.duration:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
            silent_input_for[i] = next_input_idx
            next_input_idx += 1

    vf = _per_clip_video_filter(target_orientation, strategy)

    parts: list[str] = []
    for i in range(3):
        parts.append(f"[{i}:v]{vf}[v{i}]")
    for i, info in enumerate(infos):
        audio_input = silent_input_for.get(i, i)
        parts.append(f"[{audio_input}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
    parts.append("[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]")
    filter_complex = ";".join(parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def build_music_mix_cmd(
    concat_path: str,
    music_path: str,
    output_path: str,
    duration: float,
    volume: float,
    fade_in: float,
    fade_out: float,
    concat_has_audio: bool,
) -> list[str]:
    """Mix concat with background music. Music is truncated by amix=duration=first.

    If the concat has no audio (all three sources silent), the music alone
    becomes the audio track of the output.
    """
    fade_out_start = max(0.0, duration - fade_out)
    if concat_has_audio:
        filter_complex = (
            f"[1:a]volume={volume},"
            f"afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_out}[m];"
            f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
    else:
        filter_complex = (
            f"[1:a]volume={volume},"
            f"afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_out},"
            f"atrim=0:{duration:.3f}[a]"
        )
    return [
        "ffmpeg",
        "-y",
        "-i", concat_path,
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        output_path,
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(cmd: list[str], *, friendly_action: str, timeout: int | None = None) -> None:
    t = timeout if timeout is not None else FFMPEG_TIMEOUT_SECONDS
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired as exc:
        logger.error("ffmpeg_timeout", extra={"action": friendly_action, "cmd": cmd[:3]})
        raise FfmpegError(
            f"FFmpeg colgado al {friendly_action} (timeout {t}s). "
            f"Probable problema con un clip muy largo o un archivo corrupto."
        ) from exc

    if proc.returncode != 0:
        logger.error(
            "ffmpeg_failed",
            extra={
                "action": friendly_action,
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-2000:],
            },
        )
        raise FfmpegError(
            f"FFmpeg falló al {friendly_action}. Revisa los logs para el detalle técnico."
        )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    duration_seconds: float
    concat_strategy: Literal["fast", "reencode"]


def run_pipeline(
    *,
    hook: Path,
    cuerpo: Path,
    cta: Path,
    music: Path,
    output: Path,
    params: JobParams,
) -> PipelineResult:
    """Probe → concat (fast or re-encode) → mix music → write output.

    Synchronous and CPU-bound. Call from a worker thread.
    Known failures raise `_FriendlyError` (Spanish, safe to surface).
    """
    import structlog

    log = structlog.get_logger("render-pipeline")
    settings = get_settings()
    workdir = output.parent

    # ---- 1. Probe clips in role order: hook → cuerpo → cta ----
    ordered_paths = [str(hook), str(cuerpo), str(cta)]
    try:
        infos = [probe(p) for p in ordered_paths]
    except FfmpegError as exc:
        raise _FriendlyError(str(exc)) from exc

    # ---- 2. Concat ----
    concat_path = workdir / "concat.mp4"
    orientation = params.orientation

    if can_concat_without_reencode(infos, orientation):
        log.info("concat_fast_path")
        list_path = build_concat_list_file(str(workdir), ordered_paths)
        cmd = build_concat_copy_cmd(list_path, str(concat_path))
        try:
            run(cmd, friendly_action="concatenar los clips")
        except FfmpegError as exc:
            raise _FriendlyError(str(exc)) from exc
        concat_strategy: Literal["fast", "reencode"] = "fast"
    else:
        log.info("concat_reencode_path", strategy=settings.orientation_strategy)
        cmd = build_concat_reencode_cmd(
            infos,
            output_path=str(concat_path),
            target_orientation=orientation,
            strategy=settings.orientation_strategy,
        )
        try:
            run(cmd, friendly_action="normalizar y concatenar los clips")
        except FfmpegError as exc:
            raise _FriendlyError(str(exc)) from exc
        concat_strategy = "reencode"

    # ---- 3. Probe concat for duration + audio presence ----
    try:
        concat_info = probe(str(concat_path))
    except FfmpegError as exc:
        raise _FriendlyError(
            f"No pude leer el vídeo concatenado para calcular su duración — {exc}"
        ) from exc
    duration = concat_info.duration
    if duration <= 0:
        raise _FriendlyError(
            "El vídeo concatenado tiene duración 0 — uno de los clips está vacío."
        )

    # ---- 4. Mix music ----
    mix_cmd = build_music_mix_cmd(
        concat_path=str(concat_path),
        music_path=str(music),
        output_path=str(output),
        duration=duration,
        volume=params.music_volume,
        fade_in=params.fade_in,
        fade_out=params.fade_out,
        concat_has_audio=concat_info.has_audio,
    )
    try:
        run(mix_cmd, friendly_action="mezclar la música con el vídeo")
    except FfmpegError as exc:
        raise _FriendlyError(str(exc)) from exc

    return PipelineResult(duration_seconds=round(duration, 2), concat_strategy=concat_strategy)
