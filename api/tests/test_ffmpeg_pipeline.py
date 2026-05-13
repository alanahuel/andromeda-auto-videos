from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import patch

import pytest

from src import ffmpeg_pipeline
from src.ffmpeg_pipeline import (
    FfmpegError,
    VideoInfo,
    build_concat_copy_cmd,
    build_concat_list_file,
    build_concat_reencode_cmd,
    build_ffprobe_cmd,
    build_music_mix_cmd,
    can_concat_without_reencode,
    probe,
)


def _info(
    *,
    path: str = "/tmp/x.mp4",
    w: int = 1080,
    h: int = 1920,
    fps: float = 30.0,
    vcodec: str = "h264",
    acodec: str | None = "aac",
    sr: int | None = 48000,
    duration: float = 12.0,
) -> VideoInfo:
    return VideoInfo(
        path=path,
        width=w,
        height=h,
        fps=fps,
        vcodec=vcodec,
        acodec=acodec,
        sample_rate=sr,
        duration=duration,
    )


# ---------------------------------------------------------------------------
# can_concat_without_reencode
# ---------------------------------------------------------------------------


def test_concat_fastpath_when_all_match_and_orientation_matches():
    infos = [_info(), _info(), _info()]
    assert can_concat_without_reencode(infos, "vertical") is True


def test_concat_fastpath_rejected_when_orientation_differs():
    infos = [_info(w=1080, h=1920), _info(w=1080, h=1920), _info(w=1080, h=1920)]
    assert can_concat_without_reencode(infos, "horizontal") is False


def test_concat_fastpath_rejected_when_codecs_differ():
    infos = [_info(vcodec="h264"), _info(vcodec="hevc"), _info(vcodec="h264")]
    assert can_concat_without_reencode(infos, "vertical") is False


def test_concat_fastpath_rejected_when_fps_differs():
    infos = [_info(fps=30.0), _info(fps=29.97), _info(fps=30.0)]
    assert can_concat_without_reencode(infos, "vertical") is False


def test_concat_fastpath_rejected_when_one_clip_missing_audio():
    infos = [_info(acodec="aac"), _info(acodec=None, sr=None), _info(acodec="aac")]
    assert can_concat_without_reencode(infos, "vertical") is False


# ---------------------------------------------------------------------------
# build_ffprobe_cmd / probe
# ---------------------------------------------------------------------------


def test_build_ffprobe_cmd_shape():
    cmd = build_ffprobe_cmd("/tmp/foo.mp4")
    assert cmd[0] == "ffprobe"
    assert "-print_format" in cmd and "json" in cmd
    assert cmd[-1] == "/tmp/foo.mp4"


def test_probe_parses_streams_and_format():
    fake = {
        "format": {"duration": "12.5"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "r_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
            },
        ],
    }
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(fake), stderr="")
    with patch("src.ffmpeg_pipeline.subprocess.run", return_value=completed):
        info = probe("/tmp/x.mp4")
    assert info.width == 1080 and info.height == 1920
    assert info.vcodec == "h264" and info.acodec == "aac"
    assert info.fps == pytest.approx(30.0)
    assert info.duration == pytest.approx(12.5)
    assert info.has_audio is True
    assert info.orientation == "vertical"


def test_probe_no_audio_stream():
    fake = {
        "format": {"duration": "8"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
            }
        ],
    }
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(fake), stderr="")
    with patch("src.ffmpeg_pipeline.subprocess.run", return_value=completed):
        info = probe("/tmp/silent.mp4")
    assert info.has_audio is False
    assert info.acodec is None
    assert info.orientation == "horizontal"
    assert info.fps == pytest.approx(30000 / 1001)


def test_probe_raises_friendly_error_when_ffprobe_fails():
    err = subprocess.CalledProcessError(returncode=1, cmd=["ffprobe"], stderr="broken")
    with patch("src.ffmpeg_pipeline.subprocess.run", side_effect=err):
        with pytest.raises(FfmpegError) as exc:
            probe("/tmp/x.mp4")
    assert "corrupto" in str(exc.value) or "no es compatible" in str(exc.value)


# ---------------------------------------------------------------------------
# concat list file
# ---------------------------------------------------------------------------


def test_build_concat_list_file_escapes_single_quotes(tmp_path):
    path_with_quote = str(tmp_path / "weird'name.mp4")
    list_path = build_concat_list_file(str(tmp_path), [path_with_quote, str(tmp_path / "b.mp4")])
    assert os.path.basename(list_path) == "concat_list.txt"
    body = open(list_path).read()
    assert "weird'\\''name.mp4" in body
    assert body.count("file '") == 2


# ---------------------------------------------------------------------------
# build_concat_copy_cmd
# ---------------------------------------------------------------------------


