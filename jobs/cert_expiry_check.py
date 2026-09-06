"""證照效期每日檢查：D90/D60/D30 通知本人+人事、逾期週一彙總。--dry-run 只印不寄。"""
import sys, requests
from pathlib import Path
from datetime import date, timedelta
import pymssql

DRY = '--dry-run' in sys.argv
HR_CC = 'stacyweng@kinetics.com.tw'   # 人事收件人，要改跟我說
TIERS = [(90, 'D90'), (60, 'D60'), (30, 'D30')]

env = {}
for line in (Path.home() / 'ktc_hr/.env').read_text().splitlines():
    if '=' in line and not line.strip().startswith('#'):
        k, _, v = line.partition('=')
        env[k.strip()] = v.strip()

def graph_token():
    r = requests.post(
        f"https://login.microsoftonline.com/{env['GRAPH_TENANT']}/oauth2/v2.0/token",
        data={'client_id': env['GRAPH_CLIENT'], 'client_secret': env['GRAPH_SECRET'],
              'scope': 'https://graph.microsoft.com/.default',
              'grant_type': 'client_credentials'}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']

def send_mail(token, to, cc, subject, body):
    if DRY:
        print(f'  [dry-run] 收件:{to} 主旨:{subject}'); return True
    r = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{env['RECRUIT_MAILBOX']}/sendMail",
        headers={'Authorization': f'Bearer {token}'},
        json={'message': {
            'subject': subject,
            'body': {'contentType': 'HTML', 'body': body},
            'toRecipients': [{'emailAddress': {'address': to}}],
            'ccRecipients': [{'emailAddress': {'address': c}} for c in cc]}},
        timeout=30)
    return r.status_code == 202

conn = pymssql.connect('192.168.0.68', env['DB_USER'], env['DB_PASS'], 'KTC_AI')
cur = conn.cursor(as_dict=True)
hr = pymssql.connect('192.168.0.61', env['DB_USER'], env['DB_PASS'], 'HR')
hcur = hr.cursor(as_dict=True)
hcur.execute('SELECT 工號, 內部信箱 FROM dbo.v_EmployeeList')
mail_map = {r['工號'].strip(): (r['內部信箱'] or '').strip() for r in hcur.fetchall()}
hr.close()

today = date.today()
token = None
sent = {'D90': 0, 'D60': 0, 'D30': 0, 'OVERDUE': 0}

# --- 到期前分級通知 ---
for days, tier in TIERS:
    cur.execute("""
        SELECT c.cert_id, c.emp_code, c.emp_name, c.cert_name, c.expiry_date
        FROM dbo.hr_cert c
        WHERE c.status = N'有效' AND c.expiry_date IS NOT NULL
          AND c.expiry_date > %s AND c.expiry_date <= %s
          AND NOT EXISTS (SELECT 1 FROM dbo.hr_cert_notify_log l
                          WHERE l.cert_id = c.cert_id AND l.tier = %s)""",
        (today, today + timedelta(days=days), tier))
    for r in cur.fetchall():
        to = mail_map.get(r['emp_code'])
        if not to: continue
        if token is None and not DRY: token = graph_token()
        ok = send_mail(token, to, [HR_CC],
            f"【證照到期提醒】{r['cert_name']} 將於 {r['expiry_date']} 到期",
            f"<p>{r['emp_name']} 您好：</p><p>您的證照「{r['cert_name']}」"
            f"將於 <b>{r['expiry_date']}</b> 到期（{days} 天內），請安排回訓/換證，"
            f"完成後將新證提供人事更新。</p><p>-- KTC 人事系統自動通知</p>")
        if ok and not DRY:
            cur.execute('INSERT INTO dbo.hr_cert_notify_log(cert_id, tier) VALUES (%s, %s)',
                        (r['cert_id'], tier))
            conn.commit()
        sent[tier] += 1

# --- 逾期彙總（週一寄人事） ---
if today.weekday() == 0 or DRY:
    cur.execute("""SELECT emp_code, emp_name, cert_name, expiry_date
                   FROM dbo.hr_cert
                   WHERE status = N'有效' AND expiry_date < %s
                   ORDER BY expiry_date""", (today,))
    rows = cur.fetchall()
    if rows:
        lines = ''.join(f"<tr><td>{r['emp_code']}</td><td>{r['emp_name']}</td>"
                        f"<td>{r['cert_name']}</td><td>{r['expiry_date']}</td></tr>"
                        for r in rows)
        if token is None and not DRY: token = graph_token()
        send_mail(token, HR_CC, [],
            f'【證照逾期週報】共 {len(rows)} 張逾期未換證',
            f"<table border=1 cellpadding=4><tr><th>工號</th><th>姓名</th>"
            f"<th>證照</th><th>到期日</th></tr>{lines}</table>")
        sent['OVERDUE'] = len(rows)

conn.close()
print(f"檢查完成 {today}：D90={sent['D90']} D60={sent['D60']} D30={sent['D30']} 逾期彙總={sent['OVERDUE']}")
