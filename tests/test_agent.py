"""送信プログラムのテスト。センサーもネットもいらない（偽物で置き換える）。

実行（srp_agent フォルダで）:
  python -m unittest discover -s tests -t .
"""
import array
import http.server
import json
import math
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# smbus2 が無くても読み込めるように、偽物を入れておく（実機では本物を使う）
if "smbus2" not in sys.modules:
    try:
        import smbus2  # noqa: F401
    except ImportError:
        fake = types.ModuleType("smbus2")

        class _Msg:
            def __init__(self, addr, kind, data=None, n=0):
                self.addr, self.kind, self.buf = addr, kind, list(data) if data else [0] * n

            def __iter__(self):
                return iter(self.buf)

        class i2c_msg:  # noqa: N801
            @staticmethod
            def write(addr, data):
                return _Msg(addr, "w", data=data)

            @staticmethod
            def read(addr, n):
                return _Msg(addr, "r", n=n)

        fake.i2c_msg = i2c_msg
        fake.SMBus = None
        sys.modules["smbus2"] = fake

import aggregate  # noqa: E402
import noise  # noqa: E402
import outbox  # noqa: E402
import sensors  # noqa: E402
import settings as settings_mod  # noqa: E402
from agent import Agent  # noqa: E402

JST = timezone(timedelta(hours=9))


def make_settings(tmp, **over):
    path = os.path.join(tmp, "config.toml")
    with open(os.path.join(HERE, "..", "config.example.toml"), encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    env = os.path.join(tmp, "agent.env")
    with open(env, "w") as f:
        f.write("SRP_INGEST_KEY=test-key\n")
    s = settings_mod.load(path, env)
    s.data_dir = os.path.join(tmp, "data")
    for k, v in over.items():
        setattr(s, k, v)
    return s


class TestSettings(unittest.TestCase):
    def test_example_loads_and_key_comes_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("SRP_INGEST_KEY", None)
            s = make_settings(tmp)
            self.assertEqual(s.api_key, "test-key")
            self.assertEqual(s.sample_interval_sec, 60)
            self.assertEqual(s.bucket_minutes, 10)
            self.assertTrue(s.ingest_url.endswith("/api/ingest/env-buckets"))


class TestAggregate(unittest.TestCase):
    def test_ten_minutes_become_one_bucket(self):
        agg = aggregate.BucketAggregator(10)
        t0 = datetime(2026, 10, 13, 23, 0, tzinfo=JST)
        for m in range(10):
            agg.add("room_temp", t0 + timedelta(minutes=m), 20.0 + m * 0.1)
        for sec in range(1, 601):
            agg.add_noise_second(t0 + timedelta(seconds=sec), 30.0 if sec != 100 else 60.0)
        agg.add_noise_event({"started_at": "x", "peak_db": 60.0, "_t": t0 + timedelta(seconds=99)})
        self.assertEqual(agg.pop_finished(t0 + timedelta(minutes=10)), [])   # まだ猶予の2秒内
        out = agg.pop_finished(t0 + timedelta(minutes=10, seconds=3))
        self.assertEqual(len(out), 1)
        b = {x["sensor_key"]: x for x in out[0]["buckets"]}
        self.assertEqual(b["room_temp"]["sample_count"], 10)
        self.assertAlmostEqual(b["room_temp"]["mean_value"], 20.45)
        self.assertEqual(b["noise_db"]["sample_count"], 600)
        self.assertEqual(b["noise_db"]["max_value"], 60.0)
        self.assertEqual(b["noise_db"]["event_count"], 1)
        self.assertGreater(b["noise_db"]["leq_db"], 30.0)     # Leq は大きな音に引っぱられる
        self.assertEqual(out[0]["noise_events"], [{"started_at": "x", "peak_db": 60.0}])

    def test_leq(self):
        self.assertAlmostEqual(aggregate.leq([30.0, 30.0]), 30.0)
        self.assertAlmostEqual(aggregate.leq([30.0, 40.0]), 37.4, places=1)


class TestNoise(unittest.TestCase):
    def test_dbfs_of_sine(self):
        amp = int(2 ** 31 * 10 ** (-40 / 20))
        a = array.array("i")
        for i in range(48000):
            a.append(int(amp * math.sin(2 * math.pi * 1000 * i / 48000)) + 5000)
            a.append(0)
        self.assertAlmostEqual(noise.dbfs_of_chunk(a.tobytes(), 0), -43.0, delta=0.1)
        self.assertIsNone(noise.dbfs_of_chunk(a.tobytes(), 1))

    def test_event_detector(self):
        d = noise.EventDetector(margin_db=15, window_sec=300, warmup_sec=60)
        t = datetime(2026, 10, 13, 23, 0, tzinfo=JST)
        events = []
        series = [30.0] * 120 + [50.0, 55.0, 52.0] + [30.0] * 10
        for i, db in enumerate(series):
            e = d.feed(t + timedelta(seconds=i + 1), db)
            if e:
                events.append(e)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["peak_db"], 55.0)
        self.assertEqual(e["duration_sec"], 3.0)
        self.assertEqual(e["threshold_db"], 45.0)
        self.assertEqual(e["started_at"], (t + timedelta(seconds=120)).isoformat(timespec="seconds"))

    def test_no_events_during_warmup(self):
        d = noise.EventDetector(margin_db=15, warmup_sec=60)
        t = datetime(2026, 10, 13, 23, 0, tzinfo=JST)
        self.assertIsNone(d.feed(t, 30.0))
        self.assertIsNone(d.feed(t + timedelta(seconds=1), 90.0))
        self.assertIsNone(d.feed(t + timedelta(seconds=2), 30.0))

    def test_find_card(self):
        listing = ("card 3: sndrpigooglevoi [snd_rpi_googlevoicehat_soundcar], device 0: "
                   "Google voiceHAT SoundCard HiFi voicehat-hifi-0 [x]\n")
        orig = noise.subprocess.run
        noise.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=listing)
        try:
            self.assertEqual(noise.find_card(""), (3, 0))
            self.assertEqual(noise.find_card("3"), (3, 0))
            with self.assertRaises(RuntimeError):
                noise.find_card("5")
        finally:
            noise.subprocess.run = orig