def test_build_concat_copy_cmd_uses_concat_demuxer_and_copy():
    cmd = build_concat_copy_cmd("/tmp/list.txt", "/tmp/out.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "concat"
    assert "-safe" in cmd and cmd[cmd.index("-safe") + 1] == "0"
    assert "-c" in cmd and "copy" in cmd
    assert cmd[-1] == "/tmp/out.mp4"
    assert "+faststart" in cmd


# ---------------------------------------------------------------------------
# build_concat_reencode_cmd
# ---------------------------------------------------------------------------


def test_concat_reencode_vertical_crop_filter_present():
    infos = [_info() for _ in range(3)]
    cmd = build_concat_reencode_cmd(infos, "/tmp/out.mp4", "vertical", "crop")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in fc
    assert "crop=1080:1920" in fc
    assert fc.count("concat=n=3:v=1:a=1") == 1


def test_concat_reencode_horizontal_pad_filter_present():
    infos = [_info(w=1080, h=1920) for _ in range(3)]
    cmd = build_concat_reencode_cmd(infos, "/tmp/out.mp4", "horizontal", "pad")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in fc
    assert "pad=1920:1080" in fc


def test_concat_reencode_libx264_aac_profile():
    infos = [_info() for _ in range(3)]
    cmd = build_concat_reencode_cmd(infos, "/tmp/out.mp4", "vertical", "crop")
    assert "-c:v" in cmd and "libx264" in cmd
    assert "-c:a" in cmd and "aac" in cmd
    assert "-crf" in cmd
    assert "-profile:v" in cmd and "high" in cmd


def test_concat_reencode_adds_silent_lavfi_for_clip_without_audio():
    infos = [_info(acodec=None, sr=None, duration=5.0), _info(), _info()]
    cmd = build_concat_reencode_cmd(infos, "/tmp/out.mp4", "vertical", "crop")
    lavfi_pos = cmd.index("-f")
    assert cmd[lavfi_pos + 1] == "lavfi"
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[3:a]aresample=48000" in fc


def test_concat_reencode_requires_exactly_three_clips():
    with pytest.raises(ValueError):
        build_concat_reencode_cmd([_info(), _info()], "/tmp/out.mp4", "vertical", "crop")


# ---------------------------------------------------------------------------
# build_music_mix_cmd
# ---------------------------------------------------------------------------


def test_music_mix_cmd_uses_amix_and_correct_fade_timing():
    cmd = build_music_mix_cmd(
        concat_path="/tmp/concat.mp4",
        music_path="/tmp/music.mp3",
        output_path="/tmp/out.mp4",
        duration=90.0,
        volume=0.3,
        fade_in=2.0,
        fade_out=2.0,
        concat_has_audio=True,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0.3" in fc
    assert "afade=t=in:st=0:d=2.0" in fc
    assert "afade=t=out:st=88.000:d=2.0" in fc
    assert "amix=inputs=2:duration=first" in fc
    assert "-c:v" in cmd and "copy" in cmd[cmd.index("-c:v") + 1 : cmd.index("-c:v") + 2]


def test_music_mix_cmd_when_concat_has_no_audio_maps_only_music():
    cmd = build_music_mix_cmd(
        concat_path="/tmp/concat.mp4",
        music_path="/tmp/music.mp3",
        output_path="/tmp/out.mp4",
        duration=10.0,
        volume=0.3,
        fade_in=1.0,
        fade_out=1.0,
        concat_has_audio=False,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=" not in fc
    assert "atrim=0:10.000" in fc


def test_music_mix_fade_out_start_clamped_to_zero_for_short_video():
    cmd = build_music_mix_cmd(
        concat_path="/tmp/concat.mp4",
        music_path="/tmp/music.mp3",
        output_path="/tmp/out.mp4",
        duration=1.0,
        volume=0.5,
        fade_in=2.0,
        fade_out=5.0,
        concat_has_audio=True,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "afade=t=out:st=0.000:d=5.0" in fc


# ---------------------------------------------------------------------------
# run() — timeout and non-zero exit handling
# ---------------------------------------------------------------------------


def test_run_raises_friendly_error_on_non_zero_exit():
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad input")
    with patch("src.ffmpeg_pipeline.subprocess.run", return_value=completed):
        with pytest.raises(FfmpegError) as exc:
            ffmpeg_pipeline.run(["ffmpeg", "-i", "x"], friendly_action="concatenar los clips")
    assert "concatenar los clips" in str(exc.value)


def test_run_raises_friendly_error_on_timeout():
    err = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=1)
    with patch("src.ffmpeg_pipeline.subprocess.run", side_effect=err):
        with pytest.raises(FfmpegError) as exc:
            ffmpeg_pipeline.run(["ffmpeg", "-i", "x"], friendly_action="concatenar", timeout=1)
    assert "colgado" in str(exc.value)
