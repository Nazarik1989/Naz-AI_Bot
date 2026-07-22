"""Local ffmpeg composition and mandatory technical QA for Story-first media."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class MediaError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration_seconds: float
    width: int
    height: int
    codec: str
    pixel_format: str
    frame_rate: str
    motion_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds, "width": self.width, "height": self.height,
            "codec": self.codec, "pixel_format": self.pixel_format, "frame_rate": self.frame_rate,
            "motion_score": self.motion_score,
        }


@dataclass(frozen=True, slots=True)
class LicensedTrack:
    track_id: str
    path: Path
    bpm: float
    beat_grid: tuple[float, ...]
    license: str
    source: str
    checksum: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), **kwargs)  # noqa: S603


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rate_value(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator or "1")
    except (ValueError, ZeroDivisionError) as exc:
        raise MediaError("media_frame_rate_invalid") from exc


class MediaComposer:
    def __init__(
        self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", font_path: Path | None = None,
        timeout_seconds: int = 120, runner: Runner | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.font_path = font_path
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.runner = runner or _default_runner

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                list(args), capture_output=True, text=True, timeout=self.timeout_seconds,
                check=False, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError("media_tool_unavailable_or_timed_out") from exc
        if result.returncode != 0:
            raise MediaError("media_tool_failed")
        return result

    @staticmethod
    def safe_output(root: Path, relative: str) -> Path:
        base = root.resolve()
        target = (base / relative).resolve()
        if base not in target.parents or target.suffix.casefold() != ".mp4":
            raise MediaError("unsafe_media_path")
        return target

    @staticmethod
    def _temporary(destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".mp4", dir=destination.parent)
        os.close(descriptor)
        return Path(raw)

    def _motion_score(self, path: Path) -> float:
        args = [
            self.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-an",
            "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-", "-f", "null", "-",
        ]
        result = self._run(args)
        values = [float(item) for item in re.findall(r"YDIF=([0-9.]+)", result.stdout)]
        return round(max(values, default=0.0), 4)

    def probe(self, path: Path, *, require_story_duration: bool = True, require_motion: bool = True) -> MediaProbe:
        if not path.is_file() or path.stat().st_size < 12:
            raise MediaError("media_missing_or_empty")
        result = self._run([
            self.ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,width,height,avg_frame_rate:format=duration",
            "-of", "json", str(path),
        ])
        try:
            payload = json.loads(result.stdout)
            stream = payload["streams"][0]
            duration = float(payload["format"]["duration"])
            width, height = int(stream["width"]), int(stream["height"])
            codec, pix_fmt = str(stream["codec_name"]), str(stream["pix_fmt"])
            frame_rate = str(stream["avg_frame_rate"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaError("media_probe_invalid") from exc
        if codec != "h264":
            raise MediaError("media_codec_invalid")
        if pix_fmt != "yuv420p":
            raise MediaError("media_pixel_format_invalid")
        if width * 16 != height * 9 or height < width:
            raise MediaError("media_resolution_invalid")
        if require_story_duration and not 3.95 <= duration <= 8.05:
            raise MediaError("media_duration_invalid")
        if not 23.0 <= _rate_value(frame_rate) <= 60.0:
            raise MediaError("media_frame_rate_invalid")
        motion_score = self._motion_score(path) if require_motion else 0.0
        if require_motion and motion_score < 0.25:
            raise MediaError("media_motion_not_detected")
        return MediaProbe(round(duration, 3), width, height, codec, pix_fmt, frame_rate, motion_score)

    def normalize(self, source: Path, destination: Path, *, duration_seconds: float) -> MediaProbe:
        if not 4 <= duration_seconds <= 8:
            raise MediaError("planned_duration_invalid")
        temporary = self._temporary(destination)
        try:
            self._run([
                self.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(source),
                "-t", str(duration_seconds), "-an", "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ])
            probe = self.probe(temporary)
            os.replace(temporary, destination)
            return probe
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _ffmpeg_filter_path(path: Path) -> str:
        return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")

    def overlay_story(self, clean: Path, destination: Path, *, text: str, safe_zone: str) -> MediaProbe:
        if not text.strip() or re.search(r"(?i)(api[_ -]?key|password|bearer|secret|личное сообщение)", text):
            raise MediaError("overlay_text_unsafe")
        if self.font_path is None or not self.font_path.is_file():
            raise MediaError("cyrillic_font_missing")
        wrapped = "\n".join(textwrap.wrap(" ".join(text.split()), width=25, max_lines=4, placeholder="…"))
        text_fd, raw_text = tempfile.mkstemp(prefix=".naz-overlay-", suffix=".txt", dir=destination.parent)
        os.close(text_fd)
        text_file = Path(raw_text)
        text_file.write_text(wrapped, encoding="utf-8")
        temporary = self._temporary(destination)
        y = {"upper-middle": "h*0.24", "middle-left": "h*0.43", "lower-middle above platform controls": "h*0.66"}.get(safe_zone, "h*0.43")
        drawtext = (
            f"drawtext=fontfile='{self._ffmpeg_filter_path(self.font_path)}':"
            f"textfile='{self._ffmpeg_filter_path(text_file)}':fontcolor=white:fontsize=54:"
            f"line_spacing=14:box=1:boxcolor=black@0.55:boxborderw=24:x=(w-text_w)/2:y={y}"
        )
        try:
            self._run([
                self.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(clean), "-an", "-vf", drawtext,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ])
            probe = self.probe(temporary)
            os.replace(temporary, destination)
            return probe
        finally:
            temporary.unlink(missing_ok=True)
            text_file.unlink(missing_ok=True)

    def compose_reel(
        self, *, pack_root: Path, shots: Sequence[Mapping[str, Any]], destination: Path,
        track: LicensedTrack,
    ) -> MediaProbe:
        if not shots or not track.path.is_file() or checksum(track.path) != track.checksum:
            raise MediaError("licensed_music_invalid")
        if not track.license.strip() or not track.source.strip() or track.bpm <= 0 or len(track.beat_grid) < 2:
            raise MediaError("licensed_music_metadata_invalid")
        inputs: list[str] = []
        filters: list[str] = []
        total = 0.0
        for index, shot in enumerate(shots):
            duration = float(shot["duration_seconds"])
            start = float(shot.get("in_seconds", 0))
            if not 0.4 <= duration <= 2.0:
                raise MediaError("reel_fragment_duration_invalid")
            source = self.safe_output(pack_root, str(shot["source"]))
            probe = self.probe(source)
            if start < 0 or start + duration > probe.duration_seconds + 0.02:
                raise MediaError("reel_fragment_out_of_source")
            inputs.extend(["-i", str(source)])
            crop = str(shot.get("reel_crop", ""))
            zoom = {"tight-center": 1.18, "left-detail": 1.12, "right-detail": 1.12, "wide-center": 1.04}.get(crop)
            if zoom is None:
                raise MediaError("reel_crop_missing")
            x = "0" if crop == "left-detail" else "iw-1080" if crop == "right-detail" else "(iw-1080)/2"
            filters.append(
                f"[{index}:v]trim=start={start}:duration={duration},setpts=PTS-STARTPTS,"
                f"scale=ceil(iw*{zoom}/2)*2:ceil(ih*{zoom}/2)*2,crop=1080:1920:{x}:(ih-1920)/2,fps=30[v{index}]"
            )
            total += duration
        beat_tolerance = 0.06
        boundaries, elapsed = [], 0.0
        for shot in shots[:-1]:
            elapsed += float(shot["duration_seconds"])
            boundaries.append(elapsed)
        if any(min(abs(cut - beat) for beat in track.beat_grid) > beat_tolerance for cut in boundaries):
            raise MediaError("reel_cuts_not_on_beat_grid")
        filters.append("".join(f"[v{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[outv]")
        temporary = self._temporary(destination)
        try:
            self._run([
                self.ffmpeg, "-nostdin", "-y", "-v", "error", *inputs, "-stream_loop", "-1", "-i", str(track.path),
                "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", f"{len(shots)}:a:0",
                "-t", str(round(total, 3)), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-movflags", "+faststart", "-shortest", str(temporary),
            ])
            probe = self.probe(temporary, require_story_duration=False)
            os.replace(temporary, destination)
            return probe
        finally:
            temporary.unlink(missing_ok=True)


def load_music_library(path: Path, *, pack_root: Path | None = None) -> list[LicensedTrack]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("tracks", []) if isinstance(raw, dict) else []
    result: list[LicensedTrack] = []
    for row in rows:
        try:
            track_path = Path(str(row["path"])).expanduser().resolve()
            if pack_root is not None and pack_root.resolve() in track_path.parents:
                continue
            item = LicensedTrack(
                track_id=str(row["track_id"]), path=track_path, bpm=float(row["bpm"]),
                beat_grid=tuple(float(value) for value in row["beat_grid"]),
                license=str(row["license"]), source=str(row["source"]), checksum=str(row["checksum"]),
            )
            if item.path.is_file() and checksum(item.path) == item.checksum and item.license and item.source:
                result.append(item)
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return result
