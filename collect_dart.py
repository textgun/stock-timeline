# DART 공시에서 투자 일정을 뽑아 events.json 을 만드는 수집기

"""
주식 일정 타임라인 수집기.

    python3 collect_dart.py --selftest                  API 키 없이 규칙 점검
    python3 collect_dart.py --days 180                  수집만
    python3 collect_dart.py --days 180 --inject index.html
    python3 collect_dart.py --brief 14                  D-14 이내 일정 텍스트

세 갈래에서 일정을 모은다.

  1. 법정 제출기한   API 없이 계산. 분기·반기 45일, 사업보고서 90일.
  2. DART 공시       워치리스트 종목의 공시 목록 → 보고서명으로 분류 →
                     원문에서 실제 일정일을 추출.
  3. 고정 일정       학회·거래소 일정. static_events.json 에서 손으로 관리.

확인하지 못한 날짜는 채우지 않는다. 접수일(rcept_dt)밖에 모르는 공시는
접수일 이벤트로만 남기고 estimated 를 붙이지 않는다.
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API = "https://opendart.fss.or.kr/api"
UA = {"User-Agent": "stock-timeline/1.0"}


def log(msg):
    print(msg, file=sys.stderr)


# ══════════════════════════════════════════════════════════════════
#  API 키 · 워치리스트 · 기업코드
# ══════════════════════════════════════════════════════════════════

# 키는 파일 밖으로 내보내지 않는다. 세 곳을 순서대로 본다.
#   1. 환경변수 DART_API_KEY      — CI·일회성 실행용
#   2. 이 폴더의 .env             — 평소 쓰는 자리 (.gitignore 에 있다)
#   3. ../stock_analyzer/.env     — 같은 워크스페이스의 기존 설정 재사용
# .env 는 절대 커밋하지 않는다. .env.example 을 복사해 쓴다.
ENV_CANDIDATES = [
    os.path.join(HERE, ".env"),
    os.path.join(HERE, "..", "stock_analyzer", ".env"),
]


def load_key():
    if os.environ.get("DART_API_KEY"):
        return os.environ["DART_API_KEY"]
    for path in ENV_CANDIDATES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*DART_API_KEY\s*=\s*["\']?([^"\'\s#]+)', line)
                if m:
                    key = m.group(1)
                    log(f"키 로드 — {os.path.relpath(path, HERE)} "
                        f"({key[:4]}…{key[-2:]}, {len(key)}자)")
                    return key
    return None


NO_KEY = """DART_API_KEY 를 찾지 못했다. 셋 중 하나로 넣는다.

  1. 이 폴더에 .env 를 만든다  (권장 — .gitignore 에 있어 커밋되지 않는다)
       cp .env.example .env
       그리고 DART_API_KEY= 뒤에 키를 붙여넣는다

  2. 환경변수로 한 번만
       DART_API_KEY=... python3 collect_dart.py --days 180

  3. ../stock_analyzer/.env 에 이미 있으면 그대로 쓴다

키 발급은 https://opendart.fss.or.kr (무료).
키 없이도 법정기한·만기일·고정 일정은 만들어지지만 공시 일정은 비어 있게 된다."""


def load_watchlist():
    """워치리스트 종목코드 목록. 로컬 파일이 우선, 없으면 stock_analyzer 재사용."""
    local = os.path.join(HERE, "watchlist.json")
    if os.path.exists(local):
        wl = json.load(open(local, encoding="utf-8"))
        tickers = wl.get("tickers", [])
        log(f"워치리스트 — watchlist.json {len(tickers)}종목")
        return tickers, wl.get("notes", {})

    tickers, notes = [], {}
    for name in ("active_universe.json", "user_picks.json"):
        path = os.path.join(HERE, "..", "stock_analyzer", "data", name)
        if not os.path.exists(path):
            continue
        d = json.load(open(path, encoding="utf-8"))
        for t in d.get("tickers", []):
            if t not in tickers:
                tickers.append(t)
        notes.update(d.get("notes", {}))
    log(f"워치리스트 — stock_analyzer 폴백 {len(tickers)}종목")
    return tickers, notes


def corp_map(key):
    """종목코드 → {corp_code, name}. 캐시 → 로컬 XML → DART 다운로드 순."""
    cache = os.path.join(DATA, "corp_map.json")
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))

    xml = None
    local = os.path.join(HERE, "..", "stock_analyzer", "data", "corp_codes.xml")
    if os.path.exists(local):
        log("기업코드 — 로컬 corp_codes.xml 재사용")
        xml = open(local, encoding="utf-8").read()
    elif key:
        log("기업코드 — DART 에서 corpCode.xml 내려받는 중 (30MB)")
        req = urllib.request.Request(f"{API}/corpCode.xml?crtfc_key={key}", headers=UA)
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=120).read()))
        xml = z.read(z.namelist()[0]).decode("utf-8", "ignore")
    else:
        return {}

    out = {}
    for block in re.finditer(r"<list>(.*?)</list>", xml, re.S):
        b = block.group(1)
        sc = (re.search(r"<stock_code>(.*?)</stock_code>", b, re.S) or [None, ""])[1].strip()
        if not sc:
            continue
        out[sc] = {
            "corp_code": re.search(r"<corp_code>(.*?)</corp_code>", b, re.S).group(1).strip(),
            "name": re.search(r"<corp_name>(.*?)</corp_name>", b, re.S).group(1).strip(),
        }
    os.makedirs(DATA, exist_ok=True)
    json.dump(out, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"기업코드 — 상장사 {len(out)}건 캐시")
    return out


# ══════════════════════════════════════════════════════════════════
#  영업일 · 법정 제출기한
# ══════════════════════════════════════════════════════════════════

# KRX 휴장일. stock_analyzer/market_calendar.py 의 검증된 표를 옮겨 왔다.
# 음력 명절은 매년 달라지므로 아래 HOLIDAY_YEARS 밖의 연도는 계산 결과를
# estimated 로 내보낸다 — 그럴듯한 날짜를 확정처럼 넣지 않기 위해서다.
HOLIDAYS = {
    "2026-01-01", "2026-01-28", "2026-01-29", "2026-01-30", "2026-03-01",
    "2026-05-01", "2026-05-05", "2026-05-25", "2026-06-06", "2026-08-17",
    "2026-09-24", "2026-09-25", "2026-09-26", "2026-10-03", "2026-10-09",
    "2026-12-25", "2026-12-31",
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-03-01",
    "2025-05-01", "2025-05-05", "2025-05-06", "2025-06-06", "2025-08-15",
    "2025-10-03", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    "2025-10-09", "2025-12-25", "2025-12-31",
}
HOLIDAY_YEARS = {2025, 2026}


# NYSE 휴장일. https://www.nyse.com/markets/hours-calendars 에서 확인했다.
# 만기일이 휴장일과 겹치면 앞당겨지므로 (2026-06-19 준틴스 = 6월 셋째 금요일) 필요하다.
US_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
US_HOLIDAY_YEARS = {2026, 2027}


def is_business_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def next_business_day(d):
    """d 가 휴일이면 다음 영업일로 민다. 법정기한의 익영업일 규칙."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d, holidays=None):
    """d 가 휴일이면 직전 영업일로 당긴다. 만기일 규칙."""
    holidays = HOLIDAYS if holidays is None else holidays
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= timedelta(days=1)
    return d


