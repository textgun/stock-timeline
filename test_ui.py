# 화면 회귀 테스트 — ICS 내보내기·URL 상태·단축키·레인 높이·묶음 무결성을 브라우저에서 확인한다
"""
    python3 test_ui.py          index.html 를 헤드리스 크로미움으로 열어 검사

화면을 고쳤으면 이걸 돌리고, 그다음 라이트·다크 스크린샷을 눈으로 본다.
이 테스트는 「깨지지 않았다」만 보장한다. 「보기 좋다」는 사람이 봐야 한다.
"""

import os, re
from playwright.sync_api import sync_playwright
url = "file://" + os.path.abspath("index.html")
fails = []
with sync_playwright() as pw:
    b = pw.chromium.launch(); ctx = b.new_context(viewport={"width":1400,"height":900})
    p = ctx.new_page(); p.on("pageerror", lambda e: fails.append("JS: "+str(e)))
    p.goto(url); p.wait_for_timeout(700)

    # 1. ICS 생성
    ics = p.evaluate("toICS(shown())")
    if not ics.startswith("BEGIN:VCALENDAR"): fails.append("ICS 헤더 없음")
    if not ics.rstrip().endswith("END:VCALENDAR"): fails.append("ICS 종료 없음")
    n_ev = ics.count("BEGIN:VEVENT")
    n_vis = p.evaluate("shown().length")
    if n_ev != n_vis: fails.append(f"ICS 이벤트 수 {n_ev} != 보이는 일정 {n_vis}")
    for i, line in enumerate(ics.split("\r\n")):
        if len(line.encode()) > 75 and not line.startswith(" "):
            fails.append(f"ICS {i}행이 75옥텟 초과: {line[:40]}…"); break
    if "DTSTART;VALUE=DATE:" not in ics: fails.append("DTSTART 없음")
    # 예상·샘플에는 알림이 붙으면 안 된다
    for blk in ics.split("BEGIN:VEVENT")[1:]:
        if ("(예상)" in blk or "(샘플)" in blk) and "BEGIN:VALARM" in blk:
            fails.append("예상/샘플 일정에 알림이 붙었다"); break
    print(f"ICS  — {n_ev}개 VEVENT, {len(ics)}바이트, VALARM {ics.count('BEGIN:VALARM')}개")

    # 2. 다운로드가 실제로 동작하는지
    with p.expect_download() as dl:
        p.click("#icsBtn")
    name = dl.value.suggested_filename
    if not name.endswith(".ics"): fails.append(f"다운로드 파일명 이상: {name}")
    print(f"다운로드 — {name}")

    # 3. URL 상태 저장·복원
    p.click("#watchOnly"); p.click('#viewSeg button[data-v="agenda"]')
    p.fill("#q", "배당"); p.wait_for_timeout(300)
    h = p.evaluate("location.hash")
    for k in ("v=agenda", "w=1", "q="):
        if k not in h: fails.append(f"해시에 {k} 없음 → {h}")
    print(f"해시 — {h}")
    p.goto(url + h); p.wait_for_timeout(600)
    st = p.evaluate("({v:state.view,w:state.watchOnly,q:state.q})")
    if st != {"v":"agenda","w":True,"q":"배당"}: fails.append(f"복원 실패 {st}")
    print(f"복원 — {st}")

    # 4. 단축키
    p.goto(url); p.wait_for_timeout(500)
    p.keyboard.press("a"); p.wait_for_timeout(200)
    if p.evaluate("state.view") != "agenda": fails.append("A 키로 아젠다 전환 안 됨")
    p.keyboard.press("t"); p.wait_for_timeout(300)
    if p.evaluate("state.view") != "agenda":
        fails.append("T 키가 아젠다에서 뷰를 바꿔버렸다 (오늘로 이동만 해야 한다)")
    if p.evaluate("()=>{const s=document.querySelector('#agendaToday'),e=document.querySelector('#agenda');"
                  "return s ? Math.abs(s.getBoundingClientRect().top-e.getBoundingClientRect().top)<4 : true}"):
        pass
    else:
        fails.append("T 키로 오늘 기준선까지 안 갔다")
    p.keyboard.press("a"); p.wait_for_timeout(200)
    if p.evaluate("state.view") != "timeline": fails.append("A 키로 타임라인 복귀 안 됨")
    p.keyboard.press("/"); p.wait_for_timeout(150)
    if p.evaluate("document.activeElement.id") != "q": fails.append("/ 키로 검색 포커스 안 됨")
    print("단축키 — A/T// 동작")

    # 5. 상세 패널 + 연쇄
    chained = p.evaluate("(EVENTS.find(e=>e.chain&&CHAINS[e.chain].length>1)||{}).id")
    if chained:
        p.evaluate(f"openDetail({chained!r})"); p.wait_for_timeout(200)
        html = p.inner_html("#dList")
        if "연쇄" not in html: fails.append("연쇄 일정이 상세에 안 나옴")
        print(f"상세 — 연쇄 표시 확인 ({chained})")
    else:
        print("상세 — 연쇄 사례 없음")

    # 6. 레인 높이 폭주 확인 (지난 일정까지 켠 최악의 경우)
    p.evaluate("state.view='timeline'; state.hidePast=false; state.group='cat'; render()")
    p.wait_for_timeout(400)
    rows = p.evaluate("[...document.querySelectorAll('.lanerow')].map(r=>r.offsetHeight)")
    if not rows: fails.append("타임라인에 레인이 하나도 없다")
    mx = max(rows or [0])
    print(f"최대 레인 높이 — {mx}px (지난 일정 포함, 레인 {len(rows)}개)")
    if mx > 900: fails.append(f"레인이 {mx}px 로 폭주")
    ctx.close(); b.close()

