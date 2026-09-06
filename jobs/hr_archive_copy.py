import csv, shutil, sys, unicodedata
from pathlib import Path

def resolve(base, rel):
    cur = base
    for part in Path(rel).parts:
        cand = cur / part
        if cand.exists():
            cur = cand; continue
        hit = None
        try:
            for e in cur.iterdir():
                if unicodedata.normalize('NFC', e.name) == part:
                    hit = e; break
        except OSError:
            return None
        if hit is None: return None
        cur = hit
    return cur
from datetime import datetime

SRC = Path.home() / 'mnt/hr_src/03_人事總務行政/【人事】'
DST = Path.home() / 'mnt/KTC_AI_Files/人事歸檔'
PLAN = DST / '歸檔計畫.csv'
LOG = DST / '歸檔日誌.csv'

rows = [r for r in csv.DictReader(open(PLAN, encoding='utf-8-sig'))
        if r['判定依據'] in ('工號', '姓名')]
print(f'待複製：{len(rows)} 筆')

logs, stats = [], {'複製': 0, '跳過(已存在)': 0, '改名複製': 0, '失敗': 0}
for i, r in enumerate(rows, 1):
    if i % 500 == 0: print(f'...進度 {i}/{len(rows)}')
    src = SRC / r['來源相對路徑']
    if not src.exists():
        src = resolve(SRC, r['來源相對路徑']) or src
    dst = DST / r['目的地']
    action, note = '', ''
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.stat().st_size == src.stat().st_size:
                action = '跳過(已存在)'
            else:
                stem, suf = dst.stem, dst.suffix
                if any(c.stat().st_size == src.stat().st_size
                       for c in dst.parent.glob(f'{stem}_*{suf}')):
                    stats['跳過(已存在)'] += 1
                    logs.append([datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                 r['來源相對路徑'], r['目的地'], '跳過(已存在)', '序號檔已有'])
                    continue
                n = 2
                while (dst.parent / f'{stem}_{n}{suf}').exists(): n += 1
                dst = dst.parent / f'{stem}_{n}{suf}'
                shutil.copy2(src, dst)
                action, note = '改名複製', dst.name
        else:
            shutil.copy2(src, dst)
            action = '複製'
    except Exception as e:
        action, note = '失敗', type(e).__name__
    stats[action] += 1
    logs.append([datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 r['來源相對路徑'], r['目的地'], action, note])

with open(LOG, 'a', newline='', encoding='utf-8-sig') as fh:
    w = csv.writer(fh)
    if fh.tell() == 0:
        w.writerow(['時間', '來源', '目的地', '動作', '備註'])
    w.writerows(logs)

print('\n複製完成：')
for k, v in stats.items(): print(f'  {k}: {v}')
print(f'日誌：{LOG}')
