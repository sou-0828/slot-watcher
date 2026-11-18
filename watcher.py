"""
slot-watcher: 空き枠チェック（骨組み）

公開リポジトリなので、ログ(print)にはサイト名・日時・受付番号などを出さない。
詳しい内容は Discord にだけ送る。

終了コード:
  0 = 正常（空きなし / 通知済み / 停止中）
  1 = 一時的なエラー（次回また試す）
  2 = 致命的エラー（ワークフローを自動で無効化する）
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
STATE_FILE = Path("state/seen.json")      # 通知済みの枠（ハッシュだけ保存）
ERROR_FILE = Path("state/errors.json")    # 連続エラー回数
MAX_CONSECUTIVE_ERRORS = 3
REQUEST_TIMEOUT = 20


# ---------------- 設定 ----------------

def env(name, default=""):
    return os.environ.get(name, default).strip()


TARGET_URL = env("TARGET_URL")
LOGIN_ID = env("LOGIN_ID")
LOGIN_PASSWORD = env("LOGIN_PASSWORD")
WEBHOOK_URL = env("DISCORD_WEBHOOK_URL")
TARGET_DAYS = [d for d in env("TARGET_DAYS").replace("、", ",").split(",") if d]  # 例: "土,日"
TARGET_DATES_RAW = env("TARGET_DATES")  # 例: "12/12, 12/13, 12/20~12/27"
MODE = env("MODE", "notify")  # notify のみ対応。auto は後で追加


def parse_dates(raw, today=None):
    """
    "12/12, 2026-12-13, 12/20~12/27" → {date, ...}
    年を省略したら、今日以降で一番近い日付として扱う。
    """
    today = today or datetime.now(JST).date()

    def one(s):
        s = s.strip().replace("-", "/")
        parts = [int(p) for p in s.split("/")]
        if len(parts) == 3:
            return datetime(parts[0], parts[1], parts[2]).date()
        m, d = parts
        cand = datetime(today.year, m, d).date()
        if cand < today:
            cand = datetime(today.year + 1, m, d).date()
        return cand

    result = set()
    for token in raw.replace("、", ",").replace("～", "~").split(","):
        token = token.strip()
        if not token:
            continue
        if "~" in token:
            a, b = (one(x) for x in token.split("~", 1))
            while a <= b:
                result.add(a)
                a += timedelta(days=1)
        else:
            result.add(one(token))
    return result


# ---------------- サーバー停止時間 ----------------
# 予約サイトの受付時間（ログイン画面の記載より）
#   月〜土: 6:00〜20:00, 21:00〜翌5:00  → 20:00〜21:00 は停止
#   日・祝: 6:00〜18:00, 19:30〜翌5:00  → 18:00〜19:30 は停止
#   毎日 5:00〜6:00 も停止

def is_sunday_or_holiday(d):
    if d.weekday() == 6:
        return True
    try:
        import jpholiday
        return jpholiday.is_holiday(d)
    except ImportError:
        return False


def in_maintenance(now):
    minutes = now.hour * 60 + now.minute
    if 5 * 60 <= minutes < 6 * 60:
        return True
    if is_sunday_or_holiday(now.date()):
        return 18 * 60 <= minutes < 19 * 60 + 30
    return 20 * 60 <= minutes < 21 * 60


# ---------------- Discord ----------------

def notify(text):
    """Discordに送る。失敗してもログには中身を出さない。"""
    if not WEBHOOK_URL:
        print("Webhook未設定")
        return False
    try:
        r = requests.post(WEBHOOK_URL, json={"content": text[:1900]}, timeout=REQUEST_TIMEOUT)
        return r.status_code in (200, 204)
    except requests.RequestException:
        print("Discord送信失敗")
        return False


def redact(text):
    """エラー文からURL・ID・パスワードを伏せる"""
    import re
    text = re.sub(r"https?://\S+", "[URL]", text)
    for secret in (LOGIN_ID, LOGIN_PASSWORD, WEBHOOK_URL, TARGET_URL):
        if secret:
            text = text.replace(secret, "[伏せ字]")
    return text


# ---------------- 状態の保存 ----------------

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def slot_key(slot):
    """枠をハッシュ化（キャッシュを誰かに見られても中身がわからないように）"""
    raw = f"{slot['date']}|{slot['period']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------- サイト操作（12月5日以降に実装） ----------------

class LoginFailed(Exception):
    """ログイン失敗。パスワードロック(累積5回)を防ぐため即停止する。"""


def login(session):
    """
    TODO(12/5以降): ログインページを開いて USER ID / PASSWORD を送信する。
    - リトライは絶対にしない
    - 失敗したら LoginFailed を投げる
    - ページ表示から3分以内に送信する
    """
    raise NotImplementedError("ログイン処理はまだ未実装")


def fetch_slots(session):
    """
    TODO(12/5以降): 予約画面を読み取って空き枠のリストを返す。
    戻り値の例: [{"date": "2026-12-12", "weekday": "土", "period": "3"}]

    自動予約を足すときの絶対ルール:
    - 翌教習日の枠は取らない（夜の予約操作は「当日予約」扱いでキャンセル不可、
      キャンセル料 5,100円/時限）。取るのは2日以上先の枠だけ
    - 土日プラスコースなので 18:00 以降の枠は予約できない → 除外
    """
    raise NotImplementedError("空き枠の読み取りはまだ未実装")


# ---------------- メイン ----------------

def run_check():
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    login(session)               # 失敗時は LoginFailed
    time.sleep(2)                # 連続アクセスを避ける
    slots = fetch_slots(session)

    if TARGET_DAYS:
        slots = [s for s in slots if s.get("weekday") in TARGET_DAYS]
    if TARGET_DATES_RAW:
        dates = {d.isoformat() for d in parse_dates(TARGET_DATES_RAW)}
        slots = [s for s in slots if s.get("date") in dates]

    seen = set(load_json(STATE_FILE, []))
    new_slots = [s for s in slots if slot_key(s) not in seen]

    notified = True
    if new_slots:
        lines = [f"・{s['date']}（{s['weekday']}）{s['period']}時限" for s in new_slots]
        notified = notify("🚗 空きが出ました\n" + "\n".join(lines) + f"\n{TARGET_URL}")
        print(f"新しい空き: {len(new_slots)}件 → " + ("通知済み" if notified else "通知失敗（次回また通知）"))
    else:
        print("新しい空きなし")

    # 今ある空きだけ覚えておく（埋まった枠は忘れる）
    # 通知に失敗した枠は覚えない → 次回もう一度通知される
    keep = [s for s in slots if notified or slot_key(s) in seen]
    save_json(STATE_FILE, [slot_key(s) for s in keep])


def main():
    # テスト通知モード（サイトには一切アクセスしない）
    if env("TEST_NOTIFY") == "true":
        ok = notify("✅ slot-watcher テスト通知です")
        print("テスト通知: " + ("成功" if ok else "失敗"))
        return 0 if ok else 1

    # 設定チェック（足りなければサイトにアクセスしない）
    missing = [n for n, v in [("TARGET_URL", TARGET_URL), ("LOGIN_ID", LOGIN_ID),
                              ("LOGIN_PASSWORD", LOGIN_PASSWORD), ("DISCORD_WEBHOOK_URL", WEBHOOK_URL)] if not v]
    if missing:
        print("設定が足りないのでスキップ: " + ", ".join(missing))
        return 0

    if TARGET_DATES_RAW:
        try:
            parse_dates(TARGET_DATES_RAW)
        except (ValueError, TypeError):
            notify("⛔ TARGET_DATES の書き方が間違っているので停止しました。\n"
                   "例: 12/12, 12/13, 12/20~12/27")
            print("TARGET_DATES の形式エラー → 停止")
            return 2

    if in_maintenance(datetime.now(JST)):
        print("サーバー停止時間なのでスキップ")
        return 0

    if MODE != "notify":
        print("MODEは notify のみ対応")
        return 2

    errors = load_json(ERROR_FILE, {"count": 0})

    try:
        run_check()
        save_json(ERROR_FILE, {"count": 0})
        return 0

    except LoginFailed:
        notify("⛔ ログインに失敗したので停止しました。\n"
               "パスワードを確認してから、GitHubのActionsで手動で再開してください。")
        print("ログイン失敗 → 停止")
        return 2

    except NotImplementedError:
        print("サイト操作が未実装のためスキップ")
        return 0

    except Exception as e:  # 通信エラーなど
        errors["count"] += 1
        save_json(ERROR_FILE, errors)
        print(f"エラー（{errors['count']}回連続）: {type(e).__name__}")  # 種類だけ
        if errors["count"] >= MAX_CONSECUTIVE_ERRORS:
            notify(f"⛔ エラーが{MAX_CONSECUTIVE_ERRORS}回続いたので停止しました。\n"
                   f"最後のエラー: {type(e).__name__}: {redact(str(e))[:300]}")
            return 2
        return 1


if __name__ == "__main__":
    print(f"実行: {datetime.now(JST):%Y-%m-%d %H:%M} JST")
    sys.exit(main())
