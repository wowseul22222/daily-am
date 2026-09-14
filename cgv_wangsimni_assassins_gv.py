import os
import sys
import json
import time
import re
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass

KST = ZoneInfo("Asia/Seoul")

CO_CD = "A420"
SITE_NO = "0074"
SITE_NAME = "CGV 왕십리"
API_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
RTCTL_SCOP_CD = "08"

# 정확히 이 한 회차만 감시
TARGET_DATE = "20260926"
TARGET_TIME = "1810"
TARGET_SCREEN_KEYWORD = "7관"
TARGET_MOVIE_KEYWORD = "암살자들"
TARGET_VIDEO_CODE = "0023"  # GV

# 한 날짜/한 회차 전용 감시.
# 평소 10초, 매시 :58~:02 / :28~:32 구간은 5초.
NORMAL_POLL_SECONDS = 10.0
FAST_POLL_SECONDS = 5.0
SUMMARY_SECONDS = 600.0
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "120"))
START_DELAY = float(os.environ.get("START_DELAY", "0"))

CGV_CUST_NO = os.environ.get("CGV_CUST_NO", "").strip()
CY_WEBHOOK = os.environ.get("CY_WEBHOOK", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_MENTION_ID", "").strip()

STATE_FILE = "cgv_wangsimni_assassins_gv_state.json"

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
}


def now_kst():
    return datetime.now(KST)


def clean(value):
    return " ".join(str(value or "").split())


def digits(value):
    return re.sub(r"\D", "", clean(value))


def normalize_video_code(value):
    value = clean(value)
    return value.zfill(4) if value.isdigit() else value


def make_headers():
    q = urlencode({
        "siteNo": SITE_NO,
        "siteNm": SITE_NAME,
        "scnYmd": TARGET_DATE,
    })
    h = dict(BASE_HEADERS)
    h["Referer"] = f"https://cgv.co.kr/cnm/movieBook/cinema?{q}"
    return h


def pretty_date(value):
    dt = datetime.strptime(value, "%Y%m%d")
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} ({weekdays[dt.weekday()]})"


def pretty_time(value):
    s = digits(value)
    if len(s) == 4:
        return f"{s[:2]}:{s[2:]}"
    return clean(value)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"alerted": False}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"alerted": False}
    except Exception:
        return {"alerted": False}


def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def booking_url(row):
    params = {
        "movNo": clean(row.get("movNo")),
        "scnYmd": TARGET_DATE,
        "siteNo": SITE_NO,
        "scnsNo": clean(row.get("scnsNo")),
        "siteNm": SITE_NAME,
        "scnSseq": clean(row.get("scnSseq")),
    }
    return "https://cgv.co.kr/cnm/movieBook/movie?" + urlencode(params)


def send_discord(row):
    if not CY_WEBHOOK:
        print("❌ CY_WEBHOOK missing")
        return False

    movie = clean(row.get("movNm")) or clean(row.get("expoProdNm")) or TARGET_MOVIE_KEYWORD
    expo = clean(row.get("expoProdNm"))
    screen = clean(row.get("expoScnsNm")) or clean(row.get("siteScnsNm"))
    start = pretty_time(row.get("scnsrtTm"))
    end = pretty_time(row.get("scnendTm"))
    free = clean(row.get("frSeatCnt"))
    url = booking_url(row)

    title = expo or f"{movie}(GV)"
    seat_line = f"\n🎫 현재 잔여좌석: {free}석" if free != "" else ""

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    message = (
        f"{mention}🚨 **왕십리 목표 GV 회차 등록 감지**\n"
        f"🎬 **{title}**\n"
        f"📅 {pretty_date(TARGET_DATE)}\n"
        f"🕕 **{start}-{end} · {screen}**"
        f"{seat_line}\n"
        f"🎟️ {url}"
    )

    payload = {
        "content": message,
        "flags": 4,
    }
    if DISCORD_USER_ID:
        payload["allowed_mentions"] = {"users": [DISCORD_USER_ID]}

    try:
        r = requests.post(CY_WEBHOOK, json=payload, timeout=15)
        r.raise_for_status()
        print("✅ DISCORD SENT:", r.status_code)
        return True
    except Exception as e:
        print("❌ DISCORD ERROR:", repr(e))
        return False


