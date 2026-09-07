import os, json, pyodbc
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

BASE = os.path.dirname(__file__)
env = dict(l.strip().split("=",1) for l in open(os.path.join(BASE,".env")) if "=" in l)
CS = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={env['DB_HOST']};DATABASE={env['DB_NAME']};"
      f"UID={env['DB_USER']};PWD={env['DB_PASS']};TrustServerCertificate=yes")

app = FastAPI(docs_url=None, redoc_url=None)

def db():
    return pyodbc.connect(CS, autocommit=False)

def check_token(t, mark_used=False):
    try:
        cn = db(); cur = cn.cursor()
        cur.execute("SELECT Kind, RefId, ExpiresAt, UsedAt FROM rec.form_token WHERE Token=?", t)
        row = cur.fetchone()
        if not row or row.UsedAt or row.ExpiresAt < datetime.now():
            cn.close(); return None
        if mark_used:
            cur.execute("UPDATE rec.form_token SET UsedAt=SYSDATETIME() WHERE Token=?", t)
            cn.commit()
        out = {"kind": row.Kind, "ref": row.RefId}
        cn.close(); return out
    except Exception:
        return None

def resolve_exam_set(cur, candidate_id):
    """卷別解析：手動指定優先；否則以應徵職務比對職能關鍵字自動出卷"""
    cur.execute("""SELECT TOP 1 s.Id, s.Title, s.TimeLimitMin
                   FROM rec.interview i
                   JOIN rec.candidate c ON c.Id = i.CandidateId
                   LEFT JOIN rec.exam_set s
                     ON s.Id = i.ExamSetId
                     OR (i.ExamSetId IS NULL AND c.JobTitle LIKE '%' + s.JobFunction + '%')
                   WHERE i.CandidateId = ? AND s.Id IS NOT NULL
                   ORDER BY CASE WHEN s.Id = i.ExamSetId THEN 0 ELSE 1 END, LEN(s.JobFunction) DESC""",
                candidate_id)
    return cur.fetchone()

@app.get("/form")
def form(t: str = ""):
    if not check_token(t):
        raise HTTPException(404)
    return FileResponse(os.path.join(BASE, "static", "form.html"))

@app.get("/static/logo-img")
def logo():
    return FileResponse(os.path.join(BASE, "static", "kinetics_logo.png"))

@app.get("/api/form/meta")
def meta(t: str = ""):
    info = check_token(t)
    if not info:
        raise HTTPException(404)
    return {"kind": info["kind"]}

@app.get("/api/exam/questions")
def exam_questions(t: str = ""):
    info = check_token(t)
    if not info or info["kind"] != "exam":
        raise HTTPException(404)
    cn = db(); cur = cn.cursor()
    st = resolve_exam_set(cur, info["ref"])
    if not st:
        cn.close(); raise HTTPException(404)
    cur.execute("SELECT Id, Seq, Prompt, OptionsJson FROM rec.exam_question WHERE SetId=? ORDER BY Seq", st.Id)
    qs = [{"id": r.Id, "seq": r.Seq, "prompt": r.Prompt, "options": json.loads(r.OptionsJson)} for r in cur.fetchall()]
    cn.close()
    return {"title": st.Title, "time_limit": st.TimeLimitMin, "questions": qs}

@app.post("/api/submit")
async def submit(req: Request):
    body = await req.json()
    info = check_token(body.get("token",""), mark_used=True)
    if not info:
        raise HTTPException(404)
    try:
        cn = db(); cur = cn.cursor()
        data = body.get("data", {})
        payload = json.dumps(data, ensure_ascii=False)
        result = {"ok": True}
        if info["kind"] == "apply":
            cur.execute("UPDATE rec.interview SET FormDoneAt=SYSDATETIME(), Notes=? WHERE CandidateId=?",
                        payload, info["ref"])
        elif info["kind"] == "exam":
            answers = data.get("answers", {})
            st = resolve_exam_set(cur, info["ref"])
            score = total = 0.0
            if st:
                cur.execute("SELECT Id, Answer, Points FROM rec.exam_question WHERE SetId=?", st.Id)
                for r in cur.fetchall():
                    total += float(r.Points)
                    if str(answers.get(str(r.Id), "")).upper() == r.Answer.upper():
                        score += float(r.Points)
            cur.execute("UPDATE rec.interview SET ExamScore=?, Notes=CONCAT(ISNULL(Notes,''),CHAR(10),?) WHERE CandidateId=?",
                        score, payload, info["ref"])
            result.update({"score": score, "total": total})
        elif info["kind"] == "orient":
            cur.execute("UPDATE rec.onboard SET OrientationDoneAt=SYSDATETIME(), TrainHours=? WHERE Id=?",
                        data.get("train_hours", 0), info["ref"])
            cur.execute("INSERT INTO rec.event(EventType, EmpNo, Payload) SELECT 'orientation_done', EmpNo, ? FROM rec.onboard WHERE Id=?",
                        payload, info["ref"])
        cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES(?,?,?)",
                    "form_"+info["kind"], info["ref"] if info["kind"] != "orient" else None, "submitted")
        cn.commit(); cn.close()
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500)

