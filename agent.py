#!/usr/bin/env python3
"""寝室センサーの値を Google Cloud（Cloud Run の SRP）へ送る本番用プログラム。

設計（docs/00_design.md 第4.2章、2026-10-13 決定）
  - 温度・湿度・気圧・CO2・明るさ : 1分ごとに測り、10分ごとに平均・最小・最大を送る
  - 騒音                         : 1秒ごとに測り、10分ごとに Leq・最大・物音を送る（1秒の値は送らない）
  - 24時間動かし続ける。睡眠の時間帯はサーバーが Apple Watch の就寝・起床で切り出す
  - 送れなかった分はファイルに残し、つながったら送り直す

使い方
  python agent.py --check      センサーとマイクを1回ずつ読み、サーバーに鍵が通るか確かめる
  python agent.py --dry-run    送らずに、10分ごとに送る内容を画面に出す
  python agent.py              本番（ふだんは systemd から動かす）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

import settings as settings_mod
from aggregate import BucketAggregator
from noise import NoiseMonitor, measure_seconds
from outbox import AuthError, HttpSender, Outbox, RejectedError, TemporaryError
from sensors import SensorSet, list_ds18b20

log = logging.getLogger("srp.agent")

SAMPLE_OFFSET_SEC = 3   # 区切りの3秒後に測る（マイクの最後の1秒が届くのを待つ）


class PrintSender:
    """--dry-run 用。送らずに画面へ出す。"""

    def send(self, payload: dict) -> dict:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return {"dry_run": True}


class Agent:
    def __init__(self, s, sender, sensors=None, clock=None):
        self.s = s
        self.sender = sender
        self.sensors = sensors or SensorSet(s)
        self.agg = BucketAggregator(minutes=s.bucket_minutes)
        self.outbox = Outbox(s.data_path, keep_days=s.keep_days)
        self.noise = None
        if s.use_inmp441:
            self.noise = NoiseMonitor(s, self.agg.add_noise_second, self.agg.add_noise_event)
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._stop = threading.Event()

    def stop(self, *_):
        self._stop.set()

    def next_sample_time(self, now: datetime) -> datetime:
        step = self.s.sample_interval_sec
        base = now.replace(microsecond=0)
        sec_of_day = base.hour * 3600 + base.minute * 60 + base.second
        nxt = (sec_of_day // step + 1) * step
        return base - timedelta(seconds=sec_of_day) + timedelta(seconds=nxt)

    def tick(self, sample_at: datetime) -> None:
        """1回分: 測る → 終わった10分をファイルに残す → たまっている分を送る。"""
        for key, value in self.sensors.read_all().items():
            self.agg.add(key, sample_at, value)
        for p in self.agg.pop_finished(self.clock()):
            if not p["buckets"] and not p["noise_events"]:
                continue
            p["device_id"] = self.s.device_id
            p["batch_id"] = f"{self.s.device_id}-{p.pop('bucket_start')}"
            self.outbox.put(p)
        res = self.outbox.flush(self.sender)
        if res["sent"]:
            log.info("送信 %d 件（未送信 %d 件）", res["sent"], res["pending"])

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("開始します。送り先: %s（device_id=%s）", self.s.ingest_url, self.s.device_id)
        if self.noise is not None:
            self.noise.start()
        self.outbox.prune()
        self.outbox.flush(self.sender)
        last_prune = time.monotonic()
        try:
            while not self._stop.is_set():
                sample_at = self.next_sample_time(self.clock())
                wait = (sample_at - self.clock()).total_seconds() + SAMPLE_OFFSET_SEC
                if wait > 0 and self._stop.wait(wait):
                    break
                self.tick(sample_at)
                if time.monotonic() - last_prune > 3600:
                    self.outbox.prune()
                    last_prune = time.monotonic()
        finally:
            log.info("終了します（測りかけの10分は送りません）")
            if self.noise is not None:
                self.noise.stop()
            self.sensors.close()


def check(s) -> int:
    """設置の確認。センサー → マイク → サーバーの順に1回ずつ試す。"""
    ok = True
    print("== センサー ==")
    found = list_ds18b20()
    print(f"  見つかった DS18B20: {', '.join(found) or 'なし'}")
    for label, sid in (("室温用", s.room_ds18b20_id), ("布団用", s.futon_ds18b20_id)):
        if sid and sid not in found:
            print(f"  ! {label}の ID {sid} が見つかりません")
            ok = False
    sensors = SensorSet(s)
    try:
        values = sensors.read_all()
        for key in sensors.slots:
            if key in values:
                print(f"  {key:13s} {values[key]:10.2f}")
            else:
                print(f"  {key:13s} 読めません（上のログを確認）")
                ok = False
    finally:
        sensors.close()

    if s.use_inmp441:
        print("== マイク（3秒） ==")
        try:
            dbs = measure_seconds(s, 3)
            print("  " + "  ".join(f"{d:.1f}" for d in dbs) + "  （音の大きさ。校正していない目安）")
            if not dbs:
                print("  ! 音が全部0です（SD・VDD・L/R の配線を確認）")
                ok = False
        except Exception as e:
            print(f"  ! {e}")
            ok = False

    print("== サーバー ==")
    if not s.base_url or not s.api_key:
        print("  ! base_url（config.toml）か SRP_INGEST_KEY（~/.config/srp-agent/agent.env）が空です")
        return 1
    try:
        HttpSender(s.ingest_url, s.api_key, s.send_timeout_sec).send(
            {"device_id": s.device_id, "batch_id": "check", "buckets": [], "noise_events": []}
        )
        print(f"  OK: {s.ingest_url} に鍵が通りました（何も保存していません）")
    except (AuthError, RejectedError, TemporaryError) as e:
        print(f"  ! {e}")
        ok = False
    print("\n結果:", "すべて OK" if ok else "確認が必要な項目があります")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SRP 寝室センサーの送信プログラム")
    p.add_argument("--config", help="config.toml の場所（既定: このフォルダ）")
    p.add_argument("--check", action="store_true", help="設置の確認だけして終わる")
    p.add_argument("--dry-run", action="store_true", help="送らずに送る内容を画面に出す")
    p.add_argument("--verbose", action="store_true", help="細かいログも出す")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        s = settings_mod.load(args.config)
    except FileNotFoundError:
        print("config.toml がありません。config.example.toml をコピーして作ってください。")
        return 1
    except ValueError as e:
        print(f"config.toml の値が正しくありません: {e}")
        return 1

    if args.check:
        return check(s)
    if args.dry_run:
        s.data_dir = os.path.join(s.data_dir, "dry-run")
        sender = PrintSender()
    else:
        if not s.base_url or not s.api_key:
            print("base_url（config.toml）か SRP_INGEST_KEY（~/.config/srp-agent/agent.env）が空です。")
            return 1
        sender = HttpSender(s.ingest_url, s.api_key, s.send_timeout_sec)
    Agent(s, sender).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