def fetch_rows(session):
    params = {
        "coCd": CO_CD,
        "siteNo": SITE_NO,
        "scnYmd": TARGET_DATE,
        "scnsNo": "",
        "scnSseq": "",
        "rtctlScopCd": RTCTL_SCOP_CD,
        "custNo": CGV_CUST_NO,
    }

    r = session.get(
        API_URL,
        params=params,
        headers=make_headers(),
        timeout=15,
    )

    if r.status_code == 429:
        print("⚠️ HTTP 429 - 60초 대기")
        time.sleep(60)
        return None

    r.raise_for_status()
    data = r.json()

    if not isinstance(data, dict) or data.get("statusCode") not in (0, "0", None):
        raise RuntimeError(f"CGV statusCode={data.get('statusCode')} message={data.get('statusMessage')}")

    rows = data.get("data")
    return [x for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []


def is_target(row):
    if clean(row.get("siteNo")) not in ("", SITE_NO):
        return False

    if clean(row.get("scnYmd")) not in ("", TARGET_DATE):
        return False

    if digits(row.get("scnsrtTm")) != TARGET_TIME:
        return False

    if normalize_video_code(row.get("videoAddexpCd")) != TARGET_VIDEO_CODE:
        return False

    movie_text = " ".join([
        clean(row.get("movNm")),
        clean(row.get("expoProdNm")),
    ])
    if TARGET_MOVIE_KEYWORD not in movie_text:
        return False

    screen_text = " ".join([
        clean(row.get("expoScnsNm")),
        clean(row.get("siteScnsNm")),
    ])
    if TARGET_SCREEN_KEYWORD not in screen_text:
        return False

    return True


def scan_once(session):
    rows = fetch_rows(session)
    if rows is None:
        return None, 0

    hits = [row for row in rows if is_target(row)]
    return (hits[0] if hits else None), len(rows)


def current_poll_seconds(now):
    """
    평소에는 10초.
    오픈 가능성이 높은 정각/30분 전후 2분 구간은 5초.
      :58, :59, :00, :01, :02
      :28, :29, :30, :31, :32
    """
    if now.minute in {58, 59, 0, 1, 2, 28, 29, 30, 31, 32}:
        return FAST_POLL_SECONDS
    return NORMAL_POLL_SECONDS


def main():
    print("=" * 100)
    print("CGV WANGSIMNI SINGLE GV WATCH")
    print("TARGET:", TARGET_MOVIE_KEYWORD, pretty_date(TARGET_DATE), pretty_time(TARGET_TIME), TARGET_SCREEN_KEYWORD)
    print("MATCH: videoAddexpCd=0023 + exact date/time + 7관 + movie keyword")
    print("POLL: normal 10s / :58~:02 & :28~:32 = 5s / exact :00,:30 forced scan")
    print("SITE:", SITE_NO, SITE_NAME)
    print("CUST_NO:", "SET" if CGV_CUST_NO else "MISSING")
    print("WEBHOOK:", "SET" if CY_WEBHOOK else "MISSING")
    print("=" * 100)

    if not CGV_CUST_NO:
        print("❌ CGV_CUST_NO missing")
        sys.exit(2)

    if not CY_WEBHOOK:
        print("❌ CY_WEBHOOK missing")
        sys.exit(2)

    if START_DELAY > 0:
        time.sleep(START_DELAY)

    state = load_state()
    if state.get("alerted"):
        print("✅ 이미 목표 회차 알림을 보낸 상태입니다. 종료합니다.")
        return

    # 목표일이 이미 지나갔으면 더 이상 감시하지 않는다.
    if now_kst().strftime("%Y%m%d") > TARGET_DATE:
        print("ℹ️ 목표 날짜가 지나 감시를 종료합니다.")
        return

    session = requests.Session()
    started = time.monotonic()
    last_summary = started
    next_regular = started
    last_forced_key = None
    scans = 0
    errors = 0

    while time.monotonic() - started < RUN_SECONDS:
        now = now_kst()
        mono = time.monotonic()

        forced_key = None
        if now.minute in (0, 30) and now.second < 8:
            forced_key = now.strftime("%Y%m%d%H%M")

        due_regular = mono >= next_regular
        due_forced = forced_key is not None and forced_key != last_forced_key

        if due_regular or due_forced:
            if due_forced:
                print(f"⚡ {now.strftime('%H:%M:%S')} 정각/30분 강제 조회")
                last_forced_key = forced_key

            try:
                row, total = scan_once(session)
                scans += 1

                if row:
                    print("🚨 TARGET FOUND")
                    print(json.dumps(row, ensure_ascii=False, indent=2))
                    if send_discord(row):
                        state = {
                            "alerted": True,
                            "alertedAtKst": now_kst().isoformat(),
                            "targetDate": TARGET_DATE,
                            "targetTime": TARGET_TIME,
                            "targetScreen": TARGET_SCREEN_KEYWORD,
                            "movie": TARGET_MOVIE_KEYWORD,
                            "movNo": clean(row.get("movNo")),
                            "prodNo": clean(row.get("prodNo")),
                            "scnsNo": clean(row.get("scnsNo")),
                            "scnSseq": clean(row.get("scnSseq")),
                        }
                        save_state(state)
                        print("✅ 목표 회차 감지/알림/상태저장 완료. 감시 종료.")
                        return
                else:
                    print(
                        f"🔎 NOT FOUND | {now.strftime('%H:%M:%S')} KST | "
                        f"rows={total} | target={TARGET_DATE} {TARGET_TIME} {TARGET_SCREEN_KEYWORD}"
                    )

            except Exception as e:
                errors += 1
                print("❌ SCAN ERROR:", repr(e))

            poll_seconds = current_poll_seconds(now_kst())
            next_regular = time.monotonic() + poll_seconds

        if mono - last_summary >= SUMMARY_SECONDS:
            print(
                f"💚 감시중 | 최근 누적 조회 {scans}회 | 오류 {errors} | "
                f"목표 {pretty_date(TARGET_DATE)} {pretty_time(TARGET_TIME)} {TARGET_SCREEN_KEYWORD}"
            )
            last_summary = mono

        # 바쁜 루프 방지. :00/:30 진입도 놓치지 않을 정도로 짧게.
        time.sleep(0.5)

    print("RUN_SECONDS 종료")


if __name__ == "__main__":
    main()