class FakeBus:
    """BMP180（データシートの例の値）・BH1750・SCD40 を返す偽の I2C。"""

    CAL = dict(AC1=408, AC2=-72, AC3=-14383, AC4=32741, AC5=32757, AC6=23153,
               B1=6190, B2=4, MB=-32768, MC=-8711, MD=2868)
    ORDER = ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "B1", "B2", "MB", "MC", "MD"]

    def __init__(self, present=(0x77, 0x23, 0x62)):
        self.present = set(present)
        self.ctrl = None
        self.scd_last = None
        self.closed = False

    def _chk(self, a):
        if a not in self.present:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, a, reg):
        self._chk(a)
        return 0x55

    def read_i2c_block_data(self, a, reg, n):
        self._chk(a)
        if reg == 0xAA:
            out = []
            for k in self.ORDER:
                v = self.CAL[k] & 0xFFFF
                out += [v >> 8, v & 0xFF]
            return out
        if self.ctrl == 0x2E:
            return [27898 >> 8, 27898 & 0xFF]
        oss = (self.ctrl - 0x34) >> 6
        raw = self.up_for_1000hpa(oss) << (8 - oss)
        return [(raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF][:n]

    @classmethod
    def up_for_1000hpa(cls, oss):
        """補正後に約 1000 hPa になる生の値を探す（データシートの補正係数のまま）。"""
        lo, hi = 0, 1 << (16 + oss)
        while lo < hi:
            mid = (lo + hi) // 2
            if sensors.bmp180_compensate(cls.CAL, 27898, mid, oss)[1] < 100000:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def write_byte_data(self, a, reg, v):
        self._chk(a)
        self.ctrl = v

    def write_byte(self, a, v):
        self._chk(a)

    def i2c_rdwr(self, *msgs):
        for m in msgs:
            self._chk(m.addr)
            if m.addr == 0x23:
                m.buf = [0x01, 0x2C]                      # 300 / 1.2 = 250 lx
            elif m.kind == "w":
                self.scd_last = (m.buf[0] << 8) | m.buf[1]
            else:
                words = {0x3682: [1, 2, 3], 0xE4B8: [0x8006], 0xEC05: [0x01F4, 0x6667, 0x5EB9]}[self.scd_last]
                out = []
                for w in words:
                    b = [w >> 8, w & 0xFF]
                    out += b + [sensors.crc8(b)]
                m.buf = out

    def close(self):
        self.closed = True


class FakeSysfs:
    def __init__(self, tmp):
        self.w1 = os.path.join(tmp, "w1")
        self.iio = os.path.join(tmp, "iio")
        for sid, t in (("28-aaa", 21125), ("28-bbb", 30500)):
            os.makedirs(os.path.join(self.w1, sid))
            with open(os.path.join(self.w1, sid, "w1_slave"), "w") as f:
                f.write(f"xx : crc=00 YES\nxx t={t}\n")
        dev = os.path.join(self.iio, "iio:device0")
        os.makedirs(dev)
        for name, v in (("name", "dht11@5"), ("in_humidityrelative_input", "55000"),
                        ("in_temp_input", "24000")):
            with open(os.path.join(dev, name), "w") as f:
                f.write(v + "\n")
        sensors.W1_DIR, sensors.IIO_DIR = self.w1, self.iio


class TestSensors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakeSysfs(self.tmp)
        sensors.time.sleep = lambda s: None

    def test_all_sensors(self):
        s = make_settings(self.tmp, room_ds18b20_id="28-aaa", futon_ds18b20_id="28-bbb")
        ss = sensors.SensorSet(s, bus_factory=FakeBus)
        v = ss.read_all()
        self.assertEqual(v["room_temp"], 21.125)
        self.assertEqual(v["bed_temp"], 30.5)
        self.assertEqual(v["humidity"], 55.0)
        self.assertAlmostEqual(v["light_lux"], 250.0)
        self.assertEqual(v["co2_ppm"], 500.0)
        self.assertAlmostEqual(v["pressure_hpa"], 1000.0, delta=0.1)
        ss.close()

    def test_bmp180_matches_datasheet_example(self):
        cal = FakeBus.CAL
        self.assertEqual(sensors.bmp180_compensate(cal, 27898, 23843, 0), (15.0, 69964))

    def test_missing_sensor_does_not_stop_others(self):
        s = make_settings(self.tmp, room_ds18b20_id="28-aaa", futon_ds18b20_id="")
        ss = sensors.SensorSet(s, bus_factory=lambda: FakeBus(present=(0x23,)))
        with self.assertLogs("srp.sensors", level="WARNING") as cm:
            v = ss.read_all()
        self.assertIn("room_temp", v)
        self.assertIn("light_lux", v)
        self.assertNotIn("bed_temp", v)       # ID が空
        self.assertNotIn("co2_ppm", v)        # 0x62 が無い
        self.assertNotIn("pressure_hpa", v)   # 0x77 が無い
        self.assertTrue(any("futon_ds18b20_id" in m for m in cm.output))


class FakeSender:
    def __init__(self, fail_times=0):
        self.sent, self.fail_times = [], fail_times

    def send(self, payload):
        if self.fail_times:
            self.fail_times -= 1
            raise outbox.TemporaryError("ネットがない")
        self.sent.append(json.loads(json.dumps(payload)))
        return {}


class TestOutbox(unittest.TestCase):
    def test_keeps_until_sent_and_resends_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = outbox.Outbox(tmp)
            for i in range(3):
                ob.put({"batch_id": f"pi-2026-10-13T23:{i}0:00+09:00", "buckets": [], "noise_events": []})
            sender = FakeSender(fail_times=1)
            r = ob.flush(sender)
            self.assertEqual((r["sent"], r["pending"]), (0, 3))
            r = ob.flush(sender)
            self.assertEqual((r["sent"], r["pending"]), (3, 0))
            self.assertEqual([p["batch_id"][-14:-6] for p in sender.sent], ["23:00:00", "23:10:00", "23:20:00"])

    def test_rejected_payload_moves_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = outbox.Outbox(tmp)
            ob.put({"batch_id": "bad", "buckets": []})

            class Rej:
                def send(self, p):
                    raise outbox.RejectedError("400")
            with self.assertLogs("srp.outbox", level="ERROR"):
                r = ob.flush(Rej())
            self.assertEqual((r["rejected"], r["pending"]), (1, 0))
            self.assertEqual(os.listdir(os.path.join(tmp, "failed")), ["bad.json"])

    def test_prune_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            ob = outbox.Outbox(tmp, keep_days=1)
            p = ob.put({"batch_id": "old"})
            os.utime(p, (0, 0))
            with self.assertLogs("srp.outbox", level="WARNING"):
                self.assertEqual(ob.prune(), 1)


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.seen.append((self.headers.get("X-API-Key"), json.loads(body)))
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


class TestHttpSender(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/api/ingest/env-buckets"

    def tearDown(self):
        self.srv.shutdown()

    def test_status_codes(self):
        snd = outbox.HttpSender(self.url, "k", timeout=5)
        _Handler.status = 200
        self.assertEqual(snd.send({"a": 1}), {"ok": True})
        self.assertEqual(_Handler.seen[-1], ("k", {"a": 1}))
        for code, exc in ((401, outbox.AuthError), (422, outbox.RejectedError), (503, outbox.TemporaryError)):
            _Handler.status = code
            with self.assertRaises(exc):
                snd.send({"a": 1})

    def test_unreachable_is_temporary(self):
        snd = outbox.HttpSender("http://127.0.0.1:9/x", "k", timeout=2)
        with self.assertRaises(outbox.TemporaryError):
            snd.send({})


class TestAgentTick(unittest.TestCase):
    def test_twenty_minutes_make_two_payloads(self):
        tmp = tempfile.mkdtemp()
        FakeSysfs(tmp)
        sensors.time.sleep = lambda s: None
        s = make_settings(tmp, room_ds18b20_id="28-aaa", futon_ds18b20_id="28-bbb", use_inmp441=False)
        now = {"t": None}
        sender = FakeSender()
        a = Agent(s, sender, sensors=sensors.SensorSet(s, bus_factory=FakeBus), clock=lambda: now["t"])
        t0 = datetime(2026, 10, 13, 23, 0, tzinfo=JST)
        self.assertEqual(a.next_sample_time(t0 + timedelta(seconds=20)), t0 + timedelta(minutes=1))
        for m in range(21):
            at = t0 + timedelta(minutes=m)
            now["t"] = at + timedelta(seconds=3)
            a.tick(at)
        self.assertEqual(len(sender.sent), 2)
        p = sender.sent[0]
        self.assertEqual(p["batch_id"], "bedroom-pi-2026-10-13T23:00:00+09:00")
        self.assertEqual(p["device_id"], "bedroom-pi")
        keys = {b["sensor_key"]: b for b in p["buckets"]}
        self.assertEqual(set(keys), {"room_temp", "bed_temp", "humidity", "pressure_hpa", "light_lux", "co2_ppm"})
        self.assertTrue(all(b["sample_count"] == 10 for b in p["buckets"]))
        # サーバーの受け口のテスト（backend/tests/test_env_buckets.py）で使う見本として書き出す
        sample = os.environ.get("SRP_WRITE_SAMPLE_PAYLOAD")
        if sample:
            with open(sample, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    unittest.main()