print()
if fails:
    print(f"실패 {len(fails)}건"); [print("  ✗", f) for f in fails]; raise SystemExit(1)
print("전부 통과")

# 7. 묶음이 일정을 잃지 않는지 — 모든 확대 수준에서 총합이 맞아야 한다
with sync_playwright() as pw:
    b = pw.chromium.launch(); p = b.new_page()
    p.goto(url); p.wait_for_timeout(600)
    bad = []
    for ppd in (0.9, 1.5, 2.6, 6, 14, 30, 46):
        r = p.evaluate("""(ppd)=>{
            state.ppd=ppd; state.hidePast=false; state.group='cat';
            const L=layout(); const out=[];
            L.groups.forEach(g=>{
              const items=L.by[g.key].items;
              const n=items.reduce((a,p)=>a+(p.group?p.group.length:1),0);
              const ids=new Set(items.flatMap(p=>(p.group||[p.ev]).map(e=>e.id)));
              out.push([g.key,n,g.n,ids.size]);
            });
            return out;}""", ppd)
        for key, n, want, uniq in r:
            if n != want or uniq != want:
                bad.append(f"ppd={ppd} {key}: 마커합 {n} · 고유 {uniq} · 실제 {want}")
    print(f"\n묶음 총합 — 7개 확대 수준 × {len(r)}개 레인 검사")
    if bad:
        print(f"실패 {len(bad)}건"); [print("  ✗", x) for x in bad[:6]]; raise SystemExit(1)
    print("모든 확대 수준에서 일정 수 일치")
    b.close()

# 8. 막대가 서로 겹치지 않는지 — 라벨 배치가 막대를 밀어내면 안 된다
with sync_playwright() as pw:
    b = pw.chromium.launch(); p = b.new_page()
    p.goto(url); p.wait_for_timeout(600)
    bad = []
    for ppd in (0.9, 2.6, 6, 14, 30, 46):
        for hide in (True, False):
            r = p.evaluate("""([ppd,hide])=>{
                state.ppd=ppd; state.hidePast=hide; state.group='cat';
                const L=layout(); const bad=[];
                L.groups.forEach(g=>{
                  const rows={};
                  L.by[g.key].items.forEach(p=>{(rows[p.row]=rows[p.row]||[]).push(p);});
                  Object.entries(rows).forEach(([r,ps])=>{
                    ps.sort((a,b)=>a.left-b.left);
                    for(let i=0;i<ps.length-1;i++)
                      if(ps[i].left+ps[i].w > ps[i+1].left+0.5)
                        bad.push(`${g.key} row${r}`);
                  });
                  if(L.by[g.key].rows > 8) bad.push(`${g.key} rows=${L.by[g.key].rows}`);
                });
                return bad;}""", [ppd, hide])
            bad += [f"ppd={ppd} hidePast={hide}: {x}" for x in r]
    print(f"\n막대 겹침 — 6개 확대 수준 × 2가지 필터 검사")
    if bad:
        print(f"실패 {len(bad)}건"); [print("  ✗", x) for x in bad[:6]]; raise SystemExit(1)
    print("겹침 없음 · 레인 8줄 이하 유지")
    b.close()

