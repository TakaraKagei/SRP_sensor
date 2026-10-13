"""設定の読み込み。

- 動作の設定は config.toml（このフォルダ）に書く。
- 機械用の鍵（X-API-Key）は config.toml に書かない。次のどちらかで渡す。
    1. 環境変数 SRP_INGEST_KEY
    2. ~/.config/srp-agent/agent.env の  SRP_INGEST_KEY=...  の行
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.toml")
DEFAULT_ENV_FILE = os.path.expanduser("~/.config/srp-agent/agent.env")


@dataclass
class Settings:
    # 送り先
    base_url: str = ""
    api_key: str = ""
    device_id: str = "bedroom-pi"
    send_timeout_sec: float = 30.0
    # 測り方（2026-10-13 決定: 1分ごとに測り、10分ごとにまとめて送る。24時間）
    sample_interval_sec: int = 60
    bucket_minutes: int = 10
    # センサー
    room_ds18b20_id: str = ""
    futon_ds18b20_id: str = ""
    use_dht11: bool = True
    use_bmp180: bool = True
    use_bh1750: bool = True
    use_scd40: bool = True
    use_inmp441: bool = True
    # マイク
    mic_card: str = ""            # 空なら arecord -l から voicehat を探す
    mic_channel: str = "left"     # L/R を GND にしたので左
    noise_db_offset: float = 120.0
    noise_event_margin_db: float = 15.0
    # 送れなかったデータの置き場所
    data_dir: str = "~/.local/share/srp-agent"
    keep_days: int = 14

    @property
    def ingest_url(self) -> str:
        return self.base_url.rstrip("/") + "/api/ingest/env-buckets"

    @property
    def data_path(self) -> str:
        return os.path.expanduser(self.data_dir)


PLACEHOLDER_URL_PART = "xxxxxxxxxx"


def check_required(s: Settings) -> tuple[list[str], list[str]]:
    """動かす前に足りない設定を調べる。(止めるべきもの, 注意だけのもの)"""
    errors, warnings = [], []
    if not s.base_url or PLACEHOLDER_URL_PART in s.base_url:
        errors.append("config.toml の base_url が見本のままです。Cloud Run の URL（https://srp-…run.app）を書いてください")
    elif not s.base_url.startswith(("https://", "http://")):
        errors.append("config.toml の base_url は https:// で始まる URL にしてください")
    if not s.api_key:
        errors.append("鍵がありません。~/.config/srp-agent/agent.env に SRP_INGEST_KEY=… の1行を書いてください")
    if not s.room_ds18b20_id:
        warnings.append("config.toml の room_ds18b20_id が空です（室温を送れません）")
    if not s.futon_ds18b20_id:
        warnings.append("config.toml の futon_ds18b20_id が空です（布団の中の温度を送れません）")
    return errors, warnings


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def load(config_path: str | None = None, env_file: str | None = None) -> Settings:
    path = config_path or DEFAULT_CONFIG
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    s = Settings()
    sections = {
        "server": ("base_url", "device_id", "send_timeout_sec"),
        "sampling": ("sample_interval_sec", "bucket_minutes"),
        "sensors": ("room_ds18b20_id", "futon_ds18b20_id", "use_dht11", "use_bmp180",
                    "use_bh1750", "use_scd40", "use_inmp441"),
        "noise": ("mic_card", "mic_channel", "noise_db_offset", "noise_event_margin_db"),
        "storage": ("data_dir", "keep_days"),
    }
    for section, keys in sections.items():
        for k in keys:
            if k in raw.get(section, {}):
                setattr(s, k, raw[section][k])

    env = _read_env_file(env_file or DEFAULT_ENV_FILE)
    s.api_key = os.environ.get("SRP_INGEST_KEY") or env.get("SRP_INGEST_KEY", "")
    s.base_url = os.environ.get("SRP_BASE_URL") or env.get("SRP_BASE_URL") or s.base_url

    if s.mic_channel not in ("left", "right"):
        raise ValueError("noise.mic_channel は left か right にしてください")
    if 60 % s.sample_interval_sec != 0 and s.sample_interval_sec % 60 != 0:
        raise ValueError("sampling.sample_interval_sec は 60 の約数か倍数にしてください")
    if 60 % s.bucket_minutes != 0:
        raise ValueError("sampling.bucket_minutes は 60 の約数にしてください（サーバーは10分）")
    return s