# ══ 題庫上傳（暫掛 /admin/exam，之後移入 9100 走 AD 登入）══
import secrets as _secrets, tempfile, subprocess
from fastapi import UploadFile, File, Form

ADMIN_KEY = env.get("ADMIN_KEY", "")

@app.get("/admin/exam")
def admin_exam_page():
    return FileResponse(os.path.join(BASE, "static", "exam_admin.html"))

@app.post("/api/admin/exam/upload")
async def admin_exam_upload(key: str = Form(...), file: UploadFile = File(...)):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403)
    import openpyxl, json as _json
    tmp = os.path.join(BASE, "exam_import", "upload_" + _secrets.token_hex(4) + ".xlsx")
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        ws = openpyxl.load_workbook(tmp, data_only=True)["題庫"]
    except Exception:
        os.remove(tmp)
        return JSONResponse({"ok": False, "msg": "讀不到「題庫」工作表，請用範本格式"}, status_code=400)
    cn = db(); cur = cn.cursor()
    groups, skipped = {}, 0
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or not row[0]:
            continue
        jf, title, tmin, seq, prompt, a, b, c, d, ans, pts = (list(row) + [None]*11)[:11]
        opts = [str(x).strip() for x in (a, b, c, d) if x not in (None, "")]
        if not prompt or len(opts) < 2 or str(ans).strip().upper() not in "ABCD":
            skipped += 1; continue
        g = groups.setdefault(str(jf).strip(), {"title": str(title or jf).strip(),
                                                "tmin": int(tmin or 60), "qs": []})
        g["qs"].append((int(seq or len(g["qs"])+1), str(prompt).strip(),
                        _json.dumps(opts, ensure_ascii=False), str(ans).strip().upper(), float(pts or 5)))
    lines = []
    for jf, g in groups.items():
        cur.execute("SELECT Id FROM rec.exam_set WHERE JobFunction=?", jf)
        r = cur.fetchone()
        if r:
            sid = r.Id
            cur.execute("UPDATE rec.exam_set SET Title=?, TimeLimitMin=? WHERE Id=?", g["title"], g["tmin"], sid)
            cur.execute("DELETE FROM rec.exam_question WHERE SetId=?", sid)
        else:
            cur.execute("INSERT INTO rec.exam_set(JobFunction, Title, TimeLimitMin) VALUES(?,?,?)", jf, g["title"], g["tmin"])
            cur.execute("SELECT SCOPE_IDENTITY() AS Id"); sid = int(cur.fetchone()[0])
        for q in sorted(g["qs"]):
            cur.execute("INSERT INTO rec.exam_question(SetId, Seq, Prompt, OptionsJson, Answer, Points) VALUES(?,?,?,?,?,?)", sid, *q)
        lines.append(f"{jf}：{len(g['qs'])} 題（{'覆蓋' if r else '新卷'}）")
    cn.commit(); cn.close(); os.remove(tmp)
    return {"ok": True, "msg": "、".join(lines) + (f"｜略過 {skipped} 列" if skipped else "")}

@app.post("/api/admin/exam/sets")
async def admin_exam_sets(req: Request):
    body = await req.json()
    if not ADMIN_KEY or body.get("key") != ADMIN_KEY:
        raise HTTPException(403)
    cn = db(); cur = cn.cursor()
    cur.execute("""SELECT s.Id, s.JobFunction, s.Title, s.TimeLimitMin, COUNT(q.Id) AS Cnt
                   FROM rec.exam_set s LEFT JOIN rec.exam_question q ON q.SetId=s.Id
                   GROUP BY s.Id, s.JobFunction, s.Title, s.TimeLimitMin ORDER BY s.JobFunction""")
    rows = [{"id": r.Id, "jf": r.JobFunction, "title": r.Title, "tmin": r.TimeLimitMin, "cnt": r.Cnt}
            for r in cur.fetchall()]
    cn.close()
    return {"ok": True, "sets": rows}
