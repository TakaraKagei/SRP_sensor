"""センサーの読み取り（本番用）。

読み方は raspi/sensor_test の各テストと同じ。
  - 室温用・布団用DS18B20 : Linux の 1-Wire ドライバ（config.txt の dtoverlay=w1-gpio）
  - DHT11               : Linux の dht11 ドライバ（dtoverlay=dht11）。湿度だけ使う
  - BMP180              : I2C 0x77。気圧だけ使う（データシートの計算手順）
  - BH1750              : I2C 0x23 または 0x5C。明るさ
  - SCD40               : I2C 0x62。CO2 だけ使う（約5秒ごとに測り続けるモード）

1つのセンサーで1つの量を測る（docs/10_hardware.md）。DHT11・BMP180・SCD40 の温度は使わない。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time

log = logging.getLogger("srp.sensors")

W1_DIR = "/sys/bus/w1/devices"
IIO_DIR = "/sys/bus/iio/devices"
DS18B20_POWER_ON_RESET_C = 85.0


# ===================================================================== DS18B20
def read_ds18b20(sensor_id: str, retries: int = 3) -> float:
    path = os.path.join(W1_DIR, sensor_id, "w1_slave")
    last = ""
    for _ in range(retries):
        with open(path) as f:
            lines = f.read().strip().splitlines()
        if len(lines) >= 2 and lines[0].strip().endswith("YES"):
            m = re.search(r"t=(-?\d+)", lines[1])
            if m:
                t = int(m.group(1)) / 1000.0
                if t == DS18B20_POWER_ON_RESET_C:
                    raise RuntimeError("85.0℃（電源投入直後の初期値）。電源線を確認")
                return t
        last = " / ".join(lines)
        time.sleep(0.2)
    raise RuntimeError(f"CRC チェックに失敗（{last}）")


def list_ds18b20() -> list[str]:
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(W1_DIR, "28-*")))


# ======================================================================= DHT11
def find_dht11() -> str:
    candidates = []
    for d in sorted(glob.glob(os.path.join(IIO_DIR, "iio:device*"))):
        if os.path.exists(os.path.join(d, "in_humidityrelative_input")):
            try:
                with open(os.path.join(d, "name")) as f:
                    name = f.read().strip()
            except OSError:
                name = ""
            candidates.append((0 if "dht" in name.lower() else 1, d))
    if not candidates:
        raise RuntimeError("DHT11 が見つかりません（dtoverlay=dht11 を確認）")
    return sorted(candidates)[0][1]


def read_dht11_humidity(dev: str, retries: int = 2) -> float:
    last = None
    for i in range(retries):
        try:
            with open(os.path.join(dev, "in_humidityrelative_input")) as f:
                return int(f.read().strip()) / 1000.0
        except OSError as e:          # DHT11 はときどき読み損ねる
            last = e
            if i + 1 < retries:
                time.sleep(2.5)       # データシート: 読み取りは2秒以上あける
    raise RuntimeError(f"読み取りに失敗（{last}）")


# ====================================================================== BMP180
def _s16(msb, lsb):
    v = (msb << 8) | lsb
    return v - 0x10000 if v & 0x8000 else v


def _u16(msb, lsb):
    return (msb << 8) | lsb


def _cdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def bmp180_compensate(cal: dict, ut: int, up: int, oss: int) -> tuple[float, int]:
    """Bosch BMP180 データシートの補正手順。(温度℃, 気圧Pa)"""
    x1 = ((ut - cal["AC6"]) * cal["AC5"]) >> 15
    x2 = _cdiv(cal["MC"] << 11, x1 + cal["MD"])
    b5 = x1 + x2
    t = (b5 + 8) >> 4
    b6 = b5 - 4000
    x1 = (cal["B2"] * ((b6 * b6) >> 12)) >> 11
    x2 = (cal["AC2"] * b6) >> 11
    x3 = x1 + x2
    b3 = _cdiv(((cal["AC1"] * 4 + x3) << oss) + 2, 4)
    x1 = (cal["AC3"] * b6) >> 13
    x2 = (cal["B1"] * ((b6 * b6) >> 12)) >> 16
    x3 = ((x1 + x2) + 2) >> 2
    b4 = (cal["AC4"] * ((x3 + 32768) & 0xFFFFFFFF)) >> 15
    b7 = (((up - b3) & 0xFFFFFFFF) * (50000 >> oss)) & 0xFFFFFFFF
    p = _cdiv(b7 * 2, b4) if b7 < 0x80000000 else _cdiv(b7, b4) * 2
    x1 = (p >> 8) * (p >> 8)
    x1 = (x1 * 3038) >> 16
    x2 = (-7357 * p) >> 16
    p = p + ((x1 + x2 + 3791) >> 4)
    return t / 10.0, p


class BMP180:
    ADDRESS = 0x77
    WAIT = {0: 0.005, 1: 0.008, 2: 0.014, 3: 0.026}

    def __init__(self, bus, oss: int = 3):
        self.bus, self.oss = bus, oss
        chip = bus.read_byte_data(self.ADDRESS, 0xD0)
        if chip != 0x55:
            raise RuntimeError(f"チップ ID が 0x{chip:02X}（BMP180 なら 0x55）")
        raw = bus.read_i2c_block_data(self.ADDRESS, 0xAA, 22)
        w = [(raw[i], raw[i + 1]) for i in range(0, 22, 2)]
        if any(_u16(*x) in (0, 0xFFFF) for x in w):
            raise RuntimeError("補正係数が読めません（通信が不安定）")
        self.cal = {
            "AC1": _s16(*w[0]), "AC2": _s16(*w[1]), "AC3": _s16(*w[2]),
            "AC4": _u16(*w[3]), "AC5": _u16(*w[4]), "AC6": _u16(*w[5]),
            "B1": _s16(*w[6]), "B2": _s16(*w[7]),
            "MB": _s16(*w[8]), "MC": _s16(*w[9]), "MD": _s16(*w[10]),
        }

    def read_pressure_hpa(self) -> float:
        a = self.ADDRESS
        self.bus.write_byte_data(a, 0xF4, 0x2E)
        time.sleep(self.WAIT[0])
        msb, lsb = self.bus.read_i2c_block_data(a, 0xF6, 2)
        ut = (msb << 8) | lsb
        self.bus.write_byte_data(a, 0xF4, 0x34 + (self.oss << 6))
        time.sleep(self.WAIT[self.oss])
        msb, lsb, xlsb = self.bus.read_i2c_block_data(a, 0xF6, 3)
        up = ((msb << 16) | (lsb << 8) | xlsb) >> (8 - self.oss)
        _, p = bmp180_compensate(self.cal, ut, up, self.oss)
        hpa = p / 100.0
        if not 300 <= hpa <= 1100:
            raise RuntimeError(f"気圧 {hpa:.1f} hPa は測定範囲（300〜1100）の外")
        return hpa


# ====================================================================== BH1750
class BH1750:
    ADDRESSES = (0x23, 0x5C)

    def __init__(self, bus):
        self.bus = bus
        self.address = None
        for a in self.ADDRESSES:
            try:
                bus.write_byte(a, 0x01)        # Power On
                self.address = a
                break
            except OSError:
                continue
        if self.address is None:
            raise RuntimeError("0x23 / 0x5C に応答がありません")
        bus.write_byte(self.address, 0x10)     # 1 lx 単位で測り続けるモード
        time.sleep(0.18)

    def read_lux(self) -> float:
        from smbus2 import i2c_msg
        msg = i2c_msg.read(self.address, 2)
        self.bus.i2c_rdwr(msg)
        hi, lo = list(msg)
        return ((hi << 8) | lo) / 1.2


# ======================================================================= SCD40
def crc8(data) -> int:
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class SCD40:
    ADDRESS = 0x62

    def __init__(self, bus):
        self.bus = bus
        self.last = None            # (co2, 時刻)
        self.stop()
        self._words(0x3682, 3)      # シリアル番号。応答の確認
        self._cmd(0x21B1)           # 約5秒ごとに測り続ける

    def _cmd(self, cmd):
        from smbus2 import i2c_msg
        self.bus.i2c_rdwr(i2c_msg.write(self.ADDRESS, [cmd >> 8, cmd & 0xFF]))

    def _words(self, cmd, n):
        from smbus2 import i2c_msg
        self._cmd(cmd)
        time.sleep(0.001)
        msg = i2c_msg.read(self.ADDRESS, 3 * n)
        self.bus.i2c_rdwr(msg)
        data = list(msg)
        out = []
        for i in range(n):
            msb, lsb, c = data[3 * i: 3 * i + 3]
            if crc8([msb, lsb]) != c:
                raise RuntimeError("CRC が一致しません")
            out.append((msb << 8) | lsb)
        return out

    def stop(self):
        try:
            self._cmd(0x3F86)
        except OSError:
            pass
        time.sleep(0.5)

    def read_co2(self) -> float:
        if self._words(0xE4B8, 1)[0] & 0x07FF:
            co2, _, _ = self._words(0xEC05, 3)
            self.last = (float(co2), time.monotonic())
        if self.last is None:
            raise RuntimeError("まだ最初の測定が終わっていません")
        value, at = self.last
        if time.monotonic() - at > 120:
            raise RuntimeError("2分以上新しい値がありません")
        return value


# ================================================================== まとめ役
class _Slot:
    """センサー1つ分。失敗したら次の回に作り直す。ログは出しすぎない。"""

    LOG_EVERY_SEC = 1800

    def __init__(self, name: str, setup, read):
        self.name, self._setup, self._read = name, setup, read
        self.obj = None
        self._failing_since = None
        self._last_log = 0.0

    def get(self):
        try:
            if self.obj is None:
                self.obj = self._setup()
            value = self._read(self.obj)
        except Exception as e:  # センサーごとに止める。全体は止めない
            self.obj = None
            now = time.monotonic()
            if self._failing_since is None or now - self._last_log > self.LOG_EVERY_SEC:
                log.warning("%s: 読めません（%s）", self.name, e)
                self._last_log = now
            if self._failing_since is None:
                self._failing_since = now
            return None
        if self._failing_since is not None:
            log.info("%s: 読めるようになりました", self.name)
            self._failing_since = None
        return value


class SensorSet:
    """1回分の測定値を {sensor_key: 値} で返す。"""

    def __init__(self, settings, bus_factory=None):
        self.s = settings
        self._bus = None
        self._bus_factory = bus_factory
        self.slots: dict[str, _Slot] = {}

        def ds(sensor_id, label):
            def setup():
                if not sensor_id:
                    raise RuntimeError(
                        f"config.toml の {label} が空です。test_ds18b20.py --watch で ID を調べて書く"
                    )
                return sensor_id
            return setup

        self.slots["room_temp"] = _Slot("室温用DS18B20", ds(settings.room_ds18b20_id, "room_ds18b20_id"), read_ds18b20)
        self.slots["bed_temp"] = _Slot("布団用DS18B20", ds(settings.futon_ds18b20_id, "futon_ds18b20_id"), read_ds18b20)
        if settings.use_dht11:
            self.slots["humidity"] = _Slot("DHT11", find_dht11, read_dht11_humidity)
        if settings.use_bmp180:
            self.slots["pressure_hpa"] = _Slot("BMP180", lambda: BMP180(self.bus()), lambda o: o.read_pressure_hpa())
        if settings.use_bh1750:
            self.slots["light_lux"] = _Slot("BH1750", lambda: BH1750(self.bus()), lambda o: o.read_lux())
        if settings.use_scd40:
            self.slots["co2_ppm"] = _Slot("SCD40", lambda: SCD40(self.bus()), lambda o: o.read_co2())

    def bus(self):
        if self._bus is None:
            if self._bus_factory is not None:
                self._bus = self._bus_factory()
            else:
                from smbus2 import SMBus
                self._bus = SMBus(1)
        return self._bus

    def read_all(self) -> dict[str, float]:
        values = {}
        for key, slot in self.slots.items():
            v = slot.get()
            if v is not None:
                values[key] = v
        return values

    def close(self) -> None:
        scd = self.slots.get("co2_ppm")
        if scd is not None and scd.obj is not None:
            scd.obj.stop()
        if self._bus is not None:
            self._bus.close()
            self._bus = None