# 9. 손질 — 숨긴 일정이 화면에서 빠지고, 고친 흔적이 상세에 나오는지
with sync_playwright() as pw:
    b = pw.chromium.launch(); p = b.new_page()
    p.on("pageerror", lambda e: fails.append("JS: "+str(e)))
    p.goto(url); p.wait_for_timeout(600)
    bad = []

    # 임의의 일정을 숨김·수정 상태로 만들어 화면 동작만 본다 (파일은 건드리지 않는다)
    r = p.evaluate("""()=>{
        const vis=shown();                    // 지금 보이는 것 중에서 골라야 개수 변화가 보인다
        const a=vis[0], b=vis[1];
        const before=vis.length;
        a.hidden=true;
        const after=shown().length;
        b.edited=true; b._orig={start:'2020-01-01'};
        openDetail(b.id);
        const out={before, after,
          edits: document.querySelector('#dEdits').hidden===false,
          idShown: document.querySelector('#dId').textContent.includes(b.id),
          flag: document.querySelector('#dTitle').innerHTML.includes('수정')};
        delete a.hidden; delete b.edited; delete b._orig; render();
        return out;}""")
    if r["after"] != r["before"]-1: bad.append(f"숨긴 일정이 안 빠졌다 {r['before']}→{r['after']}")
    if not r["edits"]:   bad.append("고친 흔적 블록이 안 보인다")
    if not r["idShown"]: bad.append("상세에 id 가 없다")
    if not r["flag"]:    bad.append("제목에 「수정」 딱지가 없다")

    # 복사되는 명령이 실제로 붙여넣어 쓸 수 있는 모양인가
    cmd = p.evaluate("""()=>{
        let got=null;
        const real=navigator.clipboard;
        Object.defineProperty(navigator,'clipboard',{configurable:true,
          value:{writeText:t=>{got=t;return Promise.resolve();}}});
        const ev=EVENTS.find(e=>e.estimated)||EVENTS[0];
        openDetail(ev.id);
        document.querySelector('#dFix').click();
        Object.defineProperty(navigator,'clipboard',{configurable:true,value:real});
        return {cmd:got, id:ev.id, est:!!ev.estimated, start:ev.start};}""")
    c = cmd["cmd"] or ""
    if not c.startswith("python3 collect_dart.py --fix "): bad.append(f"고치기 명령 형식 이상: {c}")
    if cmd["id"] not in c:            bad.append("명령에 id 가 없다")
    if f"start={cmd['start']}" not in c: bad.append("명령에 현재 날짜가 안 들어갔다")
    if cmd["est"] and "estimated=false" not in c: bad.append("예상 일정인데 estimated=false 가 없다")

    # 지난 한 달치는 보이고, 그보다 오래된 것은 접혀 있어야 한다
    w = p.evaluate("""()=>{
        state.view='timeline'; state.hidePast=true; state.q=''; state.cats=new Set(Object.keys(CATS));
        state.watchOnly=false; state.only30=false; render();
        const v=shown();
        return {보임:v.length,
                지난:v.filter(isPast).length,
                가장오래된:Math.min(...v.map(e=>days(today,endOf(e)))),
                ics:toICS(v.filter(e=>!isPast(e))).split('BEGIN:VEVENT').length-1,
                ics지난포함:v.filter(isPast).length>0};}""")
    if w["지난"] == 0:            bad.append("지난 일정이 하나도 안 보인다")
    if w["가장오래된"] < -30:      bad.append(f"한 달보다 오래된 일정이 보인다 ({w['가장오래된']}일)")
    if w["ics"] != w["보임"]-w["지난"]: bad.append("ICS 에 지난 일정이 섞였다")
    print(f"과거 창 — 보임 {w['보임']} (지난 {w['지난']}, 최대 {-w['가장오래된']}일 전) · ICS {w['ics']}건")
    print(f"\n손질 — 숨김·수정 표시·명령 생성 검사\n  {c}")
    if bad:
        print(f"실패 {len(bad)}건"); [print("  ✗", x) for x in bad]; raise SystemExit(1)
    print("손질 동작 정상")
    b.close()
