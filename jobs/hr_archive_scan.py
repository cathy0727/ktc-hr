import csv, re, getpass, sys, unicodedata
from pathlib import Path
from datetime import datetime
import pymssql

SRC = Path.home() / 'mnt/hr_src/03_人事總務行政/【人事】'
DST = Path.home() / 'mnt/KTC_AI_Files/人事歸檔'
SKIP = {'thumbs.db', '.ds_store', 'desktop.ini'}
CERT_KW = ('證照', '證書', '訓練', '結業', 'ISO')
EXCLUDE_TOP = ('B427+0495+B495+B592 Stacy+公用(RUBY+JOY)',
               '08-系統操作_鼎新HR_EasyFlow', '契約審查技巧',
               '客戶滿意度調查表', '06-公司制度與合規管理')

env = {}
for line in (Path.home() / 'ktc_hr/.env').read_text().splitlines():
    if '=' in line and not line.strip().startswith('#'):
        k, _, v = line.partition('=')
        env[k.strip()] = v.strip()
user = env.get('DB_USER')
pwd = env.get('DB_PASS') or env.get('DB_PASSWORD')
print(f'使用 DB 帳號：{user}')
try:
    conn = pymssql.connect('192.168.0.61', user, pwd, 'HR')
    cur = conn.cursor()
    cur.execute('SELECT 工號, 姓名, 員工狀態 FROM dbo.v_EmployeeList')
    rows = cur.fetchall()
    conn.close()
except Exception as e:
    print(f'DB 連線失敗（{type(e).__name__}），檢查密碼後重跑'); sys.exit(1)

code_map, name_map = {}, {}
for c, n, s in rows:
    c, n = (c or '').strip(), (n or '').strip()
    if c: code_map[c] = (n, s or '')
    if n: name_map.setdefault(n, []).append(c)
print(f'員工名單載入：{len(code_map)} 筆（含離職）')

code_re = re.compile(r'(?<![0-9A-Za-z])(\d{4}[A-Z]?)(?![0-9])')
out_rows, stats = [], {'工號': 0, '姓名': 0, '多重': 0, '同名': 0, '未識別': 0}
n_scan = 0

for f in SRC.rglob('*'):
    if not f.is_file(): continue
    if f.name.lower() in SKIP or f.name.startswith(('~$', '.')): continue
    rel = unicodedata.normalize('NFC', str(f.relative_to(SRC)))
    n_scan += 1
    if n_scan % 1000 == 0: print(f'...已掃 {n_scan} 檔')

    if rel.split('/')[0] in EXCLUDE_TOP or '/' not in rel:
        out_rows.append([rel, f.name, f.suffix.lower(), '', '',
                         '排除', '', '', '', '', '（不搬）'])
        stats['排除'] = stats.get('排除', 0) + 1
        continue
    rel_code = re.sub(r'\d{4}[A-Z]?-\d{4}[A-Z]?', ' ', rel)
    codes = [c for c in dict.fromkeys(code_re.findall(rel_code)) if c in code_map]
    names = [n for n in name_map if len(n) >= 2 and n in rel]
    basis = emp_code = emp_name = status = ''
    if len(codes) == 1:
        emp_code = codes[0]; emp_name, status = code_map[emp_code]; basis = '工號'
    elif len(codes) > 1:
        basis = '多重'; emp_name = '/'.join(codes)
    elif len(names) == 1 and len(name_map[names[0]]) == 1:
        emp_name = names[0]; emp_code = name_map[emp_name][0]
        status = code_map[emp_code][1]; basis = '姓名'
    elif len(names) == 1:
        basis = '同名'; emp_name = names[0]
    elif len(names) > 1:
        basis = '多重'; emp_name = '/'.join(names[:5])
    else:
        basis = '未識別'
    stats['未識別' if basis == '未識別' else basis] = stats.get(basis if basis != '未識別' else '未識別', 0) + 1

    cat = '證照' if any(k in rel for k in CERT_KW) else ''
    if basis in ('工號', '姓名'):
        dest = f'依員工/{emp_code}_{emp_name}/' + (f'{cat}/' if cat else '') + f.name
    else:
        dest = f'未識別/{rel}'
    try:
        st = f.stat(); size_kb = round(st.st_size / 1024, 1)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d')
    except OSError:
        size_kb, mtime = '', ''
    out_rows.append([rel, f.name, f.suffix.lower(), size_kb, mtime,
                     basis, emp_code, emp_name, status, cat, dest])

plan = DST / '歸檔計畫.csv'
with open(plan, 'w', newline='', encoding='utf-8-sig') as fh:
    w = csv.writer(fh)
    w.writerow(['來源相對路徑', '檔名', '副檔名', '大小KB', '修改日期',
                '判定依據', '工號', '姓名', '員工狀態', '分類', '目的地'])
    w.writerows(out_rows)

print(f'\n掃描完成：共 {n_scan} 檔')
for k, v in stats.items(): print(f'  {k}: {v}')
print(f'計畫已寫入：{plan}')
