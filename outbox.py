"""送る前の10分値をファイルに残し、送れたら消す。

ネットが切れていても、ラズパイを再起動しても、送れていない分は残る。
つながったら古い順に送り直す。batch_id が同じなので、サーバーで二重にならない。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("srp.outbox")


class TemporaryError(Exception):
    """あとでもう一度送れば通るかもしれない失敗（ネット・サーバーの一時的な不調）。"""


class RejectedError(Exception):
    """何度送っても通らない失敗（データの形が違うなど）。"""


class AuthError(Exception):
    """鍵が違う。設定を直すまで送れない。"""


class HttpSender:
    def __init__(self, url: str, api_key: str, timeout: float = 30.0):
        self.url, self.api_key, self.timeout = url, api_key, timeout

    def send(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: 鍵（SRP_INGEST_KEY）を確認してください") from e
            if e.code in (400, 404, 413, 422):
                raise RejectedError(f"HTTP {e.code}: {detail}") from e
            raise TemporaryError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TemporaryError(str(e)) from e


class Outbox:
    def __init__(self, directory: str, keep_days: int = 14):
        self.dir = directory
        self.failed_dir = os.path.join(directory, "failed")
        self.keep_sec = keep_days * 86400
        os.makedirs(self.failed_dir, exist_ok=True)

    @staticmethod
    def _name(payload: dict) -> str:
        safe = payload["batch_id"].replace(":", "").replace("+", "p").replace("/", "_")
        return f"{safe}.json"

    def put(self, payload: dict) -> str:
        path = os.path.join(self.dir, self._name(payload))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)      # 書きかけのファイルが残らないように
        return path

    def pending(self) -> list[str]:
        names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
        return [os.path.join(self.dir, n) for n in names]

    def prune(self, now: float | None = None) -> int:
        """keep_days より古い未送信分は諦めて消す（SDカードがいっぱいにならないように）。"""
        now = now or time.time()
        removed = 0
        for d in (self.dir, self.failed_dir):
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isfile(p) and now - os.path.getmtime(p) > self.keep_sec:
                    os.remove(p)
                    removed += 1
        if removed:
            log.warning("古い未送信データを %d 件消しました", removed)
        return removed

    def flush(self, sender, limit: int = 200) -> dict:
        """古い順に送る。一時的な失敗が出たら、そこでやめて次の機会に回す。"""
        sent = rejected = 0
        for path in self.pending()[:limit]:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            try:
                sender.send(payload)
            except TemporaryError as e:
                log.warning("送信できません（%s）。あとで送り直します。未送信 %d 件",
                            e, len(self.pending()))
                break
            except AuthError as e:
                log.error("%s", e)
                break
            except RejectedError as e:
                log.error("サーバーが受け付けませんでした（%s）。failed/ に移します", e)
                os.replace(path, os.path.join(self.failed_dir, os.path.basename(path)))
                rejected += 1
                continue
            os.remove(path)
            sent += 1
        return {"sent": sent, "rejected": rejected, "pending": len(self.pending())}
