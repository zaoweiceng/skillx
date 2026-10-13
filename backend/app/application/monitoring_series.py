"""Bounded, timezone-aligned monitoring buckets shared by every metric."""

from datetime import UTC, datetime
from math import ceil, floor

from app.infrastructure.database import rfc3339


def interval_seconds(start: datetime, end: datetime) -> int:
    hours = (end - start).total_seconds() / 3600
    if hours <= 1:
        return 300
    if hours <= 3:
        return 900
    if hours <= 12:
        return 1800
    if hours <= 72:
        return 3600
    return max(1, ceil(hours / 24 / 90)) * 86400


class TimeSeries:
    def __init__(self, start: datetime, end: datetime, offset_minutes: int = 0):
        self.start, self.end = start.timestamp(), end.timestamp()
        self.interval = interval_seconds(start, end)
        offset = offset_minutes * 60
        self.anchor = floor((self.start + offset) / self.interval) * self.interval - offset
        count = ceil((self.end - self.anchor) / self.interval)
        self.sums = [0.0] * count
        self.samples = [0] * count

    def add(self, at: datetime, value: float, samples: int = 1):
        timestamp = (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()
        if self.start <= timestamp < self.end:
            index = floor((timestamp - self.anchor) / self.interval)
            self.sums[index] += value
            self.samples[index] += samples

    def view(self, *, average: bool = False):
        return {
            "intervalSeconds": self.interval,
            "series": [
                {
                    "start": rfc3339(
                        datetime.fromtimestamp(
                            max(self.start, self.anchor + index * self.interval), UTC
                        )
                    ),
                    "end": rfc3339(
                        datetime.fromtimestamp(
                            min(self.end, self.anchor + (index + 1) * self.interval), UTC
                        )
                    ),
                    "value": (total / count if count else None) if average else total,
                    "samples": count,
                }
                for index, (total, count) in enumerate(zip(self.sums, self.samples, strict=True))
            ],
        }
