#!/bin/bash
# 일정을 수집해 GitHub Pages 로 배포한다 — 로컬 크론용.
#
# 키는 이 기계의 .env 에만 있고 GitHub 로 나가지 않는다.
# GitHub Actions 로 돌리려면 키를 저장소 시크릿에 올려야 하므로 이 경로를 쓴다.
#
#   crontab -e
#   30 19 * * 1-5 /home/piction/.openclaw/workspace/schedule/daily.sh

set -u
DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/.." && pwd)
mkdir -p "$DIR/logs"
exec >> "$DIR/logs/collect_$(date +%Y-%m-%d).log" 2>&1

echo "[$(date '+%F %T')] 수집 시작"
cd "$DIR" || exit 1

# 분류 규칙이 깨진 채로 배포되지 않게 먼저 막는다
python3 collect_dart.py --selftest || { echo "셀프테스트 실패 — 중단"; exit 1; }

before=$(python3 -c "import json;print(len(json.load(open('events.json'))))" 2>/dev/null || echo 0)
python3 collect_dart.py --days 180 --inject index.html --brief 14 || { echo "수집 실패 — 중단"; exit 1; }
after=$(python3 -c "import json;print(len(json.load(open('events.json'))))")
echo "일정 $before → $after"

# 원문 서식이 바뀌거나 API 가 흔들리면 절반쯤 실패해도 명령 자체는 성공으로 끝난다.
# 그대로 배포하면 모아 둔 일정이 사라지므로 되돌리고 멈춘다.
if [ "$before" -gt 0 ] && [ "$after" -lt $((before * 6 / 10)) ]; then
  echo "급감 — 배포하지 않고 되돌린다"
  cd "$ROOT" && git checkout -- schedule/events.json schedule/index.html
  exit 1
fi

cd "$ROOT" || exit 1
git add schedule/events.json schedule/index.html
if git diff --cached --quiet; then
  echo "변경 없음 — 배포 생략"
else
  git commit -q -m "일정 자동 갱신 $(date +%F)"
  # schedule/ 은 이 저장소의 하위 폴더지만 배포 저장소에서는 루트다
  if git push --force schedule-pages "$(git subtree split --prefix=schedule)":main; then
    echo "배포 완료 — https://textgun.github.io/stock-timeline/"
  else
    echo "푸시 실패"
    exit 1
  fi
fi
echo "[$(date '+%F %T')] 끝"
