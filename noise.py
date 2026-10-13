"""INMP441（I2S マイク）で1秒ごとの音の大きさを測り、物音を見つける。

- 音声そのものは保存も送信もしない。1秒ごとに「音の大きさ」の数値にしたら捨てる。
- 音の大きさ = dBFS（デジタルの最大を0とした値）+ noise_db_offset
  既定の offset 120 は、INMP441 の公称感度（94 dB SPL で -26 dBFS）から逆算した目安。
  校正していないので騒音計の値とは一致しない。日ごとの比べ合いにだけ使う。
- 物音 = 直近5分の中央値（その部屋のふだんの音）より noise_event_margin_db 以上
  大きい音が続いたひとかたまり。
"""
from __future__ import annotations

import array
import collections
import logging
import math
import operator
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("srp.noise")

RATE = 48000            # googlevoicehat-soundcard は 48kHz 固定
CHANNELS = 2
BYTES_PER_SAMPLE = 4    # S32_LE（24ビットの値を32ビットの枠で送ってくる）
FULL_SCALE = 2 ** 31
STARTUP_SKIP_SEC = 0.25
CARD_LINE = re.compile(r"^card (\d+): (\S+) \[(.*?)\], device (\d+): (.*)$")


def find_card(wanted: str = "") -> tuple[int, int]:
    """arecord -l から (card, device) を返す。wanted が空なら voicehat を探す。"""
    text = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    for line in text.splitlines():
        m = CARD_LINE.match(line.strip())
        if not m:
            continue
        card, cid, desc, dev = int(m.group(1)), m.group(2), m.group(3) + m.group(5), int(m.group(4))
        if wanted:
            if wanted in (str(card), cid):
                return card, dev
        elif "voicehat" in (cid + desc).lower():
            return card, dev
    raise RuntimeError("録音デバイスが見つかりません（arecord -l と dtoverlay=googlevoicehat-soundcard を確認）")


def dbfs_of_chunk(raw: bytes, channel: int) -> float | None:
    """1秒ぶんの生データから、片方のチャンネルの RMS を dBFS で返す。全部0なら None。"""
    a = array.array("i")
    a.frombytes(raw[: len(raw) - len(raw) % (BYTES_PER_SAMPLE * CHANNELS)])
    if sys.byteorder != "little":
        a.byteswap()
    ch = a[channel::CHANNELS]
    n = len(ch)
    if n == 0:
        return None
    mean = sum(ch) / n
    power = sum(map(operator.mul, ch, ch)) / n - mean * mean   # 直流分を引いた平均パワー
    if power <= 0:
        return None
    return 20.0 * math.log10(math.sqrt(power) / FULL_SCALE)


class EventDetector:
    """1秒ごとの音の大きさから物音を見つける。音のデータには依存しない（テストしやすい）。"""

    def __init__(self, margin_db: float = 15.0, window_sec: int = 300, warmup_sec: int = 60):
        self.margin = margin_db
        self.history: collections.deque[float] = collections.deque(maxlen=window_sec)
        self.warmup = warmup_sec
        self._active = None

    def feed(self, t: datetime, db: float) -> dict | None:
        """t はその1秒の終わり。物音が終わったときに、その物音を dict で返す。"""
        finished = None
        if len(self.history) >= self.warmup:
            background = statistics.median(self.history)
            threshold = background + self.margin
            if db >= threshold:
                if self._active is None:
                    self._active = {"start": t - timedelta(seconds=1), "peak": db,
                                    "values": [db], "threshold": threshold}
                else:
                    self._active["peak"] = max(self._active["peak"], db)
                    self._active["values"].append(db)
            elif self._active is not None:
                finished = self._close()
        if self._active is None:
            self.history.append(db)     # 物音の最中は「ふだんの音」に入れない
        return finished

    def _close(self) -> dict:
        a, self._active = self._active, None
        energy = sum(10 ** (v / 10.0) for v in a["values"]) / len(a["values"])
        return {
            "started_at": a["start"].isoformat(timespec="seconds"),
            "duration_sec": float(len(a["values"])),    # 1秒単位
            "peak_db": round(a["peak"], 1),
            "leq_db": round(10.0 * math.log10(energy), 1),
            "threshold_db": round(a["threshold"], 1),
            "_t": a["start"],
        }


