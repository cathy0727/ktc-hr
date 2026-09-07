#!/usr/bin/env python3
"""不錄取者個資保留期到期清理（個資法§11）
每日執行：Stage=不錄取 且 RetentionDue 已過期 → 刪履歷檔＋匿名化欄位＋留刪除紀錄。
只印安全摘要，不印個資內容。"""
import os, sys, json, datetime
import pyodbc

BASE = os.path.expanduser("~/ktc_hr")
env = {}
with open(os.path.join(BASE, ".env")) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

CS = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={env['DB_HOST']};DATABASE={env['DB_NAME']};"
      f"UID={env['DB_USER']};PWD={env['DB_PASS']};TrustServerCertificate=yes")

def main():
    cn = pyodbc.connect(CS, autocommit=False)
    cur = cn.cursor()
    cur.execute("""SELECT Id, ResumePath FROM rec.candidate
                   WHERE Stage=N'不錄取' AND RetentionDue IS NOT NULL
                     AND RetentionDue < CAST(SYSDATETIME() AS date)
                     AND (Name IS NULL OR Name <> N'(逾保存期限已清除)')""")
    rows = cur.fetchall()
    purged, files_gone, file_err = 0, 0, 0
    for r in rows:
        for fp in (r.ResumePath or "").split(";"):
            fp = fp.strip()
            if fp and os.path.isfile(fp):
                try:
                    os.remove(fp); files_gone += 1
                except OSError:
                    file_err += 1
        cur.execute("""UPDATE rec.candidate
                       SET Name=N'(逾保存期限已清除)', Email=NULL, Phone=NULL,
                           AiSummary=NULL, ResumePath=NULL
                       WHERE Id=?""", r.Id)
        cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('retention_purged', ?, ?)",
                    r.Id, json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds")}))
        purged += 1
    cn.commit(); cn.close()
    print(f"[retention_purge] {datetime.date.today()} purged={purged} files_deleted={files_gone} file_errors={file_err}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[retention_purge] ERROR {type(e).__name__}")
        sys.exit(1)