def nth_weekday(year, month, weekday, n):
    """그 달의 n번째 weekday. weekday 는 0=월 … 6=일."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


# (보고서 이름, 기준 분기 종료월, 제출 기한 일수)
PERIODIC = [
    ("1분기보고서", 3, 45),
    ("반기보고서", 6, 45),
    ("3분기보고서", 9, 45),
    ("사업보고서", 12, 90),
]


def statutory_events(start, end):
    """12월 결산 법인의 정기보고서 제출기한. API 호출이 필요 없다."""
    out = []
    for year in range(start.year - 1, end.year + 2):
        for label, month, span in PERIODIC:
            period_end = date(year, month, 1) + timedelta(days=32)
            period_end = date(period_end.year, period_end.month, 1) - timedelta(days=1)
            raw = period_end + timedelta(days=span)
            due = next_business_day(raw)
            if not (start <= due <= end):
                continue

            shifted = due != raw
            unknown = due.year not in HOLIDAY_YEARS
            note = f"{'사업연도' if span == 90 else '분기'} 종료 후 {span}일 이내."
            if shifted:
                note += f" 법정 만기 {raw.isoformat()}가 휴일이라 익영업일 {due.isoformat()}이 실제 마감이다."
            if unknown:
                note += f" {due.year}년 휴장일 표가 아직 없어 대체공휴일로 하루 더 밀릴 수 있다."

            out.append({
                "id": f"stat-{year}-{month}",
                "cat": "disc",
                "title": f"{year}년 {label} 제출기한 (12월 결산)",
                "org": "금융감독원 / DART",
                "tickers": [],
                "start": due.isoformat(),
                "end": due.isoformat(),
                "place": "전자공시",
                "verified": True,
                "estimated": unknown,
                "watch": False,
                "note": note,
                "src": "https://dart.fss.or.kr/",
            })
    return out


# ══════════════════════════════════════════════════════════════════
#  파생상품 만기 — 규칙이 확정되어 있어 API 없이 계산한다
# ══════════════════════════════════════════════════════════════════

# 만기 규칙. (지역, 몇 번째, 요일, 분기월 제목, 그 외 달 제목 또는 None, 휴장일 표)
#   한국  코스피200 선·옵 최종거래일 = 매월 두 번째 목요일
#   미국  쿼드러플 위칭            = 3·6·9·12월 세 번째 금요일
#   일본  메이저 SQ                = 3·6·9·12월 두 번째 금요일
QUARTER_MONTHS = (3, 6, 9, 12)


def expiry_events(start, end):
    out = []
    for year in range(start.year, end.year + 1):
        for month in range(1, 13):
            q = month in QUARTER_MONTHS

            # ── 한국 ──
            raw = nth_weekday(year, month, 3, 2)
            day = prev_business_day(raw, HOLIDAYS)
            unknown = year not in HOLIDAY_YEARS
            note = "코스피200 선물·옵션 최종거래일은 매월 두 번째 목요일이다."
            if day != raw:
                note += f" {raw.isoformat()}이 휴장일이라 직전 거래일로 당겨졌다."
            if unknown:
                note += f" {year}년 KRX 휴장일 표가 아직 없어 휴장일과 겹치면 하루 당겨질 수 있다."
            out.append(_mk(
                f"exp-kr-{year}-{month:02d}", day,
                "선물·옵션 동시만기" if q else "코스피200 옵션만기",
                "한국거래소", "KRX",
                note + (" 「네 마녀의 날」. 지수·개별주식 선물과 옵션 네 종류가 한꺼번에 만기를 맞아"
                        " 프로그램 매매 청산으로 장 막판 변동성이 커진다." if q else ""),
                not unknown, unknown, "https://www.krx.co.kr/"))

            if not q:
                continue

            # ── 미국 ──
            raw = nth_weekday(year, month, 4, 3)
            day = prev_business_day(raw, US_HOLIDAYS)
            unknown = year not in US_HOLIDAY_YEARS
            note = "미국 지수·개별주식 선물과 옵션이 동시에 만기를 맞는 날. 3·6·9·12월 세 번째 금요일이다."
            if day != raw:
                note += f" {raw.isoformat()}이 NYSE 휴장일이라 직전 거래일로 당겨졌다."
            if unknown:
                note += f" {year}년 NYSE 휴장일 표가 없어 휴장일과 겹치면 당겨질 수 있다."
            out.append(_mk(
                f"exp-us-{year}-{month:02d}", day, "미국 쿼드러플 위칭",
                "NYSE / CME", "미국",
                note + " S&P500 등 주요 지수 정기변경도 이 날 효력이 발생하는 경우가 많다.",
                not unknown, unknown, "https://www.nyse.com/markets/hours-calendars"))

            # ── 일본 ──
            # 일본 휴장일 표가 없다. 둘째 금요일이 공휴일이면 조정되므로 예상으로 둔다.
            day = nth_weekday(year, month, 4, 2)
            out.append(_mk(
                f"exp-jp-{year}-{month:02d}", day, "일본 메이저 SQ",
                "오사카거래소", "일본",
                "닛케이225 선물·옵션 SQ 산출일. 3·6·9·12월 두 번째 금요일이다. "
                "일본 휴장일 표가 이 프로젝트에 없어 공휴일과 겹치면 조정될 수 있으므로 예상으로 표시한다.",
                False, True, "https://www.jpx.co.jp/"))
    return [e for e in out if start <= date.fromisoformat(e["start"]) <= end]


def _mk(eid, day, title, org, place, note, _verified, estimated, src):
    return {
        "id": eid, "cat": "market", "title": title, "org": org,
        "tickers": [], "start": day.isoformat(), "end": day.isoformat(),
        "place": place, "verified": True, "estimated": estimated,
        "watch": False, "note": note, "src": src,
    }


# ══════════════════════════════════════════════════════════════════
#  보고서명 분류
# ══════════════════════════════════════════════════════════════════

# 개인 지분 신고처럼 일정이 아닌 공시. 워치리스트 종목이라도 버린다.
NOISE = [
    "임원ㆍ주요주주특정증권등소유상황보고서",
    "임원·주요주주특정증권등소유상황보고서",
    "주식등의대량보유상황보고서",
    "최대주주등소유주식변동신고서",
    "대규모기업집단현황공시",
    "동일인등출자계열회사",
    "지급수단별",
    "특수관계인",
    "기업지배구조보고서",
    "의결권대리행사권유참고서류",
    "임원ㆍ주요주주특정증권등거래계획보고서",
    "증권신고서(채무증권)",
    "발행조건확정",
]

# (보고서명 부분일치, 카테고리, 원문 파서 kind, 화면 제목)
# kind 가 None 이면 접수일만 이벤트로 남긴다.
RULES = [
    ("결산실적공시예고",           "ir",   "preview",   "실적발표 예정 공시"),
    ("기업설명회(IR)개최",          "ir",   "ir",        "기업설명회(IR)"),
    ("영업(잠정)실적",             "ir",   None,        "잠정실적 공시"),
    ("매출액또는손익구조",          "ir",   None,        "손익구조 변동 공시"),
    ("영업실적등에관한전망",         "ir",   None,        "실적 전망 공시"),

    ("주주총회소집공고",            "disc", "agm_pub",   "주주총회"),
    ("주주총회소집결의",            "disc", "agm_res",   "주주총회 소집결의"),
    ("정기주주총회결과",            "disc", None,        "정기주주총회 결과"),
    ("주주명부폐쇄기간또는기준일설정", "disc", "record",   "주주명부 기준일"),
    ("감사보고서제출",              "disc", None,        "감사보고서 제출"),
    ("사업보고서",                 "disc", None,        "사업보고서 제출"),
    ("반기보고서",                 "disc", None,        "반기보고서 제출"),
    ("분기보고서",                 "disc", None,        "분기보고서 제출"),

    ("현금ㆍ현물배당을위한주주명부폐쇄", "cap", "record",  "배당 기준일 설정"),
    ("현금ㆍ현물배당결정",           "cap",  "dividend",  "현금배당"),
    ("주요사항보고서(유상증자결정)",   "cap",  "rights",    "유상증자"),
    ("주요사항보고서(무상증자결정)",   "cap",  "bonus",     "무상증자"),
    ("주요사항보고서(자기주식취득결정)", "cap", "treasury",  "자기주식 취득"),
    ("주요사항보고서(자기주식처분결정)", "cap", None,       "자기주식 처분"),
    ("자기주식취득결과보고서",        "cap",  None,        "자기주식 취득 결과"),
    ("자기주식처분결과보고서",        "cap",  None,        "자기주식 처분 결과"),
    ("주식소각결정",                "cap",  None,        "주식 소각"),
    ("전환사채권발행결정",           "cap",  "rights",    "전환사채 발행"),
    ("신주인수권부사채권발행결정",     "cap",  "rights",    "신주인수권부사채 발행"),
    ("증권신고서(지분증권)",         "cap",  None,        "증권신고서(지분증권)"),

    ("투자판단관련주요경영사항",       "conf", None,        "투자판단 주요경영사항"),
    ("단일판매ㆍ공급계약체결",        "partner", None,     "공급계약 체결"),
    ("단일판매·공급계약체결",         "partner", None,     "공급계약 체결"),
    ("신규시설투자등",              "partner", None,      "신규 시설투자"),
    ("타법인주식및출자증권취득결정",   "partner", None,      "타법인 지분 취득"),
]


def classify(report_nm):
    """보고서명 → (cat, kind, 제목). 해당 없으면 None."""
    nm = re.sub(r"\s+", "", report_nm)
    nm = nm.replace("[기재정정]", "").replace("[첨부정정]", "").replace("[첨부추가]", "")
    for pat in NOISE:
        if pat in nm:
            return None
    for pat, cat, kind, title in RULES:
        if re.sub(r"\s+", "", pat) in nm:
            return cat, kind, title
    return None


# ══════════════════════════════════════════════════════════════════
#  공시 원문 파싱
# ══════════════════════════════════════════════════════════════════

DATE_RE = r"(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})"

# 공시가 알리는 일정은 접수일 근처에 있다. 한참 벗어나면 원문 파싱이 어긋난 것이다.
# 산문 속 라벨을 잡거나 과거 연혁 표를 집으면 이 창 밖으로 떨어진다.
SANE_BACK, SANE_FWD = 540, 1100


def sane(when, rcept_dt, why):
    lo = date.fromisoformat(rcept_dt) - timedelta(days=SANE_BACK)
    hi = date.fromisoformat(rcept_dt) + timedelta(days=SANE_FWD)
    if lo <= date.fromisoformat(when) <= hi:
        return True
    log(f"  날짜 버림 {why} → {when} (접수 {rcept_dt} 에서 너무 멀다 — 원문 파싱 어긋남)")
    return False


def _norm(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def date_after(text, *labels, window=90):
    """라벨 뒤 window 글자 안에서 첫 날짜를 찾는다. 값이 '-' 면 없는 것으로 본다."""
    for label in labels:
        for m in re.finditer(re.escape(label), text):
            seg = text[m.end(): m.end() + window]
            cut = re.search(r"\s\d{1,2}\.\s", seg[2:])   # 다음 번호 항목에서 자른다
            if cut:
                seg = seg[: cut.end() + 2]
            hit = re.search(DATE_RE, seg)
            if hit:
                return _norm(*hit.groups())
    return None


def text_after(text, *labels, window=60):
    """라벨 뒤 짧은 문구. 날짜·시각이 섞인 후보는 다른 필드를 잡은 것이라 버린다."""
    for label in labels:
        for m in re.finditer(re.escape(label), text):
            seg = text[m.end(): m.end() + window].strip(" :·-")
            seg = re.split(r"\s+\d+\.\s", seg)[0].strip(" :·-")
            if not seg or re.search(DATE_RE + r"|\d{1,2}:\d{2}", seg):
                continue
            return seg[:60]
    return None


# kind → [(chainStep, [라벨 후보...], 카테고리 접미 제목)]
# 실제 공시 원문에서 확인한 라벨만 넣는다.
DOC_FIELDS = {
    "ir":       [("개최", ["시작시간 종료시간", "행사일", "1. 일시"], "개최")],
    "dividend": [("기준일", ["배당기준일"], "배당 기준일"),
                 ("지급일", ["배당금지급 예정일자", "배당금지급예정일자"], "배당금 지급")],
    "agm_pub":  [("개최", ["1. 일시", "일시 :", "일시:"], "개최")],
    "agm_res":  [("개최", ["1. 일시 날짜", "1. 일시"], "개최"),
                 ("기준일", ["의결권행사기준일"], "의결권 기준일")],
    "record":   [("기준일", ["1. 기준일", "기준일"], "기준일")],
    "rights":   [("청약", ["청약기일", "청약일"], "청약"),
                 ("납입", ["납입일"], "납입"),
                 ("상장", ["신주의 상장 예정일", "신주의상장예정일", "상장 예정일"], "신주 상장")],
    "bonus":    [("기준일", ["신주배정기준일"], "신주배정 기준일"),
                 ("상장", ["신주의 상장 예정일", "신주의상장예정일"], "신주 상장")],

    "preview":  [("발표", ["결산실적 공시예정일", "공시예정일", "공시 예정일"], "실적 발표")],
}


# 한 이벤트가 구간인 것들. kind → (시작 라벨, 종료 라벨, 제목 접미)
DOC_SPANS = {
    "treasury": (["취득예상기간 시작일", "시작일"], ["취득예상기간 종료일", "종료일"], "예정기간"),
}


def fetch_document(key, rcept_no, timeout=40):
    """공시 원문을 태그 없는 한 줄 텍스트로. 실패하면 None 을 돌려주고 죽지 않는다."""
    try:
        req = urllib.request.Request(
            f"{API}/document.xml?crtfc_key={key}&rcept_no={rcept_no}", headers=UA)
        blob = urllib.request.urlopen(req, timeout=timeout).read()
        z = zipfile.ZipFile(io.BytesIO(blob))
        raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
        raw = re.sub(r"<STYLE.*?</STYLE>", " ", raw, flags=re.S | re.I)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
    except Exception as exc:                       # 서식이 바뀌면 여기로 떨어진다
        log(f"  원문 실패 {rcept_no} — {exc}")
        return None


def derive(kind, text, base):
    """원문에서 실제 일정일을 뽑아 파생 이벤트를 만든다. 못 뽑으면 빈 목록."""
    if not text:
        return []

    if kind in DOC_SPANS:
        lo_labels, hi_labels, suffix = DOC_SPANS[kind]
        lo = date_after(text, *lo_labels)
        hi = date_after(text, *hi_labels)
        if not lo or not sane(lo, base["start"], f"{base['id']}/{kind}"):
            return []
        if hi and not sane(hi, base["start"], f"{base['id']}/{kind} 종료"):
            hi = None
        ev = dict(base)
        ev["id"] = f"{base['id']}-span"
        ev["title"] = f"{base['org']} {base.get('kind_title', base['title'])} {suffix}"
        ev["start"] = lo
        ev["end"] = hi if hi and hi >= lo else lo
        ev["note"] = (f"{base['report_nm']} 원문에서 추출. "
                      f"접수 {base['start']} · 접수번호 {base['rcept']}.")
        return [ev]

    fields = DOC_FIELDS.get(kind)
    if not fields:
        return []
    place = text_after(text, "2. 장소", "장소")
    out = []
    for step, labels, suffix in fields:
        when = date_after(text, *labels)
        if not when or not sane(when, base["start"], f"{base['id']}/{step}"):
            continue
        ev = dict(base)
        ev["id"] = f"{base['id']}-{step}"
        ev["title"] = f"{base['org']} {base.get('kind_title', base['title'])} {suffix}"
        ev["start"] = ev["end"] = when
        ev["chain"] = base["id"]
        ev["chainStep"] = step
        ev["note"] = (f"{base['report_nm']} 원문에서 추출. "
                      f"접수 {base['start']} · 접수번호 {base['rcept']}.")
        if place and kind in ("ir", "agm_pub", "agm_res"):
            ev["place"] = place
        out.append(ev)
    return out


# ══════════════════════════════════════════════════════════════════
#  수집
# ══════════════════════════════════════════════════════════════════

MAX_PAGES = 30          # 삼성전자가 180일에 929건(10쪽). 넉넉히 잡되 무한루프는 막는다


def api_list(key, corp_code, bgn, end, page=1):
    url = (f"{API}/list.json?crtfc_key={key}&corp_code={corp_code}"
           f"&bgn_de={bgn}&end_de={end}&page_count=100&page_no={page}")
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))


def api_list_all(key, corp_code, bgn, end):
    """공시 목록 전체. 목록 API 는 100건씩 끊어 주므로 끝까지 넘겨야 한다.

    1쪽만 읽으면 공시가 많은 종목에서 조용히 대량 누락된다 — 삼성전자의
    1·2분기 잠정실적은 9쪽에 있었다. 최신순이라 1쪽만 봐도 「최근 것은 다 있는데」
    처럼 보여서 알아채기 어렵다.
    """
    first = api_list(key, corp_code, bgn, end)
    if first.get("status") != "000":
        return first, 1
    pages = min(int(first.get("total_page", 1) or 1), MAX_PAGES)
    items = list(first.get("list", []))
    for pg in range(2, pages + 1):
        time.sleep(0.2)
        nxt = api_list(key, corp_code, bgn, end, page=pg)
        if nxt.get("status") != "000":
            log(f"  {pg}쪽 실패 status {nxt.get('status')} — 이 종목은 일부만 수집됐다")
            break
        items.extend(nxt.get("list", []))
    if int(first.get("total_page", 1) or 1) > MAX_PAGES:
        log(f"  쪽수가 {first['total_page']} 로 MAX_PAGES({MAX_PAGES}) 를 넘는다 — 뒤쪽이 잘렸다")
    first["list"] = items
    return first, pages


def collect(key, days, use_doc):
    tickers, notes = load_watchlist()
    cmap = corp_map(key)
    today = date.today()
    bgn = (today - timedelta(days=days)).strftime("%Y%m%d")
    end = (today + timedelta(days=1)).strftime("%Y%m%d")

    events, missing = [], []
    for i, ticker in enumerate(tickers, 1):
        info = cmap.get(ticker)
        if not info:
            missing.append(ticker)
            continue
        try:
            res, pages = api_list_all(key, info["corp_code"], bgn, end)
        except Exception as exc:
            log(f"[{i}/{len(tickers)}] {ticker} 목록 실패 — {exc}")
            missing.append(ticker)
            continue
        if res.get("status") != "000":
            if res.get("status") != "013":         # 013 = 조회 결과 없음
                log(f"[{i}/{len(tickers)}] {ticker} status {res.get('status')} {res.get('message')}")
            continue

        kept = 0
        for it in res.get("list", []):
            hit = classify(it["report_nm"])
            if not hit:
                continue
            cat, kind, title = hit
            rcept_dt = datetime.strptime(it["rcept_dt"], "%Y%m%d").date().isoformat()
            base = {
                "id": f"d{it['rcept_no']}",
                "cat": cat,
                "title": f"{info['name']} {title}" if title.endswith("공시")
                         else f"{info['name']} {title} 공시",
                "org": info["name"],
                "tickers": [ticker],
                "start": rcept_dt,
                "end": rcept_dt,
                "place": "DART",
                "verified": True,
                "estimated": False,
                "watch": True,
                "kind_title": title,
                "note": f"{it['report_nm'].strip()} · 접수번호 {it['rcept_no']}.",
                "src": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}",
                "rcept": it["rcept_no"],
                "report_nm": it["report_nm"].strip(),
            }
            events.append(base)
            kept += 1

            if use_doc and kind:
                text = fetch_document(key, it["rcept_no"])
                events.extend(derive(kind, text, base))
                time.sleep(0.35)                   # DART 분당 호출 제한 회피
        log(f"[{i}/{len(tickers)}] {info['name']} — 공시 {res.get('total_count')}건"
            f"({pages}쪽) 중 {kept}건 채택")
        time.sleep(0.25)

    if missing:
        log(f"기업코드를 못 찾았거나 조회 실패 — {', '.join(missing)}")
    for ev in events:
        ev.pop("report_nm", None)
        ev.pop("kind_title", None)
    return events, tickers, missing


def load_static():
    path = os.path.join(HERE, "static_events.json")
    if not os.path.exists(path):
        return []
    out = []
    for ev in json.load(open(path, encoding="utf-8")):
        ev["static"] = True          # --load 가 통째로 갈아끼울 수 있게 표시해 둔다
        out.append(ev)
    return out


def dedupe(events):
    """같은 일정을 한 번만 남긴다.

    세 겹으로 걸러진다.
      1. 같은 id
      2. 파생 일정이 만들어진 접수 이벤트는 접수일 마커를 버린다
      3. [기재정정]·자회사 중복공시가 같은 날 같은 제목을 만들면 하나로 접는다
    """
    by_id = {}
    for ev in events:
        by_id[ev["id"]] = ev

    chained = {ev["chain"] for ev in by_id.values() if ev.get("chain")}
    kept = [ev for ev in by_id.values()
            if not (ev["id"] in chained and not ev.get("chain"))]

    seen, out = {}, []
    # 사람이 손댄 항목을 먼저 세워 둔다. 중복이 접힐 때 살아남는 쪽이 되도록.
    for ev in sorted(kept, key=lambda e: (not (e.get("edited") or e.get("manual")), e["id"])):
        sig = (ev["org"], ev["start"], ev["end"], ev["title"])
        if sig in seen:
            seen[sig].setdefault("dupes", 0)
            seen[sig]["dupes"] += 1
            continue
        seen[sig] = ev
        out.append(ev)
    for ev in out:
        if ev.get("dupes"):
            ev["note"] += f" 같은 일정의 정정·중복 공시 {ev['dupes']}건은 접었다."
            ev.pop("dupes")
    return sorted(out, key=lambda e: (e["start"], e["org"], e["title"]))


# ══════════════════════════════════════════════════════════════════
#  손질 — 자동 수집이 못 맞히는 것을 사람이 고친다
# ══════════════════════════════════════════════════════════════════

OVERRIDES = os.path.join(HERE, "overrides.json")

# 손으로 고칠 수 있는 필드. 여기 없는 키는 오타로 보고 막는다.
EDITABLE = {"start", "end", "title", "org", "place", "note", "src",
            "cat", "estimated", "verified", "watch", "tickers"}
DATE_FIELDS = {"start", "end"}
BOOL_FIELDS = {"estimated", "verified", "watch"}


def load_overrides():
    if not os.path.exists(OVERRIDES):
        return {"fix": {}, "hide": [], "add": []}
    d = json.load(open(OVERRIDES, encoding="utf-8"))
    d.setdefault("fix", {})
    d.setdefault("hide", [])
    d.setdefault("add", [])
    return d


def save_overrides(d):
    json.dump(d, open(OVERRIDES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def parse_kv(pairs):
    """`start=2026-09-15 estimated=false tickers=005930,000660` 를 딕셔너리로."""
    out = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"key=value 형식이 아니다 — {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        if k not in EDITABLE:
            raise SystemExit(f"고칠 수 없는 필드 — {k}. 가능한 것: {', '.join(sorted(EDITABLE))}")
        if k in BOOL_FIELDS:
            if v.lower() not in ("true", "false"):
                raise SystemExit(f"{k} 는 true/false 여야 한다 — {v}")
            out[k] = v.lower() == "true"
        elif k == "tickers":
            out[k] = [t.strip() for t in v.split(",") if t.strip()]
        else:
            if k in DATE_FIELDS:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise SystemExit(f"{k} 는 YYYY-MM-DD 여야 한다 — {v}")
            out[k] = v
    return out


def apply_overrides(events):
    """수집 결과 위에 손질을 얹는다.

    되돌릴 수 있어야 하므로 덮어쓰기 전 값을 `_orig` 에 보관한다.
    overrides.json 에서 항목을 지우면 다시 수집하지 않아도 원래 값으로 돌아간다.
    숨긴 일정은 지우지 않고 `hidden` 을 달아 둔다 — 화면에서만 빠지고 데이터는 남는다.
    """
    ov = load_overrides()
    hide, fix = set(ov["hide"]), ov["fix"]

    # 직접 추가한 일정은 overrides.json 의 add 가 유일한 출처다.
    # 이전 실행에서 붙은 것을 먼저 걷어내야 add 에서 지웠을 때 화면에서도 사라진다.
    events[:] = [e for e in events if not e.get("manual")]
    known = {e["id"] for e in events}

    for ev in events:
        prev = ev.pop("_orig", None)          # 지난번 손질을 먼저 되돌린다
        if prev:
            for k, v in prev.items():
                if v is None:
                    ev.pop(k, None)
                else:
                    ev[k] = v
            ev.pop("edited", None)
        ev.pop("hidden", None)

        if ev["id"] in hide:
            ev["hidden"] = True
        patch = fix.get(ev["id"])
        if patch:
            ev["_orig"] = {k: ev.get(k) for k in patch}
            ev.update(patch)
            ev["edited"] = True

    for i, item in enumerate(ov["add"]):
        ev = dict(item)
        ev.setdefault("id", f"manual-{i+1}")
        ev.setdefault("end", ev.get("start"))
        ev.setdefault("org", "직접 입력")
        ev.setdefault("cat", "ir")
        ev.setdefault("place", "—")
        ev.setdefault("tickers", [])
        ev.setdefault("verified", True)
        ev.setdefault("estimated", False)
        ev.setdefault("watch", False)
        ev.setdefault("note", "손으로 추가한 일정.")
        ev["manual"] = True
        if ev["id"] in hide:
            ev["hidden"] = True
        events.append(ev)

    missing = [i for i in list(fix) + list(hide) if i not in known]
    if missing:
        log(f"손질 대상이 수집 결과에 없다 — {', '.join(missing)} "
            f"(수집 기간 밖이거나 id 가 바뀌었을 수 있다)")
    return events


def cmd_fix(eid, pairs):
    ov = load_overrides()
    patch = ov["fix"].setdefault(eid, {})
    patch.update(parse_kv(pairs))
    save_overrides(ov)
    print(f"고침 등록 — {eid}")
    for k, v in patch.items():
        print(f"  {k} = {v}")
    print("\n반영하려면 python3 collect_dart.py --load --inject index.html")


def cmd_unfix(eid):
    ov = load_overrides()
    gone = ov["fix"].pop(eid, None) is not None
    if eid in ov["hide"]:
        ov["hide"].remove(eid); gone = True
    save_overrides(ov)
    print(f"{'되돌림' if gone else '걸려 있는 손질이 없다'} — {eid}")


def cmd_hide(eid):
    ov = load_overrides()
    if eid not in ov["hide"]:
        ov["hide"].append(eid)
    save_overrides(ov)
    print(f"숨김 — {eid} (데이터는 남고 화면에서만 빠진다)")


def cmd_add(pairs):
    item = parse_kv(pairs)
    for req in ("start", "title", "cat"):
        if req not in item:
            raise SystemExit(f"--add 에는 최소한 start, title, cat 이 필요하다 (빠진 것: {req})")
    ov = load_overrides()
    item["id"] = f"manual-{len(ov['add'])+1}"
    ov["add"].append(item)
    save_overrides(ov)
    print(f"추가 — {item['id']} · {item['start']} · {item['title']}")
    print("\n반영하려면 python3 collect_dart.py --load --inject index.html")


def cmd_list():
    ov = load_overrides()
    if not (ov["fix"] or ov["hide"] or ov["add"]):
        print("걸려 있는 손질이 없다.")
        return
    if ov["fix"]:
        print(f"■ 고침 {len(ov['fix'])}건")
        for eid, patch in ov["fix"].items():
            print(f"  {eid}")
            for k, v in patch.items():
                print(f"      {k} = {v}")
    if ov["hide"]:
        print(f"■ 숨김 {len(ov['hide'])}건")
        for eid in ov["hide"]:
            print(f"  {eid}")
    if ov["add"]:
        print(f"■ 추가 {len(ov['add'])}건")
        for item in ov["add"]:
            print(f"  {item['id']}  {item['start']}  {item['title']}")


# ══════════════════════════════════════════════════════════════════
#  출력
# ══════════════════════════════════════════════════════════════════

def inject(html_path, events):
    """HTML 안의 EVENTS 배열을 통째로 갈아끼운다."""
    src = open(html_path, encoding="utf-8").read()
    body = json.dumps(events, ensure_ascii=False, indent=1)
    new = f"const EVENTS = {body};"
    out, n = re.subn(r"const EVENTS = \[.*?\n\];", new, src, count=1, flags=re.S)
    if not n:
        log(f"주입 실패 — {html_path} 에서 EVENTS 배열을 못 찾았다")
        return False
    open(html_path, "w", encoding="utf-8").write(out)
    log(f"주입 완료 — {html_path} ({len(events)}건)")
    return True


WD = "월화수목금토일"


def brief(events, within):
    today = date.today()
    rows = []
    for ev in events:
        d = date.fromisoformat(ev["start"])
        dd = (d - today).days
        if 0 <= dd <= within:
            rows.append((dd, d, ev))
    rows.sort(key=lambda r: (r[0], not r[2].get("watch")))
    if not rows:
        return f"D-{within} 이내 예정 일정이 없습니다."
    lines = [f"■ D-{within} 이내 일정 {len(rows)}건 (기준 {today.isoformat()})"]
    for dd, d, ev in rows:
        mark = "★" if ev.get("watch") else "·"
        tag = " [예상]" if ev.get("estimated") else ""
        lines.append(f"{mark} D-{dd:<3} {d.isoformat()}({WD[d.weekday()]})  {ev['title']}{tag}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  셀프테스트
# ══════════════════════════════════════════════════════════════════

# 실제 DART 에서 관측한 보고서명. 새 분류 규칙을 넣었으면 여기에 한 줄 추가한다.
SAMPLES = [
    ("기업설명회(IR)개최", "ir", "ir"),
    ("기업설명회(IR)개최(안내공시)", "ir", "ir"),
    ("결산실적공시예고(안내공시)", "ir", "preview"),
    ("연결재무제표기준영업(잠정)실적(공정공시)", "ir", None),
    ("[기재정정]연결재무제표기준영업(잠정)실적(공정공시)", "ir", None),
    ("현금ㆍ현물배당결정", "cap", "dividend"),
    ("현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정", "cap", "record"),
    ("주요사항보고서(유상증자결정)", "cap", "rights"),
    ("주요사항보고서(무상증자결정)", "cap", "bonus"),
    ("주요사항보고서(자기주식취득결정)", "cap", "treasury"),
    ("주주총회소집공고", "disc", "agm_pub"),
    ("주주총회소집결의", "disc", "agm_res"),
    ("주주명부폐쇄기간또는기준일설정", "disc", "record"),
    ("사업보고서 (2025.12)", "disc", None),
    ("반기보고서 (2026.06)", "disc", None),
    ("단일판매ㆍ공급계약체결", "partner", None),
    ("증권신고서(지분증권)", "cap", None),
    ("투자판단관련주요경영사항              (CTP51 한국 품목허가 신청)", "conf", None),
    # 일정이 아닌 공시는 반드시 걸러져야 한다
    ("임원ㆍ주요주주특정증권등소유상황보고서", None, None),
    ("주식등의대량보유상황보고서(일반)", None, None),
    ("대규모기업집단현황공시[분기별공시(개별회사용)]", None, None),
    ("의결권대리행사권유참고서류", None, None),
    ("증권신고서(채무증권)", None, None),
    ("[발행조건확정]증권신고서(채무증권)", None, None),
]

# 실제 공시 원문에서 잘라 온 조각. 서식이 바뀌면 여기서 먼저 깨진다.
DOC_SAMPLES = [
    ("dividend",
     "현금ㆍ현물배당 결정 1. 배당구분 분기배당 6. 배당기준일 2026-06-30 "
     "7. 배당금지급 예정일자 2026-08-28 8. 주주총회 개최여부 미개최",
     {"기준일": "2026-06-30", "지급일": "2026-08-28"}),
    ("ir",
     "기업설명회(IR) 개최 1. 일시 행사일 시간(현지시간) 시작일 종료일 시작시간 "
     "종료시간 2026-06-17 2026-06-17 09:00 12:00 2. 장소 그랜드 하얏트 호텔",
     {"개최": "2026-06-17"}),
    ("agm_pub",
     "제18기 정기주주총회를 아래와 같이 개최하고자 하오니 - 아 래 - "
     "1. 일시: 2026년 3월 31일(화) 오전 9시 2. 장소: 대전컨벤션센터",
     {"개최": "2026-03-31"}),
    ("record",
     "주주명부폐쇄기간 또는 기준일 설정 1. 기준일 2026-09-23 "
     "2. 명의개서정지기간 시작일 2026-09-28 종료일 2026-10-02",
     {"기준일": "2026-09-23"}),
    ("preview",
     "결산실적공시 예고 1. 회사명 주식회사 펄어비스 2. 결산대상기간 시작일 "
     "2026-01-01 종료일 2026-03-31 3. 결산실적 공시예정일 2026-05-12 "
     "4. 기타 투자판단에 참고할 사항 -",
     {"발표": "2026-05-12"}),
    ("treasury",
     "자기주식 취득 결정 1. 취득예정주식(주) 보통주식 67,226 2. 취득예정금액(원) "
     "40,000,000,000 3. 취득예상기간 시작일 2026.03.26 종료일 2026.06.25 "
     "4. 보유예상기간 시작일 - 종료일 - 5. 취득목적 주주가치 제고",
     {"__span__": ("2026-03-26", "2026-06-25")}),
    ("ir",
     "기업설명회(IR) 개최(안내공시) 1. 일시 및 장소 일시 2026-03-06 13:00 "
     "장소 서울 신라호텔 2. 참가 대상자 해외 기관투자자",
     {"개최": "2026-03-06"}),
    # 철회·정정으로 값이 전부 '-' 인 공시에서 옆 필드 날짜를 잘못 집어오면 안 된다
    ("rights",
     "유상증자 결정 9. 납입일 - 10. 신주의 배당기산일 - 11. 신주권교부예정일 - "
     "12. 신주의 상장 예정일 - 15. 이사회결의일(결정일) 2026-08-26",
     {}),
]


def selftest():
    fails = []

    for nm, want_cat, want_kind in SAMPLES:
        got = classify(nm)
        if want_cat is None:
            if got is not None:
                fails.append(f"걸러져야 할 보고서가 분류됨 — {nm} → {got}")
            continue
        if got is None:
            fails.append(f"분류 안 됨 — {nm}")
        elif (got[0], got[1]) != (want_cat, want_kind):
            fails.append(f"분류 불일치 — {nm} → {got[0]}/{got[1]}, 기대 {want_cat}/{want_kind}")
    print(f"분류 규칙 {len(SAMPLES)}건 검사")

    for kind, text, want in DOC_SAMPLES:
        base = {"id": "t", "cat": "cap", "title": "테스트사 테스트 공시", "org": "테스트사",
                "kind_title": "테스트", "start": "2026-06-01", "rcept": "0",
                "report_nm": "테스트", "tickers": [], "place": "DART"}
        made = derive(kind, text, base)
        if "__span__" in want:
            lo, hi = want["__span__"]
            if not made or (made[0]["start"], made[0]["end"]) != (lo, hi):
                fails.append(f"구간 파싱 불일치 — {kind} → "
                             f"{(made[0]['start'], made[0]['end']) if made else None}, 기대 {(lo, hi)}")
            continue
        got = {e["chainStep"]: e["start"] for e in made}
        if not want and got:
            fails.append(f"원문 파싱 과잉 — {kind} 에서 값이 없어야 하는데 {got}")
        for step, when in want.items():
            if got.get(step) != when:
                fails.append(f"원문 파싱 불일치 — {kind}/{step} → {got.get(step)}, 기대 {when}")
    print(f"원문 파서 {len(DOC_SAMPLES)}건 검사")

    # 산문 속 라벨을 잡아 엉뚱한 과거 날짜를 집던 실제 사례 (SK하이닉스 증권신고서)
    prose = ("본 증권신고서 작성기준일 현재 신주의 모집수량, 모집가액, 청약기일 및 납입기일 등 "
             "주요 발행조건이 확정되지 아니하였으며 … 2023-02-01 자 정정신고서 참조")
    b = {"id": "t2", "cat": "cap", "title": "x", "org": "A", "kind_title": "유상증자",
         "start": "2026-06-24", "rcept": "0", "report_nm": "증권신고서",
         "tickers": [], "place": "DART"}
    if derive("rights", prose, b):
        fails.append("접수일에서 3년 이상 떨어진 날짜가 걸러지지 않았다")
    # 반대로 정상 범위는 통과해야 한다 (2026-03 공시가 알리는 2025-12-31 배당 기준일)
    b2 = dict(b, id="t3", start="2026-03-10")
    if not derive("record", "1. 기준일 2025-12-31 2. 명의개서정지기간", b2):
        fails.append("정상 범위의 과거 기준일까지 걸러졌다")
    print("파싱 어긋남 방어 검사")

    # 제목에 「공시」가 두 번 붙지 않아야 한다
    for _, _, _, t in RULES:
        made = f"삼성전자 {t}" if t.endswith("공시") else f"삼성전자 {t} 공시"
        if made.count("공시 공시"):
            fails.append(f"제목에 공시가 두 번 — {made}")
    print(f"제목 조립 {len(RULES)}건 검사")

    # 법정기한. 만기가 휴일이면 익영업일로 밀려야 한다.
    cases = [
        ("2026-11-16", "2026 3분기 — 11/14 토요일 → 11/16 월요일"),
        ("2026-08-14", "2026 반기 — 8/14 금요일 당일"),
        ("2026-05-15", "2026 1분기 — 5/15 금요일 당일"),
        ("2026-03-31", "2025 사업보고서 — 3/31 화요일 당일"),
    ]
    got = {e["start"] for e in statutory_events(date(2025, 1, 1), date(2028, 12, 31))}
    for when, why in cases:
        if when not in got:
            fails.append(f"법정기한 누락 — {when} ({why})")
    print(f"법정기한 {len(cases)}건 검사")

    # 만기일 — 규칙 계산과 휴장일 보정
    exp = {e["id"]: e for e in expiry_events(date(2026, 1, 1), date(2027, 12, 31))}
    exp_cases = [
        ("exp-kr-2026-09", "2026-09-10", False, "9월 둘째 목요일 — 네 마녀의 날"),
        ("exp-kr-2026-12", "2026-12-10", False, "12월 둘째 목요일"),
        ("exp-us-2026-06", "2026-06-18", False, "6/19 셋째 금요일이 준틴스 → 6/18 로 당김"),
        ("exp-us-2027-06", "2027-06-17", False, "6/18 준틴스 대체휴장 → 6/17 로 당김"),
        ("exp-us-2026-09", "2026-09-18", False, "9월 셋째 금요일 당일"),
        ("exp-kr-2027-03", "2027-03-11", True,  "2027 KRX 휴장일 표가 없어 예상이어야 한다"),
        ("exp-jp-2026-09", "2026-09-11", True,  "일본 휴장일 표가 없어 예상이어야 한다"),
    ]
    for eid, when, estimated, why in exp_cases:
        ev = exp.get(eid)
        if not ev:
            fails.append(f"만기일 누락 — {eid} ({why})")
        elif ev["start"] != when:
            fails.append(f"만기일 불일치 — {eid} → {ev['start']}, 기대 {when} ({why})")
        elif ev["estimated"] != estimated:
            fails.append(f"만기일 예상 여부 불일치 — {eid} ({why})")
        elif ev["verified"] is not True:
            fails.append(f"계산된 일정이 샘플로 나갔다 — {eid} ({why})")
    # 분기월에는 한·미·일 셋 다, 그 외 달에는 한국만 나와야 한다
    for month, want in ((9, 3), (10, 1)):
        n = sum(1 for e in exp.values() if e["start"].startswith(f"2026-{month:02d}"))
        if n != want:
            fails.append(f"2026-{month:02d} 만기 이벤트 {n}건, 기대 {want}건")
    print(f"만기일 {len(exp_cases)}건 + 분기월 구성 검사")

    # 휴일 표가 없는 연도는 반드시 estimated 로 나가야 한다
    for ev in statutory_events(date(2027, 1, 1), date(2027, 12, 31)):
        if not ev["estimated"]:
            fails.append(f"2027 기한인데 estimated 가 아님 — {ev['start']}")
        if ev["verified"] is not True:
            fails.append(f"계산된 법정기한이 샘플로 나갔다 — {ev['start']}")
    print("휴장일 미확인 연도 estimated 처리 검사")

    # 손으로 관리하는 파일이라 오타가 나기 쉽다
    need = {"id", "cat", "title", "org", "start", "end", "verified", "note"}
    seen = set()
    static = load_static()
    for ev in static:
        miss = need - set(ev)
        if miss:
            fails.append(f"static_events.json 필드 누락 — {ev.get('id','?')} {sorted(miss)}")
            continue
        if ev["id"] in seen:
            fails.append(f"static_events.json id 중복 — {ev['id']}")
        seen.add(ev["id"])
        try:
            if date.fromisoformat(ev["end"]) < date.fromisoformat(ev["start"]):
                fails.append(f"static_events.json 종료일이 시작일보다 빠름 — {ev['id']}")
        except ValueError:
            fails.append(f"static_events.json 날짜 형식 오류 — {ev['id']}")
        if ev["verified"] and not ev.get("src"):
            fails.append(f"확정인데 출처가 없다 — {ev['id']}")
        # 「샘플」은 출처 자체가 미확인이라는 뜻이다. 출처가 있으면서 날짜만
        # 잠정인 일정은 verified=True + estimated=True 로 적는다.
        if ev["verified"] is False and ev.get("src"):
            fails.append(f"출처가 있는데 샘플로 표시됐다 — {ev['id']}")
    print(f"static_events.json {len(static)}건 검사")

    # 손으로 편집하는 파일이라 오타·잘못된 필드가 들어가기 쉽다
    ov = load_overrides()
    for eid, patch in ov["fix"].items():
        if not isinstance(patch, dict):
            fails.append(f"overrides fix 값이 딕셔너리가 아니다 — {eid}")
            continue
        for k, v in patch.items():
            if k not in EDITABLE:
                fails.append(f"overrides 고칠 수 없는 필드 — {eid}.{k}")
            elif k in DATE_FIELDS:
                try:
                    date.fromisoformat(v)
                except (ValueError, TypeError):
                    fails.append(f"overrides 날짜 형식 오류 — {eid}.{k} = {v}")
            elif k in BOOL_FIELDS and not isinstance(v, bool):
                fails.append(f"overrides {k} 는 true/false 여야 한다 — {eid} = {v!r}")
    for item in ov["add"]:
        for req in ("start", "title", "cat"):
            if req not in item:
                fails.append(f"overrides add 필수 필드 누락 — {item.get('id','?')}.{req}")
    print(f"overrides.json 검사 (고침 {len(ov['fix'])} 숨김 {len(ov['hide'])} 추가 {len(ov['add'])})")

    # 손질은 되돌릴 수 있어야 한다 — 적용 후 지우면 원래 값으로 돌아가는지
    probe = [{"id": "probe", "cat": "ir", "title": "원래 제목", "org": "A", "start": "2026-01-01",
              "end": "2026-01-01", "place": "X", "verified": True, "estimated": True,
              "watch": False, "note": "원래 비고"}]
    before = json.dumps(probe[0], sort_keys=True, ensure_ascii=False)
    saved = ov["fix"].get("probe")
    ov["fix"]["probe"] = {"start": "2026-02-02", "estimated": False}
    save_overrides(ov)
    try:
        apply_overrides(probe)
        if probe[0]["start"] != "2026-02-02" or probe[0]["estimated"] is not False:
            fails.append("손질이 적용되지 않았다")
        ov["fix"].pop("probe")
        if saved is not None:
            ov["fix"]["probe"] = saved
        save_overrides(ov)
        apply_overrides(probe)
        if json.dumps(probe[0], sort_keys=True, ensure_ascii=False) != before:
            fails.append(f"손질을 지웠는데 원래 값으로 안 돌아온다 — {probe[0]}")
    finally:
        ov = load_overrides()
        ov["fix"].pop("probe", None)
        save_overrides(ov)
    print("손질 적용·되돌리기 왕복 검사")

    # add 에서 지운 항목이 다음 실행에서 사라지는지 (events.json 은 누적되므로)
    ov = load_overrides()
    keep_add = list(ov["add"])
    ov["add"] = [{"id": "manual-probe", "start": "2026-01-01", "title": "왕복 검사용",
                  "cat": "ir"}]
    save_overrides(ov)
    try:
        pool = []
        apply_overrides(pool)
        if not any(e.get("manual") for e in pool):
            fails.append("직접 추가한 일정이 붙지 않았다")
        ov["add"] = []
        save_overrides(ov)
        apply_overrides(pool)                 # events.json 을 다시 읽은 상황을 흉내
        if any(e.get("manual") for e in pool):
            fails.append("add 에서 지웠는데 일정이 남아 있다")
    finally:
        ov = load_overrides()
        ov["add"] = keep_add
        save_overrides(ov)
    print("직접 추가 일정 붙임·떼임 검사")

    # --load 가 static_events.json 을 갈아끼우는지. events.json 은 누적본이라
    # 표시를 안 해 두면 static 에서 지운 항목이 화면에 계속 남는다.
    pool = [{"id": "old-static", "cat": "conf", "title": "지운 고정 일정", "org": "A",
             "start": "2026-01-01", "end": "2026-01-01", "verified": True,
             "note": "", "static": True},
            {"id": "d123", "cat": "ir", "title": "수집분", "org": "B",
             "start": "2026-01-01", "end": "2026-01-01", "verified": True, "note": ""}]
    static = load_static()
    merged = [e for e in pool if not e.get("static")] + static
    if any(e["id"] == "old-static" for e in merged):
        fails.append("static 에서 지운 항목이 --load 후에도 남는다")
    if not any(e["id"] == "d123" for e in merged):
        fails.append("수집분이 static 갱신에 휩쓸려 사라졌다")
    if not all(e.get("static") for e in static):
        fails.append("load_static() 이 static 표시를 안 붙인다")
    print(f"고정 일정 갈아끼우기 검사 ({len(static)}건)")

    if fails:
        print(f"\n실패 {len(fails)}건")
        for f in fails:
            print("  ✗", f)
        return 1
    print("\n전부 통과")
    return 0


# ══════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="DART 공시 → 투자 일정 수집기")
    ap.add_argument("--days", type=int, default=180, help="공시 수집 기간 (기본 180일)")
    ap.add_argument("--ahead", type=int, default=18, help="법정기한 생성 범위 (기본 18개월)")
    ap.add_argument("--no-doc", action="store_true", help="원문 파싱 생략 (빠름)")
    ap.add_argument("--inject", metavar="HTML", help="HTML 의 EVENTS 배열 교체")
    ap.add_argument("--out", default=os.path.join(HERE, "events.json"))
    ap.add_argument("--brief", type=int, metavar="N", help="D-N 이내 일정 텍스트 출력")
    ap.add_argument("--load", metavar="JSON", nargs="?", const="events.json",
                    help="수집하지 않고 기존 events.json 을 읽는다 (--brief 와 함께 쓴다)")
    ap.add_argument("--selftest", action="store_true")

    g = ap.add_argument_group("손질 — 자동 수집이 못 맞히는 것을 사람이 고친다")
    g.add_argument("--fix", nargs="+", metavar=("ID", "KEY=VALUE"),
                   help="일정 하나의 값을 고친다. 예) --fix stat-2027-3 estimated=false")
    g.add_argument("--unfix", metavar="ID", help="고침·숨김을 되돌린다")
    g.add_argument("--hide", metavar="ID", help="화면에서 숨긴다 (데이터는 남는다)")
    g.add_argument("--add", nargs="+", metavar="KEY=VALUE",
                   help="일정을 직접 추가한다. start·title·cat 은 필수")
    g.add_argument("--overrides", action="store_true", help="걸려 있는 손질 목록")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.overrides:
        cmd_list(); return 0
    if args.unfix:
        cmd_unfix(args.unfix); return 0
    if args.hide:
        cmd_hide(args.hide); return 0
    if args.add:
        cmd_add(args.add); return 0
    if args.fix:
        if len(args.fix) < 2:
            raise SystemExit("--fix <id> key=value ... 형식으로 쓴다")
        cmd_fix(args.fix[0], args.fix[1:]); return 0

    if args.load:
        path = args.load if os.path.isabs(args.load) else os.path.join(HERE, args.load)
        events = json.load(open(path, encoding="utf-8"))
        log(f"불러오기 — {os.path.relpath(path, HERE)} ({len(events)}건)")
        # 손으로 관리하는 파일은 다시 읽는다. API 호출이 없으니 --load 의 뜻에 어긋나지 않고,
        # 학회·중앙은행 일정을 한 줄 고치려고 2분짜리 재수집을 돌리지 않아도 된다.
        static = load_static()
        events = [e for e in events if not e.get("static")] + static
        log(f"고정 일정 갱신 — static_events.json ({len(static)}건)")
        events = dedupe(apply_overrides(events))
        json.dump(events, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if args.inject:
            inject(args.inject, events)
        if args.brief is not None:
            print(brief(events, args.brief))
        return 0

    today = date.today()
    lo = today - timedelta(days=args.days)
    hi = today + timedelta(days=args.ahead * 31)
    events = statutory_events(lo, hi)
    events += expiry_events(lo, hi)
    events += load_static()

    key = load_key()
    if key:
        got, tickers, missing = collect(key, args.days, not args.no_doc)
        events += got
        log(f"DART 수집 {len(got)}건 / 워치리스트 {len(tickers)}종목")
    else:
        log(NO_KEY)
        log("\n키 없이 계속한다 — 법정기한·만기일·고정 일정만 만든다.")

    events = dedupe(apply_overrides(events))
    json.dump(events, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ne = sum(1 for e in events if e.get("edited"))
    nh = sum(1 for e in events if e.get("hidden"))
    nm = sum(1 for e in events if e.get("manual"))
    log(f"저장 — {os.path.relpath(args.out, HERE)} ({len(events)}건"
        + (f" · 손질 고침 {ne} 숨김 {nh} 추가 {nm}" if ne or nh or nm else "") + ")")

    if args.inject:
        inject(args.inject, events)
    if args.brief is not None:
        print(brief(events, args.brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
