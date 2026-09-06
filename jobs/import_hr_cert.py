#!/usr/bin/env python3
"""證照效期表.csv 基準匯入 KTC_AI.dbo.hr_cert（verified=0, source=csv_baseline）
v2: 狀態=僅參考日期者 expiry_date 留空、日期存 note；只有真到期日才進 expiry_date。
用法: python3 import_hr_cert.py [--dry-run] [csv路徑]
重跑安全：實跑前先清掉 source='csv_baseline' 且 verified=0 的舊列。"""
import sys, os, re, csv
from datetime import date

CSV_DEFAULT = os.path.expanduser("~/mnt/KTC_AI_Files/人事歸檔/證照效期表.csv")
ENV_PATH = os.path.expanduser("~/ktc_hr/.env")
SOURCE_TAG = "csv_baseline"

def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def pick_env(env, candidates, label):
    for k in candidates:
        if env.get(k):
            return k, env[k]
    print(f"[中止] .env 找不到{label}鍵，現有鍵名：{', '.join(sorted(env))}")
    sys.exit(1)

def parse_date(s):
    s = str(s or "").strip()
    if not s:
        return None
    t = re.sub(r"[年月/.\-]", "-", s).replace("日", "").strip("-")
    m = re.match(r"^(\d{2,4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    elif re.match(r"^\d{7}$", t):
        y, mo, d = int(t[:3]), int(t[3:5]), int(t[5:7])
    elif re.match(r"^\d{8}$", t):
        y, mo, d = int(t[:4]), int(t[4:6]), int(t[6:8])
    else:
        return None
    if y < 1911:
        y += 1911  # 民國年
    try:
        return date(y, mo, d)
    except ValueError:
        return None

def pick_col(headers, keywords):
    for kw in keywords:
        for h in headers:
            if kw in h:
                return h
    return None

def main():
    dry = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    csv_path = args[0] if args else CSV_DEFAULT

    try:
        raw = open(csv_path, "rb").read()
    except OSError as e:
        print(f"[中止] 讀不到 CSV：{type(e).__name__}")
        sys.exit(1)
    for enc in ("utf-8-sig", "cp950", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("[中止] CSV 編碼無法辨識")
        sys.exit(1)

    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        print("[中止] CSV 無資料列")
        sys.exit(1)
    headers = list(rows[0].keys())

    colmap = {
        "emp_code":    pick_col(headers, ["工號"]),
        "emp_name":    pick_col(headers, ["姓名"]),
        "cert_name":   pick_col(headers, ["證照名稱", "證照", "檔名", "文件"]),
        "category":    pick_col(headers, ["類別", "分類"]),
        "issue_date":  pick_col(headers, ["發證", "發照", "取得"]),
        "expiry_date": pick_col(headers, ["到期", "有效期"]),
        "file_path":   pick_col(headers, ["路徑"]),
        "status":      pick_col(headers, ["狀態"]),
    }
    print("== 欄位對應（CSV 欄 → hr_cert 欄）==")
    for k, v in colmap.items():
        print(f"  {k:<12} <- {v if v else '(無，將留空)'}")
    if not colmap["emp_code"] or not colmap["cert_name"]:
        print("[中止] 工號或證照名稱欄對不到，請確認 CSV 表頭")
        sys.exit(1)

    records, skipped = [], 0
    n_real = n_ref = n_expired = 0
    today = date.today()
    for r in rows:
        emp = (r.get(colmap["emp_code"]) or "").strip()
        cert = (r.get(colmap["cert_name"]) or "").strip()
        if not emp or not cert:
            skipped += 1
            continue
        g = lambda key: (r.get(colmap[key]) or "").strip() if colmap[key] else ""
        st = g("status")
        idt = parse_date(g("issue_date"))
        edt = parse_date(g("expiry_date"))
        note = ""
        if edt and "僅參考" in st:
            note = f"僅參考日期 {edt.isoformat()}"
            edt = None
            n_ref += 1
        elif edt:
            n_real += 1
            n_expired += edt < today
        records.append((emp, g("emp_name"), cert, g("category"),
                        idt, edt, g("file_path"), st, SOURCE_TAG, 0, note))

    print(f"\n== 摘要 ==\n讀入 {len(rows)} 列 / 可匯入 {len(records)} / 跳過(缺工號或證照名) {skipped}")
    print(f"真到期日 {n_real}（已逾期 {n_expired}）/ 僅參考轉 note {n_ref} / 無日期 {len(records)-n_real-n_ref}")
    for s in records[:3]:
        print(f"  例: {s[0]} {s[2][:30]} 到期={s[5]} note={s[10][:24]}")

    if dry:
        print("\n[dry-run] 未寫入 DB")
        return

    env = load_env(ENV_PATH)
    _, server = pick_env(env, ["DB_SERVER", "DB_HOST", "SQL_SERVER"], "伺服器")
    uk, user = pick_env(env, ["DB_USER", "DB_UID", "SQL_USER"], "帳號")
    _, pwd = pick_env(env, ["DB_PASS", "DB_PASSWORD", "DB_PWD"], "密碼")
    print(f"\n連線 {server}/KTC_AI（帳號鍵 {uk}）")
    try:
        import pyodbc
        conn = None
        for drv in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            try:
                conn = pyodbc.connect(
                    f"DRIVER={{{drv}}};SERVER={server};DATABASE=KTC_AI;"
                    f"UID={user};PWD={pwd};TrustServerCertificate=yes", timeout=10)
                break
            except pyodbc.Error:
                continue
        if conn is None:
            print("[錯誤] 兩個 ODBC driver 都連不上，檢查 .env 與網路")
            sys.exit(1)
        cur = conn.cursor()
        cur.execute("DELETE FROM dbo.hr_cert WHERE source=? AND verified=0", SOURCE_TAG)
        cleared = cur.rowcount
        cur.executemany(
            "INSERT INTO dbo.hr_cert (emp_code, emp_name, cert_name, category,"
            " issue_date, expiry_date, file_path, status, source, verified, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", records)
        conn.commit()
        print(f"完成：清除舊基準 {cleared} 列、寫入 {len(records)} 列（verified=0）")
    except Exception as e:
        print(f"[錯誤] {type(e).__name__}：寫入失敗，DB 未提交")
        sys.exit(1)

if __name__ == "__main__":
    main()
