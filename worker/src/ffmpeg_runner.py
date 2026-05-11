"""FFmpeg / ffprobe wrappers.

Pure command-builders return argv lists; runners execute them with
subprocess.run. Errors raise FfmpegError with a user-readable message;
the full stderr is logged separately. This shape keeps the builders
unit-testable without spawning ffmpeg.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Literal

from .settings import get_settings

logger = logging.getLogger(__name__)


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
    acodec: str | None  # None if no audio stream
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


def probe_duration(path: str) -> float:
    """Light-weight duration probe used after concat."""
    info = probe(path)
    return info.duration


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
    """Write the concat demuxer's list file and return its path."""
    list_path = os.path.join(workdir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in clip_paths:
            # Escape single quotes per concat-demuxer rules: ' → '\''
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
    """Per-clip video filter that takes any source aspect and produces the target frame."""
    w, h = _TARGET_DIMS[target_orientation]
    if strategy == "crop":
        # Scale so the source fills the target, then crop the overflow centered.
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30"
    # pad / letterbox
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
    """Build the normalised re-encode + concat command.

    For each clip that has no audio stream we add an extra `-f lavfi -t DUR -i anullsrc`
    input and consume its audio in the filter graph, so the concat filter always
    sees 3 audio streams.
    """
    if len(infos) != 3:
        raise ValueError("expected exactly 3 clips for concat")

    cmd: list[str] = ["ffmpeg", "-y"]
    # Real inputs first — their indices match infos[i].
    for info in infos:
        cmd += ["-i", info.path]
    # For each silent clip, append a lavfi input of matching duration.
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
    """Mix the concat with background music. Music is truncated by amix=duration=first.

    If the concat has no audio (extremely rare — all three sources silent), we
    map only the music as the audio track of the output.
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
    """Run an ffmpeg command. On non-zero exit or timeout, raise FfmpegError.

    `friendly_action` is interpolated into the user-facing error message
    (e.g. "concatenar los clips"). The full stderr is logged but never
    returned to the caller.
    """
    settings = get_settings()
    t = timeout if timeout is not None else settings.ffmpeg_timeout_seconds
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run with timeout has already sent SIGKILL by the time
        # TimeoutExpired is raised.
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
            f"FFmpeg falló al {friendly_action}. Revisa los logs del worker para "
            f"el detalle técnico."
        )
