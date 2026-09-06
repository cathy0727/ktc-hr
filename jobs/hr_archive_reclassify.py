# -*- coding: utf-8 -*-
"""
hr_archive_reclassify.py — 事後分類搬移(2026-09-06)
====================================================
背景:hr_archive_copy.py 只照歸檔計畫.csv 的目的地搬檔,分類在掃描端決定;
A1 patch 只套到證照規則,證件/保險/健檢三類沒進計畫檔 → 已落地檔案全在員工夾本層。
本腳本讀既有歸檔計畫.csv,把已落地的檔案「就地移進」正確子夾——
同一 NAS 共享內為 rename,瞬間完成,不重新複製、不碰來源區。

分類規則(依 2026-09-06 定案):
  健檢 / 保險 / 證件 → 只看「檔名」
  證照              → 看「整條來源路徑」(含 技術證/執照/合格 等補充關鍵字)
  優先序:健檢 > 保險 > 證件 > 證照(檔名類先判,保險單放在證照夾也會歸對)

用法:
  python3 hr_archive_reclassify.py --dry-run   # 只列計畫不動檔(先跑這個)
  python3 hr_archive_reclassify.py             # 實際搬移
可重跑:已在正確子夾的檔案自動跳過。
"""
import csv
import sys
import unicodedata
from pathlib import Path
from datetime import datetime

DST = Path.home() / 'mnt/KTC_AI_Files/人事歸檔'
PLAN = DST / '歸檔計畫.csv'
LOG = DST / '分類搬移日誌.csv'

CATS = ['健檢', '保險', '證件', '證照']  # 判定優先序
KW = {
    '健檢': ['健檢', '體檢', '健康檢查'],
    '保險': ['保險', '旅平', '勞保', '健保', '加保', '退保', '團保',
             '意外險', '眷屬', '投保'],
    '證件': ['身分證', '雙證件', '證件', '護照', '駕照', '戶籍', '戶口'],
    '證照': ['證照', '證書', '技術證', '執照', '合格'],
}


def classify(src_rel: str) -> str | None:
    """檔名類(健檢/保險/證件)先判,證照看整條路徑。回 None = 不分類留本層。"""
    fname = Path(src_rel).name
    for cat in ('健檢', '保險', '證件'):
        if any(k in fname for k in KW[cat]):
            return cat
    if any(k in src_rel for k in KW['證照']):
        return '證照'
    return None


def find_current(dst_path: Path) -> Path | None:
    """定位檔案現在的位置:計畫目的地本身、NFC 對照、或改名序號版。"""
    if dst_path.exists():
        return dst_path
    parent = dst_path.parent
    if not parent.exists():
        return None
    want = unicodedata.normalize('NFC', dst_path.name)
    stem, suf = dst_path.stem, dst_path.suffix
    serial = None
    try:
        for e in parent.iterdir():
            n = unicodedata.normalize('NFC', e.name)
            if n == want:
                return e
            if serial is None and n.startswith(stem + '_') and n.endswith(suf):
                serial = e
    except OSError:
        return None
    return serial


def main() -> None:
    dry = '--dry-run' in sys.argv

    rows = [r for r in csv.DictReader(open(PLAN, encoding='utf-8-sig'))
            if r['判定依據'] in ('工號', '姓名')]
    print(f'計畫筆數:{len(rows)}(工號+姓名){"  [DRY-RUN 只列不搬]" if dry else ""}')

    stats = {'搬移': 0, '已在正確位置': 0, '不分類(留本層)': 0,
             '找不到檔案': 0, '目標已存在跳過': 0, '失敗': 0}
    logs = []

    for i, r in enumerate(rows, 1):
        if i % 1000 == 0:
            print(f'...進度 {i}/{len(rows)}')
        cat = classify(r['來源相對路徑'])
        if cat is None:
            stats['不分類(留本層)'] += 1
            continue

        planned = DST / r['目的地']
        # 員工夾 = 目的地路徑的前兩層(依員工/XXXX_姓名)
        parts = Path(r['目的地']).parts
        if len(parts) < 2 or parts[0] != '依員工':
            stats['不分類(留本層)'] += 1
            continue
        emp_dir = DST / parts[0] / parts[1]

        cur = find_current(planned)
        if cur is None:
            # 可能已被前次搬移歸位 → 檢查目標子夾
            in_cat = find_current(emp_dir / cat / planned.name)
            if in_cat is not None:
                stats['已在正確位置'] += 1
            else:
                stats['找不到檔案'] += 1
                logs.append([r['來源相對路徑'], r['目的地'], cat,
                             '找不到檔案', ''])
            continue

        if unicodedata.normalize('NFC', cur.parent.name) == cat:
            stats['已在正確位置'] += 1
            continue

        target = emp_dir / cat / cur.name
        try:
            if target.exists():
                if target.stat().st_size == cur.stat().st_size:
                    stats['目標已存在跳過'] += 1
                    logs.append([r['來源相對路徑'], str(target.relative_to(DST)),
                                 cat, '目標已存在跳過', '同大小'])
                    continue
                stem, suf = target.stem, target.suffix
                n = 2
                while (target.parent / f'{stem}_{n}{suf}').exists():
                    n += 1
                target = target.parent / f'{stem}_{n}{suf}'
            if dry:
                stats['搬移'] += 1
                logs.append([r['來源相對路徑'], str(target.relative_to(DST)),
                             cat, '(dry-run)搬移', ''])
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                cur.rename(target)  # 同共享內 = server-side rename,瞬間
                stats['搬移'] += 1
                logs.append([r['來源相對路徑'], str(target.relative_to(DST)),
                             cat, '搬移', ''])
        except Exception as e:
            stats['失敗'] += 1
            logs.append([r['來源相對路徑'], r['目的地'], cat,
                         '失敗', type(e).__name__])

    if not dry and logs:
        new = not LOG.exists()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG, 'a', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(['時間', '來源', '新位置', '分類', '動作', '備註'])
            w.writerows([ts] + row for row in logs)

    print('\n結果:')
    for k, v in stats.items():
        print(f'  {k}: {v}')
    if dry:
        print('\n(dry-run 未動任何檔;確認數字合理後拿掉 --dry-run 實跑)')
    else:
        print(f'日誌:{LOG}')


if __name__ == '__main__':
    main()
