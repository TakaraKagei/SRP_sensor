# 寝室センサーの送信プログラム（本番用）

作成: 2026-10-13 ／ 動作確認用の `raspi/sensor_test` とは別のフォルダです。

ラズパイにつないだ7個のセンサーの値を、24時間測り続け、10分ごとにまとめて Google Cloud（Cloud Run の SRP）へ送ります。
センサーの読み方・使う GPIO は `sensor_test` と同じです（config.txt の設定もそのまま使います）。

---

## 1. 何をどのくらいの頻度で送るか（2026-10-13 決定）

| 量 | センサー | 測る頻度 | 送るもの（10分ごと） |
|---|---|---|---|
| 室温 | 室温用DS18B20 | 1分ごと | 平均・最小・最大・回数 |
| 布団の中の温度 | 布団用DS18B20 | 1分ごと | 〃 |
| 湿度 | DHT11 | 1分ごと | 〃 |
| 気圧 | BMP180 | 1分ごと | 〃 |
| CO₂ | SCD40（約5秒ごとに測り続け、1分ごとに最新値を取る） | 1分ごと | 〃 |
| 明るさ | BH1750 | 1分ごと | 〃 |
| 騒音 | INMP441 | 1秒ごと | 平均・最小・最大・Leq（エネルギー平均）・物音の回数と一覧 |

- **24時間** 測ります。何時から何時が睡眠かは、サーバーが Apple Watch の就寝・起床の時刻で後から切り出します。
- **音声そのものは保存も送信もしません。** 1秒ごとに「音の大きさ」の数字にしたら捨てます。1秒ごとの数字も送りません（10分の要約だけ）。
- 音の大きさは「dBFS ＋ 120」の **目安の値** です。120 は INMP441 の公称感度から逆算した数で、校正していないので騒音計の値とは一致しません。日ごとの比べ合いにだけ使います。
- **物音** は「直近5分の中央値（その部屋のふだんの音）より 15dB 以上大きい音のかたまり」です。
- DHT11・BMP180・SCD40 の温度は使いません（1つの量は1つのセンサーから。室温は室温用DS18B20）。
- 送れなかった分はラズパイの中にファイルで残り、ネットがつながったら古い順に送り直します（14日より古い分は消します）。

---

## 2. フォルダの中身

| ファイル | 役割 |
|---|---|
| `agent.py` | 本体。1分ごとに測り、10分ごとにまとめて送る。`--check`・`--dry-run` もここ |
| `sensors.py` | DS18B20・DHT11・BMP180・BH1750・SCD40 の読み取り（`sensor_test` と同じ読み方） |
| `noise.py` | マイクの録音を流し続け、1秒ごとの音の大きさと物音を出す |
| `aggregate.py` | 1分・1秒の値を10分ごとにまとめる |
| `outbox.py` | 送る前の10分値をファイルに残し、送れたら消す。サーバーへの送信 |
| `settings.py` | 設定の読み込み |
| `config.example.toml` | 設定の見本。`config.toml` にコピーして使う |
| `srp-agent.service` | ラズパイの起動と同時に動かすための設定（systemd） |
| `tests/test_agent.py` | センサーもネットも使わずに動きを確かめるテスト |

---

## 3. 始める前に

- [ ] `sensor_test` の7つのテストと `test_all.py` で、全センサーの値が出ている
- [ ] **サーバー（Cloud Run）を新しい版に更新した。** 今回、ラズパイからの10分値を受け取る口（`/api/ingest/env-buckets`）をサーバーに足しました。Mac で `deploy/deploy.sh` を実行してデプロイし直してください（手順は `docs/60_deploy.md`）
- [ ] Mac で次の2つを調べてメモした
  ```bash
  gcloud run services describe srp --region asia-northeast1 --format='value(status.url)'   # 送り先の URL
  gcloud secrets versions access latest --secret=srp-ingest-key                           # 機械用の鍵
  ```
  鍵は他人に見せない・チャットや Git に貼らないでください。

---

## 4. ラズパイに置く

**Mac のターミナルで**（SRP フォルダの `raspi/srp_agent` を、ラズパイの `~/Projects/srp_agent` にコピー）:

```bash
scp -r ~/Projects/SRP/raspi/srp_agent takara@takaraspberry.local:~/Projects/
```

