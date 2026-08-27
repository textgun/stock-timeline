# DART 공시에서 투자 일정을 뽑아 events.json 을 만드는 수집기

"""
바이오·IR 일정 타임라인 수집기.

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
UA = {"User-Agent": "bio-ir-timeline/1.0"}


def log(msg):
    print(msg, file=sys.stderr)


# ══════════════════════════════════════════════════════════════════
#  API 키 · 워치리스트 · 기업코드
# ══════════════════════════════════════════════════════════════════

# 이 프로젝트에는 키를 두지 않는다. 환경변수를 먼저 보고, 없으면
# 같은 워크스페이스의 stock_analyzer 설정을 재사용한다.
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
                    log(f"키 로드 — {os.path.relpath(path, HERE)}")
                    return m.group(1)
    return None


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


def is_business_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def next_business_day(d):
    """d 가 휴일이면 다음 영업일로 민다. 법정기한의 익영업일 규칙."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


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
                "verified": not unknown,
                "estimated": unknown,
                "watch": False,
                "note": note,
                "src": "https://dart.fss.or.kr/",
            })
    return out


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
    ("증권신고서(지분증권)",         "cap",  "rights",    "증권신고서(지분증권)"),

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
        if not lo:
            return []
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
        if not when or when < "2000-01-01":
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

def api_list(key, corp_code, bgn, end, page=1):
    url = (f"{API}/list.json?crtfc_key={key}&corp_code={corp_code}"
           f"&bgn_de={bgn}&end_de={end}&page_count=100&page_no={page}")
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))


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
            res = api_list(key, info["corp_code"], bgn, end)
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
                "title": f"{info['name']} {title} 공시",
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
        log(f"[{i}/{len(tickers)}] {info['name']} — 공시 {res.get('total_count')}건 중 {kept}건 채택")
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
    return json.load(open(path, encoding="utf-8"))


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
    for ev in sorted(kept, key=lambda e: e["id"]):
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
    ("증권신고서(지분증권)", "cap", "rights"),
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
                "kind_title": "테스트", "start": "2026-01-01", "rcept": "0",
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

    # 휴일 표가 없는 연도는 반드시 estimated 로 나가야 한다
    for ev in statutory_events(date(2027, 1, 1), date(2027, 12, 31)):
        if not ev["estimated"]:
            fails.append(f"2027 기한인데 estimated 가 아님 — {ev['start']}")
    print("휴장일 미확인 연도 estimated 처리 검사")

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
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.load:
        path = args.load if os.path.isabs(args.load) else os.path.join(HERE, args.load)
        events = json.load(open(path, encoding="utf-8"))
        log(f"불러오기 — {os.path.relpath(path, HERE)} ({len(events)}건)")
        if args.inject:
            inject(args.inject, events)
        if args.brief is not None:
            print(brief(events, args.brief))
        return 0

    today = date.today()
    events = statutory_events(today - timedelta(days=args.days),
                              today + timedelta(days=args.ahead * 31))
    events += load_static()

    key = load_key()
    if key:
        got, tickers, missing = collect(key, args.days, not args.no_doc)
        events += got
        log(f"DART 수집 {len(got)}건 / 워치리스트 {len(tickers)}종목")
    else:
        log("DART_API_KEY 없음 — 법정기한과 고정 일정만 생성한다")

    events = dedupe(events)
    json.dump(events, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"저장 — {os.path.relpath(args.out, HERE)} ({len(events)}건)")

    if args.inject:
        inject(args.inject, events)
    if args.brief is not None:
        print(brief(events, args.brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
