"""1分ごとの値と1秒ごとの騒音を、10分ごとの値（バケット）にまとめる。

サーバーの /api/ingest/env-buckets にそのまま渡せる形を作る。
"""
from __future__ import annotations

import math
import statistics
import threading
from datetime import datetime, timedelta

NOISE_KEY = "noise_db"


def bucket_start_of(t: datetime, minutes: int = 10) -> datetime:
    """時刻を10分の区切りの先頭に丸める（タイムゾーンはそのまま）。"""
    t = t.replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % minutes)


def leq(values: list[float]) -> float:
    """dB の平均は「エネルギーの平均」で取る（等価騒音レベル Leq）。"""
    energy = sum(10 ** (v / 10.0) for v in values) / len(values)
    return 10.0 * math.log10(energy)


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


class BucketAggregator:
    """センサー値をためて、終わった10分ぶんを取り出す。複数のスレッドから呼んでよい。"""

    def __init__(self, minutes: int = 10, grace_sec: float = 2.0) -> None:
        self.minutes = minutes
        self.grace = timedelta(seconds=grace_sec)
        self._lock = threading.Lock()
        self._values: dict[datetime, dict[str, list[float]]] = {}
        self._events: list[dict] = []

    # --- ためる ---
    def add(self, sensor_key: str, t: datetime, value: float) -> None:
        b = bucket_start_of(t, self.minutes)
        with self._lock:
            self._values.setdefault(b, {}).setdefault(sensor_key, []).append(float(value))

    def add_noise_second(self, t: datetime, db: float) -> None:
        # t はその1秒の終わりの時刻。真ん中の時刻で区切りを決める。
        self.add(NOISE_KEY, t - timedelta(seconds=0.5), db)

    def add_noise_event(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)

    # --- 取り出す ---
    def pop_finished(self, now: datetime) -> list[dict]:
        """終わった10分ぶんを、送る形（payload）にして返す。古い順。"""
        out = []
        with self._lock:
            done = sorted(
                b for b in self._values
                if b + timedelta(minutes=self.minutes) + self.grace <= now
            )
            for b in done:
                per_sensor = self._values.pop(b)
                end = b + timedelta(minutes=self.minutes)
                events = [e for e in self._events if e["_t"] < end]
                self._events = [e for e in self._events if e["_t"] >= end]
                out.append(self._payload(b, per_sensor, events))
        return out

    def _payload(self, b: datetime, per_sensor: dict[str, list[float]], events: list[dict]) -> dict:
        buckets = []
        for key in sorted(per_sensor):
            vals = per_sensor[key]
            row = {
                "sensor_key": key,
                "bucket_start": b.isoformat(),
                "mean_value": _r(statistics.fmean(vals)),
                "min_value": _r(min(vals)),
                "max_value": _r(max(vals)),
                "sample_count": len(vals),
            }
            if key == NOISE_KEY:
                row["leq_db"] = _r(leq(vals), 2)
                row["event_count"] = sum(
                    1 for e in events if b <= e["_t"] < b + timedelta(minutes=self.minutes)
                )
            buckets.append(row)
        noise_events = [{k: v for k, v in e.items() if not k.startswith("_")} for e in events]
        return {"bucket_start": b.isoformat(), "buckets": buckets, "noise_events": noise_events}