**ラズパイのターミナルで**（このプログラム専用の仮想環境を作る）:

```bash
cd ~/Projects/srp_agent
python3 -m venv .venv
.venv/bin/pip install smbus2
.venv/bin/python --version          # Python 3.13.x と出れば OK
```

---

## 5. 設定する

`config.toml` は Git に入っていません（ラズパイごとの設定なので `.gitignore` で除外）。
`git clone` / `git pull` したあと、**最初の1回だけ** 見本からコピーして作ります。作ったあとの `git pull` で消えたり上書きされたりはしません。

**必須の設定**

| 置き場所 | 項目 | 必須か | 空・見本のままだと |
|---|---|---|---|
| `config.toml` | `base_url` | **必須** | 起動しない（`base_url が見本のままです` と出る） |
| `~/.config/srp-agent/agent.env` | `SRP_INGEST_KEY` | **必須** | 起動しない（`鍵がありません` と出る） |
| `config.toml` | `room_ds18b20_id`・`futon_ds18b20_id` | 強く推奨 | 起動はするが、室温・布団の中の温度を送れない（警告が出る） |
| `config.toml` | そのほか | 任意 | 見本の値のままでよい |

**① config.toml を作る**

```bash
cd ~/Projects/srp_agent
cp config.example.toml config.toml
nano config.toml
```

書き換えるのは次の3つです（ほかはそのままでよい）。

| 項目 | 書く値 |
|---|---|
| `base_url` | 3. で調べた Cloud Run の URL（`https://srp-…run.app`。最後の `/` は付けない） |
| `room_ds18b20_id` | 室温用DS18B20 の ID（`sensor_test` の `python test_ds18b20.py --watch` で温めて調べたもの） |
| `futon_ds18b20_id` | 布団用DS18B20 の ID |

保存は `Ctrl+O` → `Enter`、終了は `Ctrl+X`。

**② 鍵を置く**（config.toml とは別の、自分だけが読めるファイルに）

```bash
mkdir -p ~/.config/srp-agent
nano ~/.config/srp-agent/agent.env
```

中身は1行だけ（`ここに鍵` を 3. で調べた鍵に置き換える）:

```text
SRP_INGEST_KEY=ここに鍵
```

保存したら、自分以外が読めないようにします。

```bash
chmod 600 ~/.config/srp-agent/agent.env
```

---

## 6. 確かめる

**`sensor_test` のスクリプトを止めてから** 行ってください（SCD40 とマイクを同時に2つのプログラムで使えないため）。

**① 設置の確認**

```bash
cd ~/Projects/srp_agent
.venv/bin/python agent.py --check
```

正常なら次のように出ます。

```text
== センサー ==
  見つかった DS18B20: 28-00000000e711, 28-00000025c86f
  room_temp          22.31
  bed_temp           22.87
  humidity           55.00
  pressure_hpa     1008.42
  light_lux         312.50
  co2_ppm           615.00
== マイク（3秒） ==
  38.2  37.9  38.4  （音の大きさ。校正していない目安）
== サーバー ==
  OK: https://srp-…run.app/api/ingest/env-buckets に鍵が通りました（何も保存していません）

結果: すべて OK
```

**② 送らずに動かしてみる（任意・10分以上かかる）**

```bash
.venv/bin/python agent.py --dry-run
```

1分ごとに測り、10分の区切り（:00、:10、:20…）を過ぎると、送る予定の内容が画面に出ます。`Ctrl+C` で止めます。

---

## 7. 自動で動かす（本番）

```bash
cd ~/Projects/srp_agent
sudo cp srp-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now srp-agent
```

ユーザー名が `takara` ではない、またはフォルダが `~/Projects/srp_agent` ではない場合は、コピーする前に `srp-agent.service` の `User=` と `/home/takara/…` を書き換えてください。

**動いているかを見る**

```bash
systemctl status srp-agent          # active (running) なら動いている
journalctl -u srp-agent -f          # ログを流して見る（Ctrl+C で抜ける）
```

10分の区切りから数秒たつと、ログに `送信 1 件（未送信 0 件）` と出ます。そのあと、SRP の画面で今日（または明日）の日付を開くと、寝室の値のグラフが出ます。

**よく使う操作**

