"""
Pluggable event detection framework.

EventDetector is the abstract base class. Subclass it and implement detect()
to create new detectors (AWA-based tacks, speed drops, etc.) without touching
the pipeline.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any
import numpy as np


class EventDetector(ABC):
    """Base class for sailing event detectors."""

    @abstractmethod
    def detect(self, video_name: str, log_data: dict, columns: dict,
               date: str) -> List[Dict[str, Any]]:
        """Detect events in a single video's log data.

        Args:
            video_name: video basename (e.g. 'VID_20260315_120000_00_001')
            log_data: dict mapping column name -> numpy array of decoded values
            columns: dict mapping column name -> column index
            date: video date string (e.g. '2026/03/15')

        Returns:
            List of event dicts with at least: id, video, time, type, comment,
            source, date
        """
        raise NotImplementedError


class HeadingManeuverDetector(EventDetector):
    """Detects tacks and gybes from heading changes.

    Uses a simplified version of detect_tacks_in_heading: finds periods where
    heading is steady, then not steady, then steady again with a large heading
    change across the transition.

    Classifies by AWA at the maneuver midpoint:
        |AWA| < 90 => tack, |AWA| >= 90 => gybe
    """

    def __init__(self, min_turn=60, steady_std_max=12, steady_seconds=30,
                 min_gap_seconds=30, sample_rate=10):
        self.min_turn = min_turn
        self.steady_std_max = steady_std_max
        self.steady_seconds = steady_seconds
        self.min_gap_seconds = min_gap_seconds
        self.sample_rate = sample_rate

    def detect(self, video_name, log_data, columns, date):
        hdg = log_data.get('hdg')
        if hdg is None or len(hdg) < self.sample_rate * 60:
            return []

        maneuvers = self._find_maneuvers(hdg)
        awa = log_data.get('awa')
        events = []

        for sample_idx, heading_change in maneuvers:
            time_s = sample_idx / self.sample_rate

            if awa is not None and sample_idx < len(awa):
                awa_val = awa[sample_idx]
                event_type = 'tack' if abs(awa_val) < 90 else 'gybe'
            else:
                event_type = 'tack' if abs(heading_change) < 120 else 'gybe'

            direction = 'P-to-S' if heading_change > 0 else 'S-to-P'
            comment = f'{direction} {heading_change:+.0f} deg'

            events.append({
                'id': f'auto-{video_name}-{time_s:.1f}',
                'video': video_name,
                'time': round(time_s, 1),
                'type': event_type,
                'comment': comment,
                'source': 'auto',
                'date': date,
            })

        return events

    def _find_maneuvers(self, heading):
        """Detect maneuvers from a heading array sampled at self.sample_rate Hz."""
        import pandas as pd

        heading = np.asarray(heading, dtype=float)
        unwrapped = np.degrees(np.unwrap(np.radians(heading)))

        sr = self.sample_rate
        steady_win = int(self.steady_seconds * sr)
        hdg_series = pd.Series(unwrapped)

        rolling_std = hdg_series.rolling(
            window=steady_win, center=True, min_periods=steady_win // 2
        ).std()
        is_steady = (rolling_std < self.steady_std_max).values

        smooth_win = int(self.steady_seconds * sr)
        smoothed = hdg_series.rolling(
            window=smooth_win, center=True, min_periods=smooth_win // 2
        ).mean().values

        delta_samples = int(self.steady_seconds * sr * 1.5)
        delta = np.zeros(len(smoothed))
        half = delta_samples // 2
        if half > 0 and delta_samples < len(smoothed):
            delta[half:-half] = smoothed[delta_samples:] - smoothed[:-delta_samples]

        maneuver_buf = int(self.min_gap_seconds * sr // 2)

        was_steady = pd.Series(is_steady.astype(float)).rolling(
            window=maneuver_buf, center=False, min_periods=1
        ).max().values > 0.5
        will_steady = pd.Series(is_steady[::-1].astype(float)).rolling(
            window=maneuver_buf, center=False, min_periods=1
        ).max().values[::-1] > 0.5

        is_maneuver = was_steady & ~is_steady & will_steady & (np.abs(delta) > self.min_turn)

        candidates = np.where(is_maneuver)[0]
        if len(candidates) == 0:
            return []

        min_gap = int(self.min_gap_seconds * sr)
        abs_delta = np.abs(delta)
        result = []
        cluster_best_idx = candidates[0]
        cluster_best_val = abs_delta[candidates[0]]

        for i in range(1, len(candidates)):
            if candidates[i] - candidates[i - 1] > min_gap:
                result.append((int(cluster_best_idx), float(delta[cluster_best_idx])))
                cluster_best_idx = candidates[i]
                cluster_best_val = abs_delta[candidates[i]]
            elif abs_delta[candidates[i]] > cluster_best_val:
                cluster_best_idx = candidates[i]
                cluster_best_val = abs_delta[candidates[i]]

        result.append((int(cluster_best_idx), float(delta[cluster_best_idx])))
        return result
