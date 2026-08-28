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
  echo "변경 없음 — 배포 생략"
  deployed="변경 없음"
else
  git commit -q -m "일정 자동 갱신 $(date +%F)"
  # schedule/ 은 이 저장소의 하위 폴더지만 배포 저장소에서는 루트다
  git push --force schedule-pages "$(git subtree split --prefix=schedule)":main || fail "푸시 실패"
  deployed="배포 완료"
fi

brief=$(cd "$DIR" && python3 collect_dart.py --load --brief 14 2>>"$LOG")
echo "$brief"
echo "[$(date '+%F %T')] 끝"

say "📅 주식 일정 타임라인 — $deployed (전체 ${after}건)"
say "https://textgun.github.io/stock-timeline/"
say ""
say "$brief"
