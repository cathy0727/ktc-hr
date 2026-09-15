#!/usr/bin/env python3
"""回填應徵者真實 Email：從 resumes/{id}_mail.html 內文抽出，
   修正被寫成 jobbank@104.com.tw / 空白的 rec.candidate.Email。
   預設 dry-run 只列清單，加 --apply 才寫入。"""
import os, re, sys, html, pathlib

HOME = pathlib.Path.home() / "ktc_hr"
RESUMES = HOME / "resumes"

def load_env():
    env = {}
    p = HOME / ".env"
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def get_conn(env):
    import pyodbc
    host = env.get("DB_HOST") or env.get("DB_SERVER") or "192.168.0.61"
    db   = env.get("DB_NAME") or env.get("DB_DATABASE") or "HRAPP"
    user = env.get("DB_USER") or "ktc_hr_rec"
    pw   = env.get("DB_PASS") or env.get("DB_PASSWORD") or ""
    cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host};"
          f"DATABASE={db};UID={user};PWD={pw};TrustServerCertificate=yes")
    return pyodbc.connect(cs)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

def extract_email(text):
    text = html.unescape(text)
    # 1) mailto 連結優先
    for m in re.finditer(r'mailto:([^"\'?&<> ]+)', text):
        addr = m.group(1)
        if "104.com.tw" not in addr and "kinetics.com.tw" not in addr:
            return addr
    # 2) 「E-mail」標籤後方 300 字內找信箱
    for m in re.finditer(r"E-?mail", text, re.I):
        seg = text[m.end():m.end()+300]
        seg = re.sub(r"<[^>]+>", " ", seg)  # 去 HTML 標籤
        em = EMAIL_RE.search(seg)
        if em and "104.com.tw" not in em.group(0) and "kinetics.com.tw" not in em.group(0):
            return em.group(0)
    return None

def main():
    apply_mode = "--apply" in sys.argv
    try:
        env = load_env()
        conn = get_conn(env)
    except Exception as e:
        print(f"❌ 連線失敗（{type(e).__name__}）：請檢查 ~/ktc_hr/.env 的 DB 設定")
        sys.exit(1)
    cur = conn.cursor()
    cur.execute("""SELECT Id, Name, Email FROM rec.candidate
                   WHERE Email LIKE '%@104.com.tw%' OR Email IS NULL OR Email = ''
                   ORDER BY Id""")
    rows = cur.fetchall()
    print(f"待處理：{len(rows)} 筆（Email 是 104 系統信箱或空白）")
    fixed, nofile, nomail = 0, 0, 0
    for cid, name, old in rows:
        f = RESUMES / f"{cid}_mail.html"
        if not f.exists():
            f2 = RESUMES / f"{cid}_resume.html"
            f = f2 if f2.exists() else None
        if f is None:
            nofile += 1
            continue
        try:
            addr = extract_email(f.read_text(errors="ignore"))
        except Exception:
            addr = None
        if not addr:
            nomail += 1
            print(f"  ⚠ {cid} {name}：內文抽不到 Email，留待人工")
            continue
        print(f"  {'✏' if apply_mode else '·'} {cid} {name}: {old or '(空)'} → {addr}")
        if apply_mode:
            cur.execute("UPDATE rec.candidate SET Email = ? WHERE Id = ?", addr, cid)
            fixed += 1
    if apply_mode:
        conn.commit()
    print(f"\n結果：可修 {fixed if apply_mode else len(rows)-nofile-nomail} 筆"
          f"｜無原信檔 {nofile} 筆｜抽不到 {nomail} 筆"
          f"｜模式：{'已寫入' if apply_mode else 'dry-run（未寫入）'}")

if __name__ == "__main__":
    main()
