#!/bin/bash
# 앞으로(내일~1주) 주요 주식 일정을 텔레그램으로 알려주는 크론 래퍼.
#
# collect_dart.py --brief N 로 D-N 이내 일정을 뽑고, 오늘(D-0) 일정은
# 이미 아는 것이므로 빼고 **내일부터** 보낸다. ★ 는 워치리스트(내 종목).
#
# 사용: crontab 에 하루 1회, 평일 저녁
#   35 19 * * 1-5 /home/piction/.openclaw/workspace/schedule/send_d1_alarm.sh

set -u
DIR=$(cd "$(dirname "$0")" && pwd)
STOCK_NEWS="$DIR/../stock_news"
LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/d1_alarm_$(date +%Y-%m-%d).log"

{
    echo "[$(date '+%F %T')] D-1 일정 알림 시작"

    cd "$DIR" || exit 1

    # events.json 기반으로 앞으로 1주 주요 일정을 뽑는다 (수집 없이, API 호출 없음)
    # --load: 기존 events.json 사용 / --brief 7: D-7 이내 (내일~다음주)
    # 금요일엔 brief 9 로 주말·월요일 일정까지 포함되도록 넓힌다.
    DOW=$(date +%u)  # 1=월 ... 5=금
    if [ "$DOW" = "5" ]; then N=9; else N=7; fi

    BRIEF=$(python3 collect_dart.py --load --brief "$N" 2>/dev/null)
    EXIT=$?
    if [ $EXIT -ne 0 ]; then
        echo "collect_dart --brief 실패 (종료코드 $EXIT) — 알림 없음"
        exit 1
    fi

    # 오늘(D-0) 일정은 이미 아는 것이므로 빼고, 내일부터만 보낸다
    MSG=$(echo "$BRIEF" | grep -E '^[·★] D-[1-9]' )

    if [ -z "$MSG" ]; then
        echo "앞으로 예정 일정이 없습니다 — 알림 생략"
        exit 0
    fi

    HEADER="📅 앞으로 1주 주요 일정 (기준 $(date +%m-%d))"
    FULL="$HEADER"$'\n'"$MSG"
    echo "전송 내용:"
    echo "$FULL"

    cd "$STOCK_NEWS" || exit 1
    python3 -c "import sys; sys.path.insert(0, '.'); import telegram_sender as ts; ts.send_telegram_message(sys.argv[1])" "$FULL"

    echo "[$(date '+%F %T')] 완료"
} >> "$LOG" 2>&1