| やりたいこと | コマンド |
|---|---|
| 止める（sensor_test を使うときなど） | `sudo systemctl stop srp-agent` |
| また動かす | `sudo systemctl start srp-agent` |
| 設定を変えたあと | `sudo systemctl restart srp-agent` |
| 起動時に動かないようにする | `sudo systemctl disable --now srp-agent` |

止めたり再起動したりすると、**測りかけの10分ぶんは送られません**（次の10分から再開）。

---

## 8. うまくいかないとき

| 症状（ログ・表示） | 確認すること |
|---|---|
| `config.toml がありません` | 5.① をやり直す（表示されるコマンドをそのまま実行すればよい） |
| `config.toml の base_url が見本のままです` | `config.toml` の `base_url` を Cloud Run の URL に書き換える |
| `鍵がありません` | 5.② をやり直す |
| `base_url（config.toml）か SRP_INGEST_KEY … が空です` | config.toml の `base_url`、`~/.config/srp-agent/agent.env` の中身 |
| `HTTP 401: 鍵（SRP_INGEST_KEY）を確認してください` | 鍵の貼り間違い（前後の空白・改行）。Mac でもう一度取り出す |
| `HTTP 404` | サーバーが古い版のまま。3. のデプロイをやり直す |
| `送信できません（…）。あとで送り直します` | ネットが切れている、またはサーバーの一時的な不調。つながれば自動で送り直す |
| `室温用DS18B20: 読めません（config.toml の room_ds18b20_id が空です…）` | config.toml に ID を書く |
| `室温用DS18B20: 読めません（No such file …）` | ID の書き間違い、または配線。`ls /sys/bus/w1/devices/` で `28-` を確認 |
| `SCD40: 読めません（…）`・`マイク: …` が出続ける | `sensor_test` を同時に動かしていないか。止めてから `sudo systemctl restart srp-agent` |
| `マイク: 音が全部0です` | INMP441 の SD・VDD・L/R の配線（`sensor_test` のテスト7） |
| `サーバーが受け付けませんでした（HTTP 400 …）` | そのデータは `~/.local/share/srp-agent/failed/` に移されています。表示された内容を添えて相談 |
| 時刻が大きくずれている | `timedatectl` で `System clock synchronized: yes` を確認。ネットにつながれば自動で合う |

1つのセンサーが読めなくても、ほかのセンサーの値は送り続けます。読めないセンサーのログは30分に1回だけ出します（読めるようになると `読めるようになりました` と出ます）。

---

## 9. テスト（任意）

センサーもネットも使わずに、まとめ方・物音の見つけ方・送り直し・送る形を確かめます。Mac でもラズパイでも動きます（Python 3.11 以上）。

```bash
cd ~/Projects/srp_agent
.venv/bin/python -m unittest discover -s tests -t .
# または
cd ~/Projects/srp_agent/tests && ../.venv/bin/python test_agent.py
```

最後に `OK` と出れば成功です。

- **実機のセンサーは使いません。** I2C の装置（`/dev/i2c-1`）にも触りません。ラズパイのように smbus2 が入っている環境では、本物の smbus2 の「メッセージ」の形を使って、偽物のセンサーとやりとりします（読み方が本物の smbus2 と合っているかまで確かめられる）。
- **ネットも使いません。** 送信のテストは、ラズパイの中だけで動く小さなサーバーを相手にします。オフラインのときの「あとで送り直す」動きも、偽物の送り先で確かめています。
- テストが通っても、**実機のセンサーが読めることの確認にはなりません。** 実機の確認は 6.① の `--check` で行います。

---

## 10. サーバー側で変えたこと（参考）

- 受け口 `POST /api/ingest/env-buckets` を追加（`backend/app/api/routes.py`）。認証は `X-API-Key`。
- 10分値は「どの日のページに入れるか」を `env_days_for()` で決める（`backend/app/services/env_service.py`）。
  ページ D は「前日 4:00 〜 当日 10:00」の値を持つので、朝 4:00〜10:00 の値は2つのページに入る。
- 同じ `batch_id`（`<device_id>-<10分の区切りの時刻>`）の再送は無視する。物音は開始時刻で重複を除く。
- 睡眠がすでに入っている日は、届くたびに睡眠中の要約を作り直す。
- 「物音がした回数」は、**睡眠中（就寝〜起床）の物音だけ** を数えるように変えた（24時間送るため）。
- テストは `backend/tests/test_env_buckets.py`。
