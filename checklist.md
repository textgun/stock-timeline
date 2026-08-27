# 체크리스트 — 바이오·IR 일정 타임라인 실용화

## 1. 수집기 `collect_dart.py`
- [x] 워치리스트 로드 — `watchlist.json`, 없으면 stock_analyzer `active_universe.json` + `user_picks.json` 병합
- [x] corp_code 매핑 — DART `corpCode.xml` 다운로드, 로컬 `corp_codes.xml` 있으면 재사용, `data/corp_map.json` 캐시
- [x] 법정 제출기한 생성 — 분기/반기 45일, 사업보고서 90일, 주말·공휴일 익영업일 보정
- [x] DART `list.json` 수집 — 워치리스트 종목별 최근 N일
- [x] `report_nm` → 카테고리 분류 RULES
- [x] 원문 파싱 — 주총일, 배당 기준일·지급일, 유상증자 청약→납입→상장
- [x] 연쇄 일정 — `chain` / `chainStep` 필드로 평평한 배열 유지
- [x] `--selftest` — 키 없이 규칙·기한계산 검증
- [x] `--inject` — index.html 의 EVENTS 배열 교체
- [x] `--brief` — D-7 이내 일정 텍스트 출력 (텔레그램 등에 파이프)

## 2. 화면 `index.html`
- [x] 축 범위 동적화 — 오늘 기준 -6M ~ +18M, 데이터에 맞춰 확장
- [~] `events.json` 외부 로드 — 하지 않기로 결정 (context-notes 결정 8)
- [x] 워치리스트 필터 — "내 종목만" 토글
- [x] 아젠다 뷰 — 날짜별 리스트, 타임라인과 전환
- [x] ICS 내보내기 — 현재 필터 결과를 캘린더 파일로
- [x] 연쇄 일정 연결선 (같은 줄에 놓인 것끼리 · 상세 패널에 단계 목록)
- [x] URL 해시에 필터 상태 저장
- [x] 키보드 — ←/→ 이동, T 오늘, / 검색
- [x] 라이트·다크 스크린샷 확인

## 3. 마무리
- [x] `--selftest` 통과
- [x] 실데이터 수집 → `events.json` 생성
- [x] CLAUDE.md 를 실제 파일명·명령어와 동기화
- [x] 커밋

## 4. 작업 중 추가된 것
- [x] 밀집 구간 묶음 마커 — 레인 폭주(1002px → 162px) 대응
- [x] `test_ui.py` — ICS·URL 상태·단축키·레인 높이·묶음 무결성 회귀 테스트
- [x] `--load` — 수집 없이 events.json 을 읽어 주입·요약
- [x] 자기주식 취득을 점 두 개가 아닌 구간 하나로
- [x] 정정·자회사 중복 공시 접기 (`dedupe`)
- [x] 채무증권 증권신고서 제외 — 주식 일정이 아니다
