#!/bin/bash
# .101 人事 → .109 歸檔 每日增量同步
LOG=~/ktc_hr/logs/hr_archive_sync.log
PY=~/ktc_hr/venv/bin/python3
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 同步開始 =====" >> "$LOG"
if ! mount | grep -q 'mnt/hr_src'; then
  echo "來源 hr_src 未掛載,跳過本次" >> "$LOG"; exit 0
fi
if ! mount | grep -q 'mnt/KTC_AI_Files'; then
  echo "目的地 KTC_AI_Files 未掛載,跳過本次" >> "$LOG"; exit 0
fi
"$PY" ~/ktc_hr/jobs/hr_archive_scan.py >> "$LOG" 2>&1 && \
"$PY" ~/ktc_hr/jobs/hr_archive_copy.py >> "$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 同步結束 =====" >> "$LOG"