class NoiseMonitor(threading.Thread):
    """arecord を動かし続け、1秒ごとに on_second(t, db)、物音のたびに on_event(e) を呼ぶ。"""

    def __init__(self, settings, on_second, on_event):
        super().__init__(name="noise", daemon=True)
        self.s = settings
        self.on_second = on_second
        self.on_event = on_event
        self.detector = EventDetector(settings.noise_event_margin_db)
        self.channel = 0 if settings.mic_channel == "left" else 1
        self._stop = threading.Event()
        self._proc = None
        self.zero_seconds = 0

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:
                log.warning("マイク: %s。10秒後にやり直します", e)
            if self._stop.wait(10):
                break

    def _run_once(self) -> None:
        card, dev = find_card(self.s.mic_card)
        cmd = ["arecord", "-D", f"plughw:{card},{dev}", "-c", str(CHANNELS), "-r", str(RATE),
               "-f", "S32_LE", "-t", "raw", "-q"]
        log.info("マイク: 録音を始めます（card %s, device %s）", card, dev)
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # arecord のメッセージを読み捨て続ける（ためると arecord が止まるため）。最後の数行だけ残す
        last_err: collections.deque[str] = collections.deque(maxlen=5)
        threading.Thread(
            target=lambda: [last_err.append(l.decode(errors="replace").strip()) for l in self._proc.stderr],
            daemon=True,
        ).start()
        chunk = RATE * CHANNELS * BYTES_PER_SAMPLE
        try:
            self._read_exact(int(RATE * STARTUP_SKIP_SEC) * CHANNELS * BYTES_PER_SAMPLE)
            while not self._stop.is_set():
                raw = self._read_exact(chunk)
                if raw is None:
                    time.sleep(0.2)
                    raise RuntimeError(f"arecord が止まりました（{' / '.join(last_err) or '理由不明'}）")
                t = datetime.now().astimezone()
                dbfs = dbfs_of_chunk(raw, self.channel)
                if dbfs is None:
                    self.zero_seconds += 1
                    if self.zero_seconds in (10, 600):
                        log.warning("マイク: 音が全部0です（SD・VDD・L/R の配線を確認）")
                    continue
                self.zero_seconds = 0
                db = dbfs + self.s.noise_db_offset
                self.on_second(t, db)
                ev = self.detector.feed(t, db)
                if ev is not None:
                    self.on_event(ev)
        finally:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()

    def _read_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            part = self._proc.stdout.read(n - len(buf))
            if not part:
                return None
            buf += part
        return bytes(buf)


def measure_seconds(settings, seconds: int = 3) -> list[float]:
    """確認用（--check）: 数秒だけ録って、1秒ごとの音の大きさを返す。"""
    card, dev = find_card(settings.mic_card)
    channel = 0 if settings.mic_channel == "left" else 1
    secs = seconds + 1
    r = subprocess.run(
        ["arecord", "-D", f"plughw:{card},{dev}", "-c", str(CHANNELS), "-r", str(RATE),
         "-f", "S32_LE", "-t", "raw", "-q", "-d", str(secs)],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "arecord が失敗しました")
    data = r.stdout[int(RATE * STARTUP_SKIP_SEC) * CHANNELS * BYTES_PER_SAMPLE:]
    chunk = RATE * CHANNELS * BYTES_PER_SAMPLE
    out = []
    for i in range(0, len(data) - chunk + 1, chunk):
        d = dbfs_of_chunk(data[i: i + chunk], channel)
        if d is not None:
            out.append(d + settings.noise_db_offset)
    return out

