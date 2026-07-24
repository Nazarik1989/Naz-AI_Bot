"""Shared fail-closed validation for audio-derived rhythmic evidence."""

from __future__ import annotations

import math
from typing import Sequence


MIN_LOCAL_EVIDENCE_COVERAGE = 0.5
MAX_LOCAL_UNEVIDENCED_GAP_SECONDS = 2.0


def beat_evidence_is_valid(
    beat_grid: Sequence[float], beat_evidence: Sequence[bool],
) -> bool:
    if len(beat_grid) < 16 or len(beat_evidence) != len(beat_grid):
        return False
    if any(type(value) is not bool for value in beat_evidence):
        return False
    try:
        beats = tuple(float(value) for value in beat_grid)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) and value >= 0.0 for value in beats)
        and all(left < right for left, right in zip(beats, beats[1:]))
    )


def rhythmic_window_is_valid(
    *, beat_grid: Sequence[float], beat_evidence: Sequence[bool],
    evidence_required: bool, start_seconds: float, duration_seconds: float,
) -> bool:
    """Validate local rhythm coverage for one exact media segment."""

    try:
        start = float(start_seconds)
        duration = float(duration_seconds)
        beats = tuple(float(value) for value in beat_grid)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0.0 or duration <= 0.0:
        return False
    if not evidence_required:
        return True
    if not beat_evidence_is_valid(beats, beat_evidence):
        return False

    end = start + duration
    window = [
        (beat, bool(beat_evidence[index]))
        for index, beat in enumerate(beats)
        if start - 0.001 <= beat <= end + 0.001
    ]
    if len(window) < 2:
        return False
    evidenced = [beat for beat, present in window if present]
    coverage = len(evidenced) / float(len(window))
    if coverage < MIN_LOCAL_EVIDENCE_COVERAGE or not evidenced:
        return False
    gaps = [evidenced[0] - start, end - evidenced[-1]]
    gaps.extend(right - left for left, right in zip(evidenced, evidenced[1:]))
    return max(gaps, default=duration) <= MAX_LOCAL_UNEVIDENCED_GAP_SECONDS + 0.001


def eligible_segment_starts(
    *, beat_grid: Sequence[float], beat_evidence: Sequence[bool],
    evidence_required: bool, track_duration_seconds: float,
    segment_duration_seconds: float, beats_per_bar: int,
) -> tuple[float, ...]:
    try:
        duration = float(track_duration_seconds)
        segment = float(segment_duration_seconds)
        bar_size = max(1, int(beats_per_bar))
        starts = tuple(float(value) for value in beat_grid[::bar_size])
    except (TypeError, ValueError):
        return ()
    return tuple(
        start for start in starts
        if (
            start + segment <= duration + 0.001
            and rhythmic_window_is_valid(
                beat_grid=beat_grid,
                beat_evidence=beat_evidence,
                evidence_required=evidence_required,
                start_seconds=start,
                duration_seconds=segment,
            )
        )
    )
