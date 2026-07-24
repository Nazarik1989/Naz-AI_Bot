"""Fail-closed analysis of generated audio from the decoded artifact itself."""

from __future__ import annotations

import array
import json
import math
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class AudioAnalysisError(RuntimeError):
    """An operator-safe audio-analysis failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AudioAnalysis:
    duration_seconds: float
    bpm: float
    beat_grid: tuple[float, ...]
    beat_evidence: tuple[bool, ...] = ()
    source: str = "actual-audio-derived-beat-track-v1"
    analyzer: str = "ffmpeg-onset-autocorrelation-v1"
    confidence: float = 1.0
    peak_prominence: float = 1.0
    onset_alignment_fraction: float = 1.0
    grid_onset_coverage: float = 1.0


Runner = Callable[[Sequence[str], float, int], bytes]


def _subprocess_runner(command: Sequence[str], timeout_seconds: float, output_limit: int) -> bytes:
    try:
        completed = subprocess.run(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise AudioAnalysisError("audio_analysis_tool_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioAnalysisError("audio_analysis_timeout") from exc
    except OSError as exc:
        raise AudioAnalysisError("audio_analysis_process_failed") from exc
    if completed.returncode != 0:
        raise AudioAnalysisError("audio_analysis_process_failed")
    if len(completed.stdout) > output_limit:
        raise AudioAnalysisError("audio_analysis_output_too_large")
    return completed.stdout


class FfmpegAudioAnalyzer:
    """Derive duration and a phase-aligned beat track from decoded audio.

    The tempo/phase calculation uses only the decoded waveform's onset-energy
    envelope.  Requested duration, requested BPM and generation prompt are not
    inputs, so the resulting grid cannot silently become a declared t=0 grid.
    """

    sample_rate = 8_000
    hop_samples = 80  # 10 ms; keeps tempo/grid quantization useful for edits

    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner or _subprocess_runner

    def preflight(self) -> None:
        """Verify both local tools before any paid provider submission."""

        for executable in ("ffprobe", "ffmpeg"):
            self._runner((executable, "-version"), 10.0, 128_000)

    def analyze(self, path: Path) -> AudioAnalysis:
        source = path.expanduser().resolve()
        if not source.is_file():
            raise AudioAnalysisError("audio_analysis_input_missing")
        duration = self._probe(source)
        pcm = self._runner(
            (
                "ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a:0",
                "-vn", "-ac", "1", "-ar", str(self.sample_rate),
                "-t", f"{duration + 1.0:.3f}", "-f", "s16le", "-",
            ),
            45.0,
            7_000_000,
        )
        decoded_duration = len(pcm) / float(self.sample_rate * 2)
        if abs(decoded_duration - duration) > 0.5:
            raise AudioAnalysisError("audio_duration_analysis_failed")
        (
            bpm, grid, beat_evidence, confidence, prominence,
            onset_fraction, grid_coverage,
        ) = self._beat_track(pcm, decoded_duration)
        return AudioAnalysis(
            duration_seconds=round(decoded_duration, 6), bpm=round(bpm, 3),
            beat_grid=grid, beat_evidence=beat_evidence,
            confidence=round(confidence, 6), peak_prominence=round(prominence, 6),
            onset_alignment_fraction=round(onset_fraction, 6),
            grid_onset_coverage=round(grid_coverage, 6),
        )

    def _probe(self, path: Path) -> float:
        raw = self._runner(
            (
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,duration:format=duration",
                "-of", "json", str(path),
            ),
            15.0,
            64_000,
        )
        try:
            value = json.loads(raw.decode("utf-8"))
            stream = value["streams"][0]
            duration = float(value["format"]["duration"])
            codec = str(stream["codec_name"]).strip().casefold()
            sample_rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
        except (
            IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError,
        ) as exc:
            raise AudioAnalysisError("audio_stream_contract_invalid") from exc
        if not math.isfinite(duration) or not 1.0 <= duration <= 380.0:
            raise AudioAnalysisError("audio_duration_analysis_failed")
        expected_mp3 = path.suffix.casefold() == ".mp3" and codec == "mp3"
        expected_wav = path.suffix.casefold() == ".wav" and codec in {
            "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le",
        }
        if not (expected_mp3 or expected_wav) or sample_rate != 44_100 or channels != 2:
            raise AudioAnalysisError("audio_stream_contract_invalid")
        return duration

    def _beat_track(
        self, raw: bytes, duration: float,
    ) -> tuple[float, tuple[float, ...], tuple[bool, ...], float, float, float, float]:
        if len(raw) < self.sample_rate * 2 or len(raw) % 2:
            raise AudioAnalysisError("audio_beat_analysis_failed")
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()

        frame_samples = self.hop_samples * 2
        energies: list[float] = []
        for start in range(0, len(samples) - frame_samples + 1, self.hop_samples):
            frame = samples[start:start + frame_samples]
            energies.append(sum(abs(value) for value in frame) / float(frame_samples))
        if len(energies) < 100 or max(energies, default=0.0) <= 1.0:
            raise AudioAnalysisError("audio_beat_analysis_failed")

        novelty: list[float] = []
        for index, energy in enumerate(energies):
            history = energies[max(0, index - 8):index]
            baseline = sum(history) / len(history) if history else energy
            novelty.append(max(0.0, energy - baseline))
        positive = [value for value in novelty if value > 0.0]
        if len(positive) < 16:
            raise AudioAnalysisError("audio_beat_analysis_failed")
        median = statistics.median(positive)
        deviation = statistics.median(abs(value - median) for value in positive)
        threshold = median + deviation
        gated = [value if value >= threshold else 0.0 for value in novelty]
        if sum(value > 0.0 for value in gated) < 8:
            raise AudioAnalysisError("audio_beat_analysis_failed")

        peaks: list[int] = []
        for index in range(1, len(gated) - 1):
            value = gated[index]
            if value <= 0.0 or value < gated[index - 1] or value < gated[index + 1]:
                continue
            if peaks and index - peaks[-1] <= 4:
                if value > gated[peaks[-1]]:
                    peaks[-1] = index
            else:
                peaks.append(index)
        if len(peaks) < 8:
            raise AudioAnalysisError("audio_beat_analysis_failed")

        frame_rate = self.sample_rate / float(self.hop_samples)
        minimum_lag = max(1, round(frame_rate * 60.0 / 180.0))
        maximum_lag = min(len(gated) // 3, round(frame_rate * 60.0 / 70.0))
        candidates: list[tuple[float, int]] = []
        for lag in range(minimum_lag, maximum_lag + 1):
            numerator = sum(gated[index] * gated[index - lag] for index in range(lag, len(gated)))
            left_energy = sum(gated[index] ** 2 for index in range(lag, len(gated)))
            right_energy = sum(gated[index - lag] ** 2 for index in range(lag, len(gated)))
            denominator = math.sqrt(left_energy * right_energy)
            score = numerator / denominator if denominator > 0.0 else 0.0
            candidates.append((score, lag))
        score, lag = max(candidates, default=(0.0, 0))
        median_score = statistics.median(value for value, _ in candidates) if candidates else 0.0
        prominence = max(0.0, min(1.0, (score - median_score) / max(score, 1e-12)))
        if lag <= 0 or score < 0.2 or prominence < 0.25:
            raise AudioAnalysisError("audio_beat_analysis_failed")

        tolerance = max(2, round(frame_rate * 0.06))
        phase_scores = [0.0] * lag
        for phase_candidate in range(lag):
            total = 0.0
            for peak in peaks:
                remainder = (peak - phase_candidate) % lag
                distance = min(remainder, lag - remainder)
                if distance <= tolerance:
                    total += gated[peak]
            phase_scores[phase_candidate] = total
        phase = max(range(lag), key=phase_scores.__getitem__)
        step = lag / frame_rate
        first = phase / frame_rate
        bpm = 60.0 / step
        count = int(math.floor((duration - first) / step))
        grid = tuple(round(first + index * step, 6) for index in range(count + 1))
        if len(grid) < 16 or grid[0] < 0.0 or grid[-1] > duration + 0.001:
            raise AudioAnalysisError("audio_beat_analysis_failed")
        grid_frames = tuple(round(value * frame_rate) for value in grid)
        aligned_onsets = sum(
            min(abs(peak - grid_frame) for grid_frame in grid_frames) <= tolerance
            for peak in peaks
        )
        beat_evidence = tuple(
            min(abs(peak - grid_frame) for peak in peaks) <= tolerance
            for grid_frame in grid_frames
        )
        onset_fraction = aligned_onsets / float(len(peaks))
        grid_coverage = sum(beat_evidence) / float(len(grid_frames))
        if onset_fraction < 0.25 or grid_coverage < 0.25:
            raise AudioAnalysisError("audio_beat_analysis_failed")
        return bpm, grid, beat_evidence, score, prominence, onset_fraction, grid_coverage
