#!/bin/bash
# 일정을 수집해 GitHub Pages 로 배포한다 — 크론용.
#
# 키는 이 기계의 .env 에만 있고 GitHub 로 나가지 않는다.
#
#   openclaw cron add --name "주식 일정 타임라인 배포" \
#     --cron "30 19 * * 1-5" --tz Asia/Seoul \
#     --command /home/piction/.openclaw/workspace/schedule/daily.sh \
#     --announce --channel telegram --to <chatId> --timeout-seconds 2400
#
# 자세한 기록은 logs/ 에 쌓고, 표준출력에는 요약만 낸다.
# 크론이 표준출력을 텔레그램으로 보내므로 여기에 로그를 다 흘리면 안 된다.

set -u
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/.." && pwd)
mkdir -p "$DIR/logs"
LOG="$DIR/logs/collect_$(date +%Y-%m-%d).log"

exec 3>&1                      # 원래 표준출력을 3번에 보관해 둔다
exec >> "$LOG" 2>&1            # 나머지는 전부 로그 파일로
say(){ echo "$@" >&3; }        # 요약만 3번으로 — 이것만 크론이 전달한다

fail(){ say "❌ 주식 일정 타임라인 — $1"; say "   로그: ${LOG/#$HOME/\~}"; exit 1; }

# 동시 실행 잠금 — 크론이 두 군데 걸리면 같은 시각에 두 번 돌며 events.json 을
# 동시에 쓰고 index.lock 이 부딪힌다(2026-10~11월 실제로 그랬다).
# 뒤에 온 실행은 앞의 실행이 끝날 때까지 기다렸다가 돈다. 그러면 「변경 없음」으로 끝난다.
exec 9> "$DIR/logs/daily.lock"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 앞선 실행이 끝나길 기다림"
  flock -w 600 9 || fail "앞선 실행이 10분 넘게 안 끝남 — 이번 실행은 건너뜀"
fi

echo "[$(date '+%F %T')] 수집 시작"
cd "$DIR" || fail "디렉터리 진입 실패"

# 분류 규칙이 깨진 채로 배포되지 않게 먼저 막는다
python3 collect_dart.py --selftest || fail "셀프테스트 실패 — 배포하지 않음"

before=$(python3 -c "import json;print(len(json.load(open('events.json'))))" 2>/dev/null || echo 0)
python3 collect_dart.py --days 180 --inject index.html || fail "수집 실패"
after=$(python3 -c "import json;print(len(json.load(open('events.json'))))")
echo "일정 $before → $after"

# 원문 서식이 바뀌거나 API 가 흔들리면 절반쯤 실패해도 명령 자체는 성공으로 끝난다.
# 그대로 배포하면 모아 둔 일정이 사라지므로 되돌리고 멈춘다.
if [ "$before" -gt 0 ] && [ "$after" -lt $((before * 6 / 10)) ]; then
  cd "$ROOT" && git checkout -- schedule/events.json schedule/index.html
  fail "일정이 $before → $after 로 급감. 되돌리고 배포 중단"
fi

cd "$ROOT" || fail "저장소 진입 실패"
git add schedule/events.json schedule/index.html
if git diff --cached --quiet; then
  echo "변경 없음 — 커밋 생략"
  deployed="변경 없음"
else
  git commit -q -m "일정 자동 갱신 $(date +%F)"
  deployed="배포 완료"
fi

# 새 커밋이 없어도 원격이 뒤처져 있으면 올린다. 예전에는 푸시가 한 번 실패하면
# 그 커밋이 다음 변경이 생길 때까지 로컬에만 남아 화면이 멈춰 있었다.
# schedule/ 은 이 저장소의 하위 폴더지만 배포 저장소에서는 루트다
# -q 로 진행 표시를 끈다. 진행 표시는 \r 로만 덮어써서 로그에서는 수천 자짜리 한 줄이 되고
# 다음 메시지까지 그 끝에 붙는다. 오류는 -q 여도 그대로 나온다.
split=$(git subtree split -q --prefix=schedule) || fail "subtree split 실패"
remote=$(GIT_TERMINAL_PROMPT=0 git ls-remote schedule-pages refs/heads/main | cut -f1)
if [ "$split" = "$remote" ]; then
  echo "원격과 같음 — 푸시 생략"
else
  [ "$deployed" = "변경 없음" ] && deployed="밀린 커밋 배포 완료"
  # 푸시 토큰은 ~/.git-credentials 에 기대지 않고 .env 에서 읽는다.
  # 그 파일이 2026-11-14 에 비워진 뒤 11-16 부터 배포가 멈춰 있었다.
  # DART 키와 같은 순서로 찾는다 — 환경변수 → 이 폴더 .env → ../stock_analyzer/.env
  [ -n "${GITHUB_TOKEN:-}" ] || GITHUB_TOKEN=$(sed -nE \
    "s/^[[:space:]]*GITHUB_TOKEN[[:space:]]*=[[:space:]]*[\"']?([^\"'[:space:]#]+).*/\1/p" \
    "$DIR/.env" "$ROOT/stock_analyzer/.env" 2>/dev/null | head -1)
  [ -n "$GITHUB_TOKEN" ] || fail "GITHUB_TOKEN 없음 — stock_analyzer/.env 확인 (커밋은 로컬에 남음)"
  export GITHUB_TOKEN
  # credential.helper= 로 전역 store 를 끈다. 성공해도 토큰을 디스크에 다시 쓰지 않고,
  # 실패해도 다른 저장소가 쓰는 자격 증명을 지우지 않는다.
  GIT_TERMINAL_PROMPT=0 git -c credential.helper= \
    -c 'credential.helper=!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
    push --force schedule-pages "$split":main || fail "푸시 실패"
fi

brief=$(cd "$DIR" && python3 collect_dart.py --load --brief 14 2>>"$LOG")
echo "$brief"
echo "[$(date '+%F %T')] 끝"

say "📅 주식 일정 타임라인 — $deployed (전체 ${after}건)"
say "https://textgun.github.io/stock-timeline/"
say ""
say "$brief"
