# -*- coding: utf-8 -*-
"""
hr_archive_expiry.py — 歸檔總表 + 證照效期表(2026-09-06)
=========================================================
掃 人事歸檔/依員工 全樹:
  1. 歸檔總表.csv  — 逐檔清單(工號/姓名/分類/檔名/相對路徑/大小/修改日),
     供人事逐檔覆核(混入檔、分類錯置在此篩)
  2. 證照效期表.csv — 證照子夾檔案,從「檔名」抽日期:
     「至/到/效期/有效期限」+ 日期 → 到期日(民國 7 碼、民國年月日、西元皆可)
     只有單一日期 → 僅參考(多為發證日)
     狀態:已過期 / 90天內到期 / 正常 / 僅參考日期 / 無日期
不連 DB、不動任何檔案,純讀檔名產表。日期抽不到的之後視數量評估 AI 讀內容回填。
"""
import csv
import re
from pathlib import Path
from datetime import date, datetime, timedelta

DST = Path.home() / 'mnt/KTC_AI_Files/人事歸檔'
ROOT = DST / '依員工'
OUT_ALL = DST / '歸檔總表.csv'
OUT_CERT = DST / '證照效期表.csv'

CATS = {'證件', '保險', '健檢', '證照'}
EXP_MARK = re.compile(r'(至|到|效期|有效期限|期限)')

# 民國年月日:114年5月20日 / 114.5.20 / 114-05-20(年 90–130)
ROC_SEP = re.compile(r'(?<!\d)(9\d|1[0-3]\d)[年./-](\d{1,2})[月./-](\d{1,2})日?(?!\d)')
# 民國 7 碼:1140520
ROC_7 = re.compile(r'(?<!\d)(09\d|1[0-3]\d)(\d{2})(\d{2})(?!\d)')
# 西元:2026/5/20、2026-05-20、2026.5.20、20260520
W_SEP = re.compile(r'(?<!\d)(20[0-4]\d)[./-](\d{1,2})[./-](\d{1,2})(?!\d)')
W_8 = re.compile(r'(?<!\d)(20[0-4]\d)(\d{2})(\d{2})(?!\d)')


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_dates(name: str) -> list[tuple[int, date]]:
    """回 [(在檔名中的位置, 日期)],西元優先比對避免被民國 7 碼誤吃。"""
    found: list[tuple[int, int, date]] = []  # (start, end, date)

    def add(m, y, mo, dd):
        dt = _mk(y, mo, dd)
        if dt is None:
            return
        s, e = m.span()
        for fs, fe, _ in found:  # 區間重疊 = 已被更早規則吃掉
            if s < fe and e > fs:
                return
        found.append((s, e, dt))

    for m in W_SEP.finditer(name):
        add(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in W_8.finditer(name):
        add(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in ROC_SEP.finditer(name):
        add(m, int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    for m in ROC_7.finditer(name):
        add(m, int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    return sorted([(s, d) for s, _, d in found])


def judge(name: str) -> tuple[str, str, str]:
    """回 (到期日字串, 狀態, 判讀依據)。"""
    dates = extract_dates(name)
    if not dates:
        return '', '無日期', ''
    expiry = None
    for pos, dt in dates:
        seg = name[max(0, pos - 6):pos]  # 日期前 6 字內有「至/到/效期」= 到期日
        if EXP_MARK.search(seg):
            expiry = dt
            basis = f'「{seg.strip()}」+{dt:%Y-%m-%d}'
            break
    if expiry is None:
        last = dates[-1][1]
        return f'{last:%Y-%m-%d}', '僅參考日期', '無到期標記,取檔名末位日期'
    today = date.today()
    if expiry < today:
        status = '已過期'
    elif expiry <= today + timedelta(days=90):
        status = '90天內到期'
    else:
        status = '正常'
    return f'{expiry:%Y-%m-%d}', status, basis


def main() -> None:
    if not ROOT.exists():
        print(f'找不到 {ROOT}')
        return

    all_rows, cert_rows = [], []
    n = 0
    for emp_dir in sorted(ROOT.iterdir()):
        if not emp_dir.is_dir():
            continue
        parts = emp_dir.name.split('_', 1)
        emp_no = parts[0]
        emp_name = parts[1] if len(parts) > 1 else ''
        for f in emp_dir.rglob('*'):
            if not f.is_file() or f.name.startswith('.'):
                continue
            n += 1
            if n % 1000 == 0:
                print(f'...已掃 {n} 檔')
            rel = f.relative_to(ROOT)
            cat = f.parent.name if f.parent.name in CATS else '本層'
            st = f.stat()
            all_rows.append([
                emp_no, emp_name, cat, f.name, str(rel),
                st.st_size,
                datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d'),
            ])
            if cat == '證照':
                expiry, status, basis = judge(f.name)
                cert_rows.append([emp_no, emp_name, f.name, str(rel),
                                  expiry, status, basis])

    with open(OUT_ALL, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['工號', '姓名', '分類', '檔名', '相對路徑', '大小', '修改日'])
        w.writerows(all_rows)

    order = {'已過期': 0, '90天內到期': 1, '正常': 2, '僅參考日期': 3, '無日期': 4}
    cert_rows.sort(key=lambda r: (order.get(r[5], 9), r[0]))
    with open(OUT_CERT, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['工號', '姓名', '檔名', '相對路徑', '到期日', '狀態', '判讀依據'])
        w.writerows(cert_rows)

    from collections import Counter
    cnt = Counter(r[5] for r in cert_rows)
    print(f'\n掃描完成:共 {n} 檔')
    print(f'歸檔總表:{OUT_ALL}({len(all_rows)} 列)')
    print(f'證照效期表:{OUT_CERT}({len(cert_rows)} 列)')
    for k in ('已過期', '90天內到期', '正常', '僅參考日期', '無日期'):
        if cnt.get(k):
            print(f'  {k}: {cnt[k]}')


if __name__ == '__main__':
    main()
