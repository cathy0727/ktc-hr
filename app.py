import os, json, secrets, pyodbc, ssl
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
import ldap3

BASE = os.path.dirname(os.path.abspath(__file__))
env = dict(l.strip().split("=",1) for l in open(os.path.join(BASE,".env")) if "=" in l)
CS = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={env['DB_HOST']};DATABASE={env['DB_NAME']};"
      f"UID={env['DB_USER']};PWD={env['DB_PASS']};TrustServerCertificate=yes")
ALLOW = {a.strip() for a in env.get("HR_ALLOW","").split(",") if a.strip()}

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=env["SECRET"], max_age=8*3600)

def db(): return pyodbc.connect(CS, autocommit=False)

def ad_login(user, pwd):
    # 公司標準：走 0.68:8700 ktc_svc 集中驗證（08 指南 §6），回傳使用者資料或 None
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(
            env["SVC_URL"] + "/auth/ad_login",
            data=_json.dumps({"username": user, "password": pwd}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": env["SVC_KEY"]},
            method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return _json.loads(r.read())
    except Exception:
        return None

def me(req: Request):
    u = req.session.get("user")
    if not u: raise HTTPException(401)
    return u

@app.get("/")
def home(req: Request):
    if not req.session.get("user"):
        return FileResponse(os.path.join(BASE,"static","login.html"))
    return FileResponse(os.path.join(BASE,"static","hr.html"))

@app.get("/static/logo-img")
def logo(): return FileResponse(os.path.join(BASE,"static","kinetics_logo.png"))

@app.post("/api/login")
async def login(req: Request):
    b = await req.json()
    u, p = b.get("user","").strip(), b.get("pwd","")
    info = ad_login(u, p) if (u and p and u in ALLOW) else None
    if not info:
        return JSONResponse({"ok": False, "msg": "帳號未授權或密碼錯誤"}, status_code=403)
    req.session["user"] = u
    req.session["name"] = info.get("name") or u
    req.session["dept"] = info.get("department") or ""
    return {"ok": True}

@app.get("/logout")
def logout(req: Request):
    req.session.clear(); return RedirectResponse("/")

@app.get("/api/hr/me")
def whoami(req: Request): return {"user": me(req)}

@app.get("/api/hr/candidates")
def candidates(req: Request):
    me(req)
    cn = db(); cur = cn.cursor()
    cur.execute("""SELECT c.Id, c.CorporationId, c.Name, c.JobTitle, c.Stage,
                          i.FormDoneAt, i.ExamScore, c.CreatedAt, c.ResumePath
                   FROM rec.candidate c LEFT JOIN rec.interview i ON i.CandidateId=c.Id
                   ORDER BY c.Id DESC""")
    rows = [{"id": r.Id, "corp": r.CorporationId, "name": r.Name, "job": r.JobTitle,
             "stage": r.Stage, "form_done": bool(r.FormDoneAt),
             "created": r.CreatedAt.strftime("%Y/%m/%d %H:%M") if r.CreatedAt else "",
             "has_resume": bool(r.ResumePath),
             "score": float(r.ExamScore) if r.ExamScore is not None else None} for r in cur.fetchall()]
    cn.close(); return {"rows": rows}

@app.post("/api/hr/candidate")
async def add_candidate(req: Request):
    u = me(req); b = await req.json()
    name, job, corp = b.get("name","").strip(), b.get("job","").strip(), b.get("corp","KTC")
    n_mail = (b.get("email") or "").strip() or None
    if not name or not job: raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("""SET NOCOUNT ON;
        INSERT INTO rec.candidate(CorporationId, Name, JobTitle, Source, Email) VALUES(?,?,?,?,?);
        SELECT CAST(SCOPE_IDENTITY() AS INT) AS Id;""", corp, name, job, "manual", n_mail)
    cid = int(cur.fetchone()[0])
    cur.execute("INSERT INTO rec.interview(CandidateId) VALUES(?)", cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('candidate_created',?,?)", cid, u)
    cn.commit(); cn.close(); return {"ok": True, "id": cid}

@app.post("/api/hr/token")
async def gen_token(req: Request):
    u = me(req); b = await req.json()
    cid, kind = int(b.get("id",0)), b.get("kind","")
    if kind not in ("apply","exam") or not cid: raise HTTPException(400)
    t = secrets.token_urlsafe(18)
    cn = db(); cur = cn.cursor()
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?,?,?,DATEADD(HOUR,8,SYSDATETIME()))",
                t, kind, cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('token_issued',?,?)", cid, kind+" by "+u)
    cn.commit(); cn.close()
    return {"ok": True, "url": f"http://192.168.0.69:9200/form?t={t}"}

@app.get("/api/hr/exam/sets")
def exam_sets(req: Request):
    me(req)
    cn = db(); cur = cn.cursor()
    cur.execute("""SELECT s.Id, s.JobFunction, s.Title, s.TimeLimitMin, COUNT(q.Id) AS Cnt
                   FROM rec.exam_set s LEFT JOIN rec.exam_question q ON q.SetId=s.Id
                   GROUP BY s.Id, s.JobFunction, s.Title, s.TimeLimitMin ORDER BY s.JobFunction""")
    rows = [{"jf": r.JobFunction, "title": r.Title, "tmin": r.TimeLimitMin, "cnt": r.Cnt} for r in cur.fetchall()]
    cn.close(); return {"sets": rows}

@app.post("/api/hr/exam/upload")
async def exam_upload(req: Request, file: UploadFile = File(...)):
    u = me(req)
    import openpyxl
    tmp = os.path.join(BASE, "up_" + secrets.token_hex(4) + ".xlsx")
    with open(tmp, "wb") as f: f.write(await file.read())
    try:
        ws = openpyxl.load_workbook(tmp, data_only=True)["題庫"]
    except Exception:
        os.remove(tmp); return JSONResponse({"ok": False, "msg": "讀不到「題庫」工作表"}, status_code=400)
    cn = db(); cur = cn.cursor()
    groups, skipped = {}, 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]: continue
        jf, title, tmin, seq, prompt, a, b, c, d, ans, pts = (list(row)+[None]*11)[:11]
        opts = [str(x).strip() for x in (a,b,c,d) if x not in (None,"")]
        if not prompt or len(opts) < 2 or str(ans).strip().upper() not in "ABCD":
            skipped += 1; continue
        g = groups.setdefault(str(jf).strip(), {"title": str(title or jf).strip(), "tmin": int(tmin or 60), "qs": []})
        g["qs"].append((int(seq or len(g["qs"])+1), str(prompt).strip(),
                        json.dumps(opts, ensure_ascii=False), str(ans).strip().upper(), float(pts or 5)))
    lines = []
    for jf, g in groups.items():
        cur.execute("SELECT Id FROM rec.exam_set WHERE JobFunction=?", jf)
        r = cur.fetchone()
        if r:
            sid = r.Id
            cur.execute("UPDATE rec.exam_set SET Title=?, TimeLimitMin=? WHERE Id=?", g["title"], g["tmin"], sid)
            cur.execute("DELETE FROM rec.exam_question WHERE SetId=?", sid)
        else:
            cur.execute("""SET NOCOUNT ON;
                INSERT INTO rec.exam_set(JobFunction, Title, TimeLimitMin) VALUES(?,?,?);
                SELECT CAST(SCOPE_IDENTITY() AS INT) AS Id;""", jf, g["title"], g["tmin"])
            sid = int(cur.fetchone()[0])
        for q in sorted(g["qs"]):
            cur.execute("INSERT INTO rec.exam_question(SetId,Seq,Prompt,OptionsJson,Answer,Points) VALUES(?,?,?,?,?,?)", sid, *q)
        lines.append(f"{jf}：{len(g['qs'])} 題（{'覆蓋' if r else '新卷'}）")
    cn.commit(); cn.close(); os.remove(tmp)
    return {"ok": True, "msg": "、".join(lines) + (f"｜略過 {skipped} 列" if skipped else "")}

# ══ 面試邀約：Teams 會議＋邀請信＋CC 用人單位 ══
import msal as _msal, requests as _rq
_G = "https://graph.microsoft.com/v1.0"

def graph_token():
    a = _msal.ConfidentialClientApplication(
        env["GRAPH_CLIENT"],
        authority="https://login.microsoftonline.com/" + env["GRAPH_TENANT"],
        client_credential=env["GRAPH_SECRET"])
    r = a.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in r:
        raise HTTPException(502, "graph token failed")
    return r["access_token"]

@app.post("/api/hr/invite")
async def invite(req: Request):
    u = me(req); b = await req.json()
    cid = int(b.get("id", 0))
    rnd = int(b.get("round", 1) or 1)
    cand_mail = (b.get("email") or "").strip()
    date, time_ = b.get("date",""), b.get("time","")
    interviewer = (b.get("interviewer") or "").strip()
    cc = [x.strip() for x in (b.get("cc") or "").split(",") if x.strip()]
    location = (b.get("location") or "本公司會議室").strip()
    if not (cid and cand_mail and date and time_):
        raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, JobTitle FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    start = f"{date}T{time_}:00"
    import datetime as _dt
    end_dt = _dt.datetime.fromisoformat(start) + _dt.timedelta(minutes=int(b.get("duration",60)))
    tk = graph_token(); MB = env["RECRUIT_MAILBOX"]
    H = {"Authorization": "Bearer " + tk, "Content-Type": "application/json"}
    attendees = [{"emailAddress": {"address": cand_mail, "name": c.Name}, "type": "required"}]
    if interviewer:
        attendees.append({"emailAddress": {"address": interviewer}, "type": "required"})
    ev = _rq.post(f"{_G}/users/{MB}/calendar/events", headers=H, timeout=30, json={
        "subject": f"{'複試邀請' if rnd >= 2 else '面試邀請'}｜{c.JobTitle}｜{c.Name}",
        "start": {"dateTime": start, "timeZone": "Taipei Standard Time"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Taipei Standard Time"},
        "location": {"displayName": location},
        "attendees": attendees,
        "isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"})
    join = ""
    if ev.status_code < 300:
        join = (ev.json().get("onlineMeeting") or {}).get("joinUrl", "")
    # 產調查表 token（面試當日 iPad 用）
    ft = secrets.token_urlsafe(18)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'apply', ?, DATEADD(DAY, 14, SYSDATETIME()))", ft, cid)
    wd = "一二三四五六日"[_dt.datetime.fromisoformat(start).weekday()]
    import base64 as _b64
    logo_b64 = _b64.b64encode(open(os.path.join(BASE, "static", "logo_mail.png"), "rb").read()).decode()
    date_disp = f"{date}(星期{wd})"
    body_html = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>{c.Name} 您好，</p>
    <p style="border-left:4px solid #1AA1A9;padding-left:12px">睿普工程誠摯地邀請您參加面試，地點及時間如下：</p>
    <p style="margin:6px 0">
    【面試職務】{c.JobTitle}<br>
    【面試日期】{date_disp}<br>
    【面試時間】{time_}<br>
    【面試地點】{location}
    {('<br>【線上會議】<a href="' + join + '">Microsoft Teams 會議連結</a>') if join else ''}
    </p>
    <p style="color:#C0392B;font-weight:700">✦煩請回覆Mail確認參加面試✦</p>
    <p>當日請攜帶身分證明文件，抵達後由人事引導以平板填寫應徵資料。<br>
    以上如有問題歡迎隨時與我聯絡，謝謝～</p>
    <div style="margin-top:22px;border-top:2px solid #ABCD03;padding-top:14px">
      <img src="cid:kineticslogo" alt="Kinetics" width="150" style="display:block"><br>
      <span style="color:#1AA1A9;font-size:12px;letter-spacing:.5px">Analyze the Impossible</span>
      <table style="font-size:12.5px;color:#5A6B76;margin-top:8px;line-height:1.8"><tr>
        <td style="padding-right:24px;vertical-align:top">
          <b style="color:#1D2088">睿普工程股份有限公司</b><br>
          Kinetics Technology Corporation<br>人事單位 招募窗口</td>
        <td style="vertical-align:top">
          M：recruit@kinetics.com.tw<br>
          T：+886-2-2698-0688　F：+886-2-2698-0686<br>
          A：新北市汐止區新台五路一段79號18樓之8（台北總公司）</td>
      </tr></table>
    </div></div>"""
    mail = {"message": {
        "subject": f"Kinetics睿普工程【{c.JobTitle}】面試邀請_{date.replace('-','')}(星期{wd}) {time_} {c.Name}",
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": cand_mail}}],
        "ccRecipients": [{"emailAddress": {"address": x}} for x in cc],
        "attachments": [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "kinetics_logo.png", "contentType": "image/png",
            "contentBytes": logo_b64, "isInline": True, "contentId": "kineticslogo"}]},
        "saveToSentItems": True}
    sm = _rq.post(f"{_G}/users/{MB}/sendMail", headers=H, json=mail, timeout=30)
    if sm.status_code >= 300:
        cn.rollback(); cn.close()
        return JSONResponse({"ok": False, "msg": "寄信失敗"}, status_code=502)
    _stage = "複試邀約中" if rnd >= 2 else "已邀約"
    cur.execute("UPDATE rec.candidate SET Email=ISNULL(Email,?), Stage=?, LastContactAt=SYSDATETIME(), ContactRounds=ISNULL(ContactRounds,0)+1 WHERE Id=?", cand_mail, _stage, cid)
    if rnd >= 2:
        cur.execute("INSERT INTO rec.interview(CandidateId) VALUES(?)", cid)
    cur.execute("UPDATE rec.interview SET InterviewerMail=?, ScheduledAt=?, TeamsUrl=? WHERE CandidateId=?",
                interviewer, start, join, cid)
    cur.execute("""MERGE rec.job_posting AS t
        USING (SELECT ? AS JobTitle) AS s ON t.JobTitle = s.JobTitle
        WHEN MATCHED THEN UPDATE SET InterviewerMail=?, CcEmails=?
        WHEN NOT MATCHED THEN INSERT(CorporationId, JobTitle, InterviewerMail, CcEmails) VALUES('KTC', s.JobTitle, ?, ?);""",
        c.JobTitle, interviewer, ",".join(cc), interviewer, ",".join(cc))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', ?, ?)",
                cid, f"面試邀請 {date} {time_} CC:{','.join(cc) or '無'}", u)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('invite_sent', ?, ?)", cid, f"round{rnd} {date} {time_}")
    cn.commit(); cn.close()
    return {"ok": True, "teams": bool(join)}

@app.get("/api/hr/invite/defaults")
def invite_defaults(id: int, req: Request):
    me(req)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Email, JobTitle FROM rec.candidate WHERE Id=?", id)
    r = cur.fetchone()
    out = {"email": (r.Email or "") if r else "", "interviewer": "", "cc": ""}
    if r and r.JobTitle:
        cur.execute("SELECT TOP 1 InterviewerMail, CcEmails FROM rec.job_posting WHERE JobTitle=? ORDER BY Id DESC", r.JobTitle)
        jp = cur.fetchone()
        if jp:
            out["interviewer"] = jp.InterviewerMail or ""
            out["cc"] = jp.CcEmails or ""
    cur.execute("""SELECT DISTINCT value AS m FROM rec.job_posting CROSS APPLY STRING_SPLIT(ISNULL(CcEmails,''), ',')
                   WHERE LTRIM(value) <> '' UNION SELECT DISTINCT InterviewerMail FROM rec.job_posting WHERE InterviewerMail IS NOT NULL""")
    out["known"] = sorted({x.m.strip() for x in cur.fetchall() if x.m and x.m.strip()})
    cn.close()
    return out


@app.get("/api/hr/resume/{cid}")
def resume(cid: int, req: Request):
    me(req)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT ResumePath, Name FROM rec.candidate WHERE Id=?", cid)
    r = cur.fetchone(); cn.close()
    if not r or not r.ResumePath:
        raise HTTPException(404)
    fp = r.ResumePath.split(";")[0]
    if not os.path.isfile(fp):
        raise HTTPException(404)
    return FileResponse(fp, media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=resume_{cid}.pdf"})

# ══ 錄取段（Phase A）：錄取通知信＋onboard token＋留痕 ══
SITE_INFO = {
    "taipei":    {"name": "台北總公司", "addr": "新北市汐止區新台五路一段79號18樓之8", "contact": "Stacy",  "tel": "02-2698-0688", "ext": "分機：213"},
    "kaohsiung": {"name": "高雄分公司", "addr": "高雄市仁武區京吉一路36號",           "contact": "DarYeh", "tel": "07-373-0626", "ext": "分機：24"},
    "huwei":     {"name": "虎尾分公司", "addr": "雲林縣虎尾鎮成德街95號",             "contact": "Apple",  "tel": "05-632-5597", "ext": ""},
    "mailiao":   {"name": "麥寮分公司", "addr": "雲林縣麥寮鄉西濱路二段260-7號",       "contact": "Dora",   "tel": "05-693-9910", "ext": ""},
}
ONBOARD_DOCS = [
    "01. 離職證明", "02. 學歷證件", "03. 退伍證件（女性免）", "04. 身分證及健保卡",
    "05. 兩吋大頭照 1 張＋電子檔案", "06. 兆豐國際商業銀行存摺",
    "07. 勞工體檢表（勞動部認可醫療機構）", "08. 附件_聘僱書、同意書",
    "09. 附件_員工薪資所得受領人免稅額申報表", "10. 附件_勞健保加保申請書",
]
HIRE_NATURES = ["任用人員", "試用人員（試用期間：三個月）", "特聘人員", "臨時性人員"]

@app.get("/api/hr/hire/defaults")
def hire_defaults(id: int, req: Request):
    me(req)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email, JobTitle, CorporationId FROM rec.candidate WHERE Id=?", id)
    r = cur.fetchone(); cn.close()
    if not r: raise HTTPException(404)
    return {"name": r.Name, "email": r.Email or "", "job": r.JobTitle or "",
            "corp": r.CorporationId or "KTC", "natures": HIRE_NATURES,
            "sites": {k: v["name"] for k, v in SITE_INFO.items()}}

@app.post("/api/hr/hire")
async def hire(req: Request):
    u = me(req); b = await req.json()
    import datetime as _dt, base64 as _b64
    cid = int(b.get("id", 0))
    date, time_ = (b.get("date") or "").strip(), (b.get("time") or "").strip()
    site = b.get("site", "")
    nature = (b.get("nature") or HIRE_NATURES[1]).strip()
    job, dept = (b.get("job") or "").strip(), (b.get("dept") or "").strip()
    honor = (b.get("honorific") or "先生").strip()
    cand_mail = (b.get("email") or "").strip()
    if not (cid and date and time_ and job and dept) or site not in SITE_INFO:
        raise HTTPException(400)
    S = SITE_INFO[site]
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    cand_mail = cand_mail or (c.Email or "").strip()
    if not cand_mail:
        cn.close(); return JSONResponse({"ok": False, "msg": "此應徵者沒有 Email，請先補上"}, status_code=400)
    rd = _dt.date.fromisoformat(date)
    wd = "一二三四五六日"[rd.weekday()]
    hh = int(time_.split(":")[0]); ampm = "上午" if hh < 12 else "下午"
    roc = f"民國 {rd.year-1911} 年 {rd.month:02d} 月 {rd.day:02d} 日"
    date_disp = f"{roc}（星期{wd}）{ampm} {time_}"
    # onboard token：先存 DB，信內連結由 .env 開關（Phase B 上線後 ONBOARD_LINK_IN_MAIL=1）
    ot = secrets.token_urlsafe(18)
    exp = _dt.datetime.combine(rd, _dt.time(23, 59)) + _dt.timedelta(days=3)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'onboard', ?, ?)", ot, cid, exp)
    onboard_url = f"http://192.168.0.69:9200/form?t={ot}"
    link_html = ""
    if env.get("ONBOARD_LINK_IN_MAIL") == "1":
        link_html = f'<p>報到當日將以平板協助您填寫資料，亦可先行預覽：<a href="{onboard_url}">線上報到表單</a></p>'
    # 附件：attach_onboard/ 整包空白表格＋inline logo
    adir = os.path.join(BASE, "attach_onboard")
    attachments, total = [], 0
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            fp = os.path.join(adir, fn)
            if not os.path.isfile(fp) or fn.startswith("."): continue
            raw = open(fp, "rb").read(); total += len(raw)
            ctype = "application/pdf" if fn.lower().endswith(".pdf") else "application/octet-stream"
            attachments.append({"@odata.type": "#microsoft.graph.fileAttachment",
                                "name": fn, "contentType": ctype,
                                "contentBytes": _b64.b64encode(raw).decode()})
    if total > 3_500_000:
        cn.rollback(); cn.close()
        return JSONResponse({"ok": False, "msg": "attach_onboard 附件合計超過 3.5MB，請壓縮後再試"}, status_code=400)
    logo_b64 = _b64.b64encode(open(os.path.join(BASE, "static", "logo_mail.png"), "rb").read()).decode()
    attachments.append({"@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "kinetics_logo.png", "contentType": "image/png",
                        "contentBytes": logo_b64, "isInline": True, "contentId": "kineticslogo"})
    docs_rows = "".join(f'<tr><td style="padding:2px 24px 2px 0">{ONBOARD_DOCS[i]}</td>'
                        f'<td style="padding:2px 0">{ONBOARD_DOCS[i+5]}</td></tr>' for i in range(5))
    body_html = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>{c.Name} {honor} 您好：</p>
    <p style="border-left:4px solid #1AA1A9;padding-left:12px">歡迎加入睿普工程股份有限公司！為了使您順利完成報到手續，敬請攜帶以下證件/文件，依下列規定之時間及地點，準時前來辦理報到手續：</p>
    <p style="margin:6px 0">
    【報到時間】{date_disp}<br>
    【報到地點】{S['addr']}／{S['name']}<br>
    【連絡人員】{S['contact']}　{S['tel']}{('　(' + S['ext'] + ')') if S['ext'] else ''}<br>
    【錄取職位】{job}｜服務單位：{dept}｜聘僱性質:{nature}</p>
    <p style="margin:10px 0 4px;font-weight:700">繳交證件及文件（空白表格如附件）：</p>
    <table style="font-size:14.5px;color:#1F2933;line-height:1.9">{docs_rows}</table>
    {link_html}
    <p style="margin-top:10px">為了使您對公司背景、文化、經營理念、各部門職掌及功能有一個概略的認識，報到當日將安排公司介紹。</p>
    <p>茲隨函附上聘僱書(一式二份)及同意書，簽章後請於報到日交付管理部。若您有任何問題，歡迎與本公司的人力資源管理團隊聯繫。在此，謝謝您的配合及再一次竭誠歡迎您加入本公司。</p>
    <div style="margin-top:22px;border-top:2px solid #ABCD03;padding-top:14px">
      <img src="cid:kineticslogo" alt="Kinetics" width="150" style="display:block"><br>
      <span style="color:#1AA1A9;font-size:12px;letter-spacing:.5px">Analyze the Impossible</span>
      <table style="font-size:12.5px;color:#5A6B76;margin-top:8px;line-height:1.8"><tr>
        <td style="padding-right:24px;vertical-align:top">
          <b style="color:#1D2088">睿普工程股份有限公司</b><br>
          Kinetics Technology Corporation<br>人事單位 招募窗口</td>
        <td style="vertical-align:top">
          M：recruit@kinetics.com.tw<br>
          T：+886-2-2698-0688　F：+886-2-2698-0686<br>
          A：新北市汐止區新台五路一段79號18樓之8（台北總公司）</td>
      </tr></table>
    </div></div>"""
    tk = graph_token(); MB = env["RECRUIT_MAILBOX"]
    H = {"Authorization": "Bearer " + tk, "Content-Type": "application/json"}
    mail = {"message": {
        "subject": f"Kinetics睿普工程【錄取通知】{c.Name}｜報到日 {rd.strftime('%Y/%m/%d')}(星期{wd}) {time_}",
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": cand_mail}}],
        "attachments": attachments},
        "saveToSentItems": True}
    sm = _rq.post(f"{_G}/users/{MB}/sendMail", headers=H, json=mail, timeout=60)
    if sm.status_code >= 300:
        cn.rollback(); cn.close()
        return JSONResponse({"ok": False, "msg": "寄信失敗"}, status_code=502)
    payload = json.dumps({"date": date, "time": time_, "site": site, "job": job, "dept": dept,
                          "nature": nature, "token": ot, "by": u}, ensure_ascii=False)
    cur.execute("UPDATE rec.candidate SET Email=ISNULL(Email,?), Stage=N'錄取', LastContactAt=SYSDATETIME() WHERE Id=?", cand_mail, cid)
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', ?, ?)",
                cid, f"錄取通知 報到 {date} {time_} @{S['name']}", u)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('hire_notice_sent', ?, ?)", cid, payload)
    cn.commit(); cn.close()
    return {"ok": True, "attached": len(attachments) - 1, "onboard_url": onboard_url}

# ══ 錄取簽核流 v2：產件 → 總經理核准蓋簽 → 自動寄新人；不錄取通知 ══
import docgen as _docgen
HIRE_DIR = os.path.join(BASE, "hire_docs")
os.makedirs(HIRE_DIR, exist_ok=True)
APPROVER_USER = env.get("APPROVER_USER", "joneschang")
APPROVER_MAIL = env.get("APPROVER_MAIL", "")
REJECT_RETENTION_DAYS = int(env.get("REJECT_RETENTION_DAYS", "365"))

def _mail_footer_html():
    return """<div style="margin-top:22px;border-top:2px solid #ABCD03;padding-top:14px">
      <img src="cid:kineticslogo" alt="Kinetics" width="150" style="display:block"><br>
      <span style="color:#1AA1A9;font-size:12px;letter-spacing:.5px">Analyze the Impossible</span>
      <table style="font-size:12.5px;color:#5A6B76;margin-top:8px;line-height:1.8"><tr>
        <td style="padding-right:24px;vertical-align:top">
          <b style="color:#1D2088">睿普工程股份有限公司</b><br>
          Kinetics Technology Corporation<br>人事單位 招募窗口</td>
        <td style="vertical-align:top">
          M：recruit@kinetics.com.tw<br>
          T：+886-2-2698-0688　F：+886-2-2698-0686<br>
          A：新北市汐止區新台五路一段79號18樓之8（台北總公司）</td>
      </tr></table></div>"""

def _logo_attachment():
    import base64 as _b64
    b = _b64.b64encode(open(os.path.join(BASE, "static", "logo_mail.png"), "rb").read()).decode()
    return {"@odata.type": "#microsoft.graph.fileAttachment", "name": "kinetics_logo.png",
            "contentType": "image/png", "contentBytes": b, "isInline": True, "contentId": "kineticslogo"}

def _file_attachment(fp, name=None):
    import base64 as _b64
    return {"@odata.type": "#microsoft.graph.fileAttachment", "name": name or os.path.basename(fp),
            "contentType": "application/pdf" if fp.lower().endswith(".pdf") else "application/octet-stream",
            "contentBytes": _b64.b64encode(open(fp, "rb").read()).decode()}

def _send_mail(to, subject, body_html, attachments, cc=None):
    tk = graph_token(); MB = env["RECRUIT_MAILBOX"]
    H = {"Authorization": "Bearer " + tk, "Content-Type": "application/json"}
    msg = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
                       "toRecipients": [{"emailAddress": {"address": to}}],
                       "attachments": attachments}, "saveToSentItems": True}
    if cc:
        msg["message"]["ccRecipients"] = [{"emailAddress": {"address": x}} for x in cc]
    r = _rq.post(f"{_G}/users/{MB}/sendMail", headers=H, json=msg, timeout=60)
    return r.status_code < 300

@app.post("/api/hr/hire/prepare")
async def hire_prepare(req: Request):
    u = me(req); b = await req.json()
    import datetime as _dt
    cid = int(b.get("id", 0))
    date, time_ = (b.get("date") or "").strip(), (b.get("time") or "").strip()
    site = b.get("site", "")
    nature = (b.get("nature") or HIRE_NATURES[1]).strip()
    job, dept = (b.get("job") or "").strip(), (b.get("dept") or "").strip()
    honor = (b.get("honorific") or "先生").strip()
    cand_mail = (b.get("email") or "").strip()
    if not (cid and date and time_ and job and dept) or site not in SITE_INFO:
        raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email, Stage FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    if env.get("SALARY_GATE", "1") != "0" and (c.Stage or "") != "薪資已核定":
        cn.close(); return JSONResponse({"ok": False, "msg": "須先完成薪資核定（目前狀態：" + (c.Stage or "—") + "）"}, status_code=400)
    cand_mail = cand_mail or (c.Email or "").strip()
    if not cand_mail:
        cn.close(); return JSONResponse({"ok": False, "msg": "此應徵者沒有 Email，請先補上"}, status_code=400)
    at = secrets.token_urlsafe(18)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'approve', ?, DATEADD(DAY, 14, SYSDATETIME()))", at, cid)
    data = {"name": c.Name, "honorific": honor, "date": date, "time": time_, "site": site,
            "nature": nature, "job": job, "dept": dept, "email": cand_mail}
    pdf = os.path.join(HIRE_DIR, f"hire_{cid}_{at[:8]}.pdf")
    try:
        _docgen.build_hire_pdf(data, pdf, signed=False)
    except Exception:
        cn.rollback(); cn.close()
        return JSONResponse({"ok": False, "msg": "PDF 產生失敗（docgen/pymupdf）"}, status_code=500)
    approve_url = f"http://192.168.0.69:9100/approve?t={at}"
    payload = json.dumps({**data, "pdf": pdf, "approve_token": at, "by": u}, ensure_ascii=False)
    cur.execute("UPDATE rec.candidate SET Email=ISNULL(Email,?), Stage=N'錄取簽核中', LastContactAt=SYSDATETIME() WHERE Id=?", cand_mail, cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('hire_doc_created', ?, ?)", cid, payload)
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'system', 'out', ?, ?)",
                cid, f"產生錄取文件 報到 {date} {time_}，送總經理核准", u)
    mailed = False
    if APPROVER_MAIL:
        body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
        <p>總經理 您好：</p>
        <p>人事單位擬錄取 <b>{c.Name}</b>（{job}／{dept}，報到日 {date} {time_}），
        錄取通知與聘僱書如附件，請核示：</p>
        <p><a href="{approve_url}" style="background:#1AA1A9;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700">預覽並核准簽署</a></p>
        <p style="color:#5A6B76;font-size:13px">核准後系統將自動套印您的簽名，並以 recruit@ 寄送錄取通知予當事人。連結 14 天內有效。</p>
        {_mail_footer_html()}</div>"""
        mailed = _send_mail(APPROVER_MAIL, f"【核准】錄取文件簽署｜{c.Name}｜{job}",
                            body, [_file_attachment(pdf), _logo_attachment()])
    cn.commit(); cn.close()
    return {"ok": True, "approve_url": approve_url, "mailed": mailed}

@app.get("/approve")
def approve_page(t: str = ""):
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Token, RefId, ExpiresAt, UsedAt FROM rec.form_token WHERE Token=? AND Kind='approve'", t)
    r = cur.fetchone(); cn.close()
    from fastapi.responses import HTMLResponse
    if not r:
        return HTMLResponse("<h3 style='font-family:sans-serif'>連結無效</h3>", status_code=404)
    if r.UsedAt:
        return HTMLResponse("<h3 style='font-family:sans-serif'>此文件已完成核准簽署 ✓</h3>")
    import datetime as _dt
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        return HTMLResponse("<h3 style='font-family:sans-serif'>連結已過期，請洽人事重新產件</h3>", status_code=410)
    html = """<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>錄取文件核准</title>
<style>body{font-family:"PingFang TC","Microsoft JhengHei",sans-serif;background:#F1F5F6;margin:0;color:#1F2933}
.top{background:#20262B;color:#fff;padding:12px 20px;border-bottom:4px solid #1AA1A9;font-weight:700}
.wrap{max-width:860px;margin:18px auto;padding:0 14px}
iframe{width:100%;height:62vh;border:1px solid #DCE6E9;border-radius:10px;background:#fff}
.bar{background:#fff;border:1px solid #DCE6E9;border-radius:10px;padding:14px;margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input{font-size:15px;padding:10px;border:1.5px solid #DCE6E9;border-radius:8px}
button{background:#2E8B57;color:#fff;border:none;border-radius:8px;font-size:15px;padding:11px 22px;cursor:pointer;font-weight:700}
#msg{padding:8px 2px;font-size:14px}.ok{color:#2E8B57}.err{color:#C0392B}</style></head><body>
<div class="top">Kinetics 錄取文件核准</div><div class="wrap">
<iframe src="/approve/pdf?t=__T__"></iframe>
<div class="bar">
<input id="u" placeholder="AD 帳號" style="width:150px" autocomplete="username">
<input id="p" type="password" placeholder="AD 密碼" style="width:170px" autocomplete="current-password">
<button onclick="go()">核准並簽署</button>
<span style="color:#5A6B76;font-size:13px">核准後系統將套印簽名並寄送錄取通知</span></div>
<div id="msg"></div></div>
<script>
async function go(){
  const m=document.getElementById('msg'); m.textContent='處理中…'; m.className='';
  const r=await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({t:'__T__',user:document.getElementById('u').value.trim(),pwd:document.getElementById('p').value})});
  const j=await r.json();
  m.textContent=j.ok?'✓ 已核准簽署，錄取通知已寄出':'✗ '+(j.msg||'失敗');
  m.className=j.ok?'ok':'err';
}
</script></body></html>"""
    return HTMLResponse(html.replace("__T__", t))

@app.get("/approve/pdf")
def approve_pdf(t: str = ""):
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT RefId, UsedAt FROM rec.form_token WHERE Token=? AND Kind='approve'", t)
    r = cur.fetchone()
    if not r: cn.close(); raise HTTPException(404)
    cur.execute("""SELECT TOP 1 Payload FROM rec.event
                   WHERE EventType='hire_doc_created' AND CandidateId=? AND Payload LIKE ?
                   ORDER BY Id DESC""", r.RefId, f'%{t}%')
    e = cur.fetchone(); cn.close()
    if not e: raise HTTPException(404)
    fp = json.loads(e.Payload)["pdf"]
    signed = fp.replace(".pdf", "_signed.pdf")
    real = signed if os.path.isfile(signed) else fp
    if not os.path.isfile(real): raise HTTPException(404)
    return FileResponse(real, media_type="application/pdf")

@app.post("/api/approve")
async def approve_do(req: Request):
    b = await req.json()
    t, user, pwd = (b.get("t") or "").strip(), (b.get("user") or "").strip(), b.get("pwd") or ""
    if not (t and user and pwd): raise HTTPException(400)
    if user.lower() != APPROVER_USER.lower():
        return JSONResponse({"ok": False, "msg": "此文件僅限總經理核准"}, status_code=403)
    if not ad_login(user, pwd):
        return JSONResponse({"ok": False, "msg": "帳號或密碼錯誤"}, status_code=403)
    import datetime as _dt
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT RefId, ExpiresAt, UsedAt FROM rec.form_token WHERE Token=? AND Kind='approve'", t)
    r = cur.fetchone()
    if not r: cn.close(); raise HTTPException(404)
    if r.UsedAt: cn.close(); return JSONResponse({"ok": False, "msg": "已核准過"}, status_code=409)
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        cn.close(); return JSONResponse({"ok": False, "msg": "連結已過期"}, status_code=410)
    cid = r.RefId
    cur.execute("""SELECT TOP 1 Payload FROM rec.event
                   WHERE EventType='hire_doc_created' AND CandidateId=? AND Payload LIKE ?
                   ORDER BY Id DESC""", cid, f'%{t}%')
    e = cur.fetchone()
    if not e: cn.close(); raise HTTPException(404)
    d = json.loads(e.Payload)
    signed = d["pdf"].replace(".pdf", "_signed.pdf")
    try:
        _docgen.stamp_sign(d["pdf"], signed)
    except Exception:
        cn.close(); return JSONResponse({"ok": False, "msg": "簽名套印失敗"}, status_code=500)
    # onboard token（Phase B 報到頁用）
    ot = secrets.token_urlsafe(18)
    rd = _dt.date.fromisoformat(d["date"])
    exp = _dt.datetime.combine(rd, _dt.time(23, 59)) + _dt.timedelta(days=3)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'onboard', ?, ?)", ot, cid, exp)
    # 寄新人：正式信 + 簽名版 PDF + 空白表格包
    S = SITE_INFO[d["site"]]
    wd = "一二三四五六日"[rd.weekday()]
    hh = int(d["time"].split(":")[0]); ampm = "上午" if hh < 12 else "下午"
    date_disp = f"民國 {rd.year-1911} 年 {rd.month:02d} 月 {rd.day:02d} 日（星期{wd}）{ampm} {d['time']}"
    docs_rows = "".join(f'<tr><td style="padding:2px 24px 2px 0">{ONBOARD_DOCS[i]}</td>'
                        f'<td style="padding:2px 0">{ONBOARD_DOCS[i+5]}</td></tr>' for i in range(5))
    link_html = ""
    if env.get("ONBOARD_LINK_IN_MAIL") == "1":
        link_html = f'<p>報到當日將以平板協助您填寫資料，亦可先行預覽：<a href="http://192.168.0.69:9200/form?t={ot}">線上報到表單</a></p>'
    body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>{d['name']} {d['honorific']} 您好：</p>
    <p style="border-left:4px solid #1AA1A9;padding-left:12px">歡迎加入睿普工程股份有限公司！為了使您順利完成報到手續，敬請攜帶以下證件/文件，依下列規定之時間及地點，準時前來辦理報到手續：</p>
    <p style="margin:6px 0">
    【報到時間】{date_disp}<br>
    【報到地點】{S['addr']}／{S['name']}<br>
    【連絡人員】{S['contact']}　{S['tel']}{('　(' + S['ext'] + ')') if S['ext'] else ''}<br>
    【錄取職位】{d['job']}｜服務單位：{d['dept']}｜聘僱性質:{d['nature']}</p>
    <p style="margin:10px 0 4px;font-weight:700">繳交證件及文件（空白表格如附件）：</p>
    <table style="font-size:14.5px;color:#1F2933;line-height:1.9">{docs_rows}</table>
    {link_html}
    <p style="margin-top:10px">為了使您對公司背景、文化、經營理念、各部門職掌及功能有一個概略的認識，報到當日將安排公司介紹。</p>
    <p>茲隨函附上錄取通知與聘僱書(一式二份)及同意書，聘僱書簽章後請於報到日交付管理部。若您有任何問題，歡迎與本公司的人力資源管理團隊聯繫。在此，謝謝您的配合及再一次竭誠歡迎您加入本公司。</p>
    {_mail_footer_html()}</div>"""
    attachments = [_file_attachment(signed, f"睿普錄取通知暨聘僱書_{d['name']}.pdf")]
    adir = os.path.join(BASE, "attach_onboard")
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            fp = os.path.join(adir, fn)
            if os.path.isfile(fp) and not fn.startswith("."):
                attachments.append(_file_attachment(fp))
    attachments.append(_logo_attachment())
    ok = _send_mail(d["email"], f"Kinetics睿普工程【錄取通知】{d['name']}｜報到日 {rd.strftime('%Y/%m/%d')}(星期{wd}) {d['time']}", body, attachments)
    if not ok:
        cn.rollback(); cn.close()
        return JSONResponse({"ok": False, "msg": "寄信失敗（文件已簽，請洽人事重寄）"}, status_code=502)
    cur.execute("UPDATE rec.form_token SET UsedAt=SYSDATETIME() WHERE Token=?", t)
    cur.execute("UPDATE rec.candidate SET Stage=N'錄取', LastContactAt=SYSDATETIME() WHERE Id=?", cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('hire_approved', ?, ?)", cid, user)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('hire_notice_sent', ?, ?)", cid,
                json.dumps({"date": d["date"], "time": d["time"], "site": d["site"], "token": ot}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', ?, ?)",
                cid, f"總經理核准，錄取通知已寄出（報到 {d['date']} {d['time']} @{S['name']}）", user)
    cn.commit(); cn.close()
    return {"ok": True}

@app.post("/api/hr/reject")
async def reject(req: Request):
    u = me(req); b = await req.json()
    cid = int(b.get("id", 0))
    if not cid: raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email, JobTitle FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    mailed = False
    if (c.Email or "").strip():
        body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
        <p>{c.Name} 您好：</p>
        <p>感謝您應徵本公司「{c.JobTitle or ''}」職務，並撥冗參與甄選程序。經審慎評估後，本次甄選未能予以進用，特此通知。</p>
        <p>您的應徵資料本公司將依個人資料保護法妥善保管，於保存期限屆滿後銷毀。日後如有適合職缺，仍歡迎您再次投遞。祝您順利覓得理想工作。</p>
        <p>睿普工程股份有限公司 人力資源管理團隊 敬上</p>
        {_mail_footer_html()}</div>"""
        mailed = _send_mail(c.Email.strip(), "Kinetics睿普工程 應徵結果通知", body, [_logo_attachment()])
        if not mailed:
            cn.close(); return JSONResponse({"ok": False, "msg": "寄信失敗"}, status_code=502)
    cur.execute("UPDATE rec.candidate SET Stage=N'不錄取', LastContactAt=SYSDATETIME(), RetentionDue=DATEADD(DAY, ?, CAST(SYSDATETIME() AS date)) WHERE Id=?",
                REJECT_RETENTION_DAYS, cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('reject_sent', ?, ?)", cid,
                json.dumps({"mailed": mailed, "by": u}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', ?, ?)",
                cid, "婉拒通知" + ("已寄出" if mailed else "（無 Email 未寄，僅更新狀態）"), u)
    cn.commit(); cn.close()
    return {"ok": True, "mailed": mailed}

# ══ v3：擬錄取意向信＋薪資核定雙關卡（pay 圈，rec 側不見數字） ══
SAL_CONFIRM_USER = env.get("SAL_CONFIRM_USER", "cathyyang")
SAL_CONFIRM_MAIL = env.get("SAL_CONFIRM_MAIL", "")
INTENT_REPLY_DAYS = int(env.get("INTENT_REPLY_DAYS", "3"))

def db_pay():
    pu, pp = env.get("DB_PAY_USER", ""), env.get("DB_PAY_PASS", "")
    if not (pu and pp):
        raise HTTPException(500, "DB_PAY_USER/DB_PAY_PASS 未設定於 .env")
    cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={env['DB_HOST']};DATABASE={env['DB_NAME']};"
          f"UID={pu};PWD={pp};TrustServerCertificate=yes")
    return pyodbc.connect(cs, autocommit=False)

@app.post("/api/hr/intent")
async def intent(req: Request):
    u = me(req); b = await req.json()
    cid = int(b.get("id", 0))
    honor = (b.get("honorific") or "先生").strip()
    cand_mail = (b.get("email") or "").strip()
    if not cid: raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email, JobTitle FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    cand_mail = cand_mail or (c.Email or "").strip()
    if not cand_mail:
        cn.close(); return JSONResponse({"ok": False, "msg": "此應徵者沒有 Email，請先補上"}, status_code=400)
    body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>{c.Name} {honor} 您好：</p>
    <p style="border-left:4px solid #1AA1A9;padding-left:12px">感謝您日前撥冗參與本公司「{c.JobTitle or ''}」職務甄選。經審慎評估，本公司擬予錄用，恭喜您！</p>
    <p>為利後續作業，敬請於 <b>{INTENT_REPLY_DAYS} 日內</b>回覆本信，確認以下事項：</p>
    <p style="margin:6px 0 6px 12px">一、是否接受本公司之錄用。<br>二、您可配合之報到日期。</p>
    <p>收到您的確認後，本公司將寄發正式錄取通知與聘僱書，並說明報到相關事宜。若有任何問題，歡迎直接回覆本信洽詢。</p>
    {_mail_footer_html()}</div>"""
    if not _send_mail(cand_mail, f"Kinetics睿普工程【錄用意向確認】{c.JobTitle or ''}｜{c.Name}", body, [_logo_attachment()]):
        cn.close(); return JSONResponse({"ok": False, "msg": "寄信失敗"}, status_code=502)
    cur.execute("UPDATE rec.candidate SET Email=ISNULL(Email,?), Stage=N'擬錄取待回覆', LastContactAt=SYSDATETIME(), ContactRounds=ISNULL(ContactRounds,0)+1 WHERE Id=?", cand_mail, cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('intent_sent', ?, ?)", cid, u)
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', N'錄用意向確認信', ?)", cid, u)
    cn.commit(); cn.close()
    return {"ok": True}

@app.post("/api/hr/salary/start")
async def salary_start(req: Request):
    u = me(req); b = await req.json()
    cid = int(b.get("id", 0))
    rv_user = (b.get("reviewer_user") or "").strip().lower()
    rv_mail = (b.get("reviewer_mail") or "").strip()
    onboard = (b.get("onboard_date") or "").strip()
    if not (cid and rv_user and rv_mail): raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, JobTitle, CorporationId FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    dept = (b.get("dept") or "").strip()
    pn = db_pay(); pc = pn.cursor()
    pc.execute("""SET NOCOUNT ON;
        INSERT INTO pay.hire_salary(CandidateId, CandName, JobTitle, Dept, CorporationId, OnboardDate, ReviewerUser, ReviewerMail, CreatedBy)
        VALUES(?,?,?,?,?,?,?,?,?);
        SELECT CAST(SCOPE_IDENTITY() AS INT) AS Id;""",
        cid, c.Name, c.JobTitle or "", dept, c.CorporationId or "KTC", onboard or None, rv_user, rv_mail, u)
    sal_id = int(pc.fetchone()[0])
    pn.commit(); pn.close()
    t = secrets.token_urlsafe(18)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'salreview', ?, DATEADD(DAY, 14, SYSDATETIME()))", t, sal_id)
    url = f"http://192.168.0.69:9100/salary/review?t={t}"
    body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>主管 您好：</p>
    <p>擬進用之新進人員 <b>{c.Name}</b>（{c.JobTitle or ''}{('／' + dept) if dept else ''}），請核定其起薪：</p>
    <p><a href="{url}" style="background:#6B46C1;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700">填寫薪資核定單</a></p>
    <p style="color:#5A6B76;font-size:13px">開啟後以您的 AD 帳號驗證身分。薪資內容僅您與薪資複核人可見。連結 14 天內有效。</p>
    {_mail_footer_html()}</div>"""
    mailed = _send_mail(rv_mail, f"【核薪】新進人員薪資核定｜{c.Name}｜{c.JobTitle or ''}", body, [_logo_attachment()])
    cur.execute("UPDATE rec.candidate SET Stage=N'薪資核定中', LastContactAt=SYSDATETIME() WHERE Id=?", cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('salary_review_started', ?, ?)", cid,
                json.dumps({"sal_id": sal_id, "reviewer": rv_user, "by": u}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'system', 'out', ?, ?)",
                cid, f"發起薪資核定（主管 {rv_user}）", u)
    cn.commit(); cn.close()
    return {"ok": True, "mailed": mailed, "review_url": url}

def _sal_token(t, kind):
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT RefId, ExpiresAt, UsedAt FROM rec.form_token WHERE Token=? AND Kind=?", t, kind)
    r = cur.fetchone(); cn.close()
    return r

_SAL_PAGE_CSS = """<style>body{font-family:"PingFang TC","Microsoft JhengHei",sans-serif;background:#F1F5F6;margin:0;color:#1F2933}
.top{background:#20262B;color:#fff;padding:12px 20px;border-bottom:4px solid #6B46C1;font-weight:700}
.wrap{max-width:640px;margin:18px auto;padding:0 14px}
.card{background:#fff;border:1px solid #DCE6E9;border-radius:10px;padding:18px;margin-bottom:12px}
.row{display:flex;gap:10px;margin:8px 0;align-items:center;flex-wrap:wrap}
.row label{width:110px;color:#5A6B76;font-size:14px}
input{font-size:15px;padding:10px;border:1.5px solid #DCE6E9;border-radius:8px;width:180px}
button{background:#6B46C1;color:#fff;border:none;border-radius:8px;font-size:15px;padding:11px 22px;cursor:pointer;font-weight:700}
#msg{padding:8px 2px;font-size:14px}.ok{color:#2E8B57}.err{color:#C0392B}
.big{font-size:18px;font-weight:700}</style>"""

@app.get("/salary/review")
def salary_review_page(t: str = ""):
    from fastapi.responses import HTMLResponse
    import datetime as _dt
    r = _sal_token(t, "salreview")
    if not r: return HTMLResponse("<h3 style='font-family:sans-serif'>連結無效</h3>", status_code=404)
    if r.UsedAt: return HTMLResponse("<h3 style='font-family:sans-serif'>此核定單已完成填寫 ✓</h3>")
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        return HTMLResponse("<h3 style='font-family:sans-serif'>連結已過期，請洽人事重新發起</h3>", status_code=410)
    pn = db_pay(); pc = pn.cursor()
    pc.execute("SELECT CandName, JobTitle, Dept, CorporationId, OnboardDate FROM pay.hire_salary WHERE Id=?", r.RefId)
    s = pc.fetchone(); pn.close()
    if not s: return HTMLResponse("<h3>資料不存在</h3>", status_code=404)
    info = f"{s.CandName}｜{s.JobTitle}{('／' + s.Dept) if s.Dept else ''}｜{s.CorporationId}" + (f"｜預計報到 {s.OnboardDate}" if s.OnboardDate else "")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>薪資核定</title>{_SAL_PAGE_CSS}</head><body>
<div class="top">Kinetics 新進人員薪資核定</div><div class="wrap">
<div class="card"><b>{info}</b></div>
<div class="card">
<div class="row"><label>本薪</label><input id="base" type="number" min="0" step="100" oninput="tot()"></div>
<div class="row"><label>職務加給</label><input id="pos" type="number" min="0" step="100" value="0" oninput="tot()"></div>
<div class="row"><label>伙食津貼</label><input id="meal" type="number" min="0" step="100" value="0" oninput="tot()"></div>
<div class="row"><label>合計（月薪）</label><span class="big" id="total">0</span> 元</div>
<div class="row"><label>備註</label><input id="cmt" style="width:340px" placeholder="選填"></div></div>
<div class="card">
<div class="row"><input id="u" placeholder="您的 AD 帳號" autocomplete="username">
<input id="p" type="password" placeholder="AD 密碼" autocomplete="current-password">
<button onclick="go()">核定送出</button></div>
<div style="color:#5A6B76;font-size:13px">送出後由薪資複核人確認，核定內容不經人事承辦。</div>
<div id="msg"></div></div></div>
<script>
function v(id){{return parseInt(document.getElementById(id).value||'0')||0}}
function tot(){{document.getElementById('total').textContent=(v('base')+v('pos')+v('meal')).toLocaleString()}}
async function go(){{
  const m=document.getElementById('msg'); m.textContent='處理中…'; m.className='';
  const r=await fetch('/api/salary/review',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{t:'{t}',user:document.getElementById('u').value.trim(),pwd:document.getElementById('p').value,
      base:v('base'),pos:v('pos'),meal:v('meal'),comment:document.getElementById('cmt').value.trim()}})}});
  const j=await r.json();
  m.textContent=j.ok?'✓ 已核定送出，將由薪資複核人確認':'✗ '+(j.msg||'失敗');
  m.className=j.ok?'ok':'err';
}}
</script></body></html>"""
    return HTMLResponse(html)

@app.post("/api/salary/review")
async def salary_review_do(req: Request):
    b = await req.json()
    t, user, pwd = (b.get("t") or "").strip(), (b.get("user") or "").strip(), b.get("pwd") or ""
    base, pos, meal = int(b.get("base") or 0), int(b.get("pos") or 0), int(b.get("meal") or 0)
    if not (t and user and pwd): raise HTTPException(400)
    if base <= 0:
        return JSONResponse({"ok": False, "msg": "本薪未填"}, status_code=400)
    import datetime as _dt
    r = _sal_token(t, "salreview")
    if not r: raise HTTPException(404)
    if r.UsedAt: return JSONResponse({"ok": False, "msg": "已填寫過"}, status_code=409)
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        return JSONResponse({"ok": False, "msg": "連結已過期"}, status_code=410)
    pn = db_pay(); pc = pn.cursor()
    pc.execute("SELECT ReviewerUser, CandidateId, CandName FROM pay.hire_salary WHERE Id=?", r.RefId)
    s = pc.fetchone()
    if not s: pn.close(); raise HTTPException(404)
    if user.lower() != (s.ReviewerUser or "").lower():
        pn.close(); return JSONResponse({"ok": False, "msg": "此核定單僅限指定主管填寫"}, status_code=403)
    if not ad_login(user, pwd):
        pn.close(); return JSONResponse({"ok": False, "msg": "帳號或密碼錯誤"}, status_code=403)
    pc.execute("""UPDATE pay.hire_salary SET BaseSalary=?, PositionAllow=?, MealAllow=?, TotalSalary=?,
                  ReviewComment=?, ReviewedAt=SYSDATETIME(), ReviewedBy=?, Status=N'待複核' WHERE Id=?""",
               base, pos, meal, base + pos + meal, (b.get("comment") or "").strip(), user, r.RefId)
    pn.commit(); pn.close()
    cn = db(); cur = cn.cursor()
    cur.execute("UPDATE rec.form_token SET UsedAt=SYSDATETIME() WHERE Token=?", t)
    ct = secrets.token_urlsafe(18)
    cur.execute("INSERT INTO rec.form_token(Token, Kind, RefId, ExpiresAt) VALUES(?, 'salconfirm', ?, DATEADD(DAY, 14, SYSDATETIME()))", ct, r.RefId)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('salary_reviewed', ?, ?)", s.CandidateId,
                json.dumps({"sal_id": r.RefId, "by": user}, ensure_ascii=False))
    cn.commit(); cn.close()
    curl = f"http://192.168.0.69:9100/salary/confirm?t={ct}"
    if SAL_CONFIRM_MAIL:
        body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
        <p>您好：</p><p>新進人員 <b>{s.CandName}</b> 之薪資已由部門主管核定，請複核確認：</p>
        <p><a href="{curl}" style="background:#1AA1A9;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700">複核薪資核定單</a></p>
        {_mail_footer_html()}</div>"""
        _send_mail(SAL_CONFIRM_MAIL, f"【複核】新進人員薪資核定｜{s.CandName}", body, [_logo_attachment()])
    return {"ok": True}

@app.get("/salary/confirm")
def salary_confirm_page(t: str = ""):
    from fastapi.responses import HTMLResponse
    import datetime as _dt
    r = _sal_token(t, "salconfirm")
    if not r: return HTMLResponse("<h3 style='font-family:sans-serif'>連結無效</h3>", status_code=404)
    if r.UsedAt: return HTMLResponse("<h3 style='font-family:sans-serif'>此核定單已完成複核 ✓</h3>")
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        return HTMLResponse("<h3 style='font-family:sans-serif'>連結已過期</h3>", status_code=410)
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>薪資複核</title>{_SAL_PAGE_CSS}</head><body>
<div class="top">Kinetics 薪資核定複核</div><div class="wrap">
<div class="card">
<div class="row"><input id="u" placeholder="您的 AD 帳號" autocomplete="username">
<input id="p" type="password" placeholder="AD 密碼" autocomplete="current-password">
<button onclick="load()">驗證並載入核定單</button></div>
<div style="color:#5A6B76;font-size:13px">薪資內容須通過身分驗證後才會顯示。</div></div>
<div class="card" id="detail" style="display:none"></div>
<div class="card" id="actions" style="display:none">
<div class="row"><button onclick="ok()">確認核定</button>
<button style="background:#5A6B76" onclick="dl()">下載核定單 PDF</button></div>
<div style="color:#5A6B76;font-size:13px">確認後看板將顯示「薪資已核定」，人事即可產生錄取文件送總經理簽核。核定單 PDF 供列印、報到日面交本人簽收。</div></div>
<div id="msg"></div></div>
<script>
function cred(){{return {{t:'{t}',user:document.getElementById('u').value.trim(),pwd:document.getElementById('p').value}}}}
async function load(){{
  const m=document.getElementById('msg'); m.textContent='驗證中…'; m.className='';
  const r=await fetch('/api/salary/confirm/view',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(cred())}});
  const j=await r.json();
  if(!j.ok){{m.textContent='✗ '+(j.msg||'失敗');m.className='err';return;}}
  m.textContent='';
  const d=j.data, f=(x)=>x==null?'—':Number(x).toLocaleString();
  document.getElementById('detail').innerHTML=
    '<b>'+d.name+'｜'+d.job+(d.dept?'／'+d.dept:'')+'｜'+d.corp+'</b>'+(d.onboard?('｜預計報到 '+d.onboard):'')+
    '<div class="row" style="margin-top:10px"><label>本薪</label>'+f(d.base)+'</div>'+
    '<div class="row"><label>職務加給</label>'+f(d.pos)+'</div>'+
    '<div class="row"><label>伙食津貼</label>'+f(d.meal)+'</div>'+
    '<div class="row"><label>合計</label><span class="big">'+f(d.total)+'</span> 元</div>'+
    (d.comment?('<div class="row"><label>備註</label>'+d.comment+'</div>'):'')+
    '<div style="color:#5A6B76;font-size:13px">主管核定：'+d.reviewer+'　'+d.reviewed_at+'</div>';
  document.getElementById('detail').style.display='block';
  document.getElementById('actions').style.display='block';
}}
async function ok(){{
  const m=document.getElementById('msg'); m.textContent='處理中…'; m.className='';
  const r=await fetch('/api/salary/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(cred())}});
  const j=await r.json();
  m.textContent=j.ok?'✓ 已確認核定':'✗ '+(j.msg||'失敗'); m.className=j.ok?'ok':'err';
}}
async function dl(){{
  const r=await fetch('/api/salary/pdf',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(cred())}});
  if(!r.ok){{document.getElementById('msg').textContent='✗ 下載失敗';return;}}
  const b=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download='薪資核定單.pdf'; a.click();
}}
</script></body></html>"""
    return HTMLResponse(html)

def _sal_confirm_auth(t, user, pwd):
    import datetime as _dt
    r = _sal_token(t, "salconfirm")
    if not r: return None, JSONResponse({"ok": False, "msg": "連結無效"}, status_code=404)
    if r.ExpiresAt and r.ExpiresAt < _dt.datetime.now():
        return None, JSONResponse({"ok": False, "msg": "連結已過期"}, status_code=410)
    if user.lower() != SAL_CONFIRM_USER.lower():
        return None, JSONResponse({"ok": False, "msg": "僅限薪資複核人操作"}, status_code=403)
    if not ad_login(user, pwd):
        return None, JSONResponse({"ok": False, "msg": "帳號或密碼錯誤"}, status_code=403)
    return r, None

def _sal_row(sal_id):
    pn = db_pay(); pc = pn.cursor()
    pc.execute("""SELECT CandidateId, CandName, JobTitle, Dept, CorporationId, OnboardDate, BaseSalary, PositionAllow,
                  MealAllow, TotalSalary, ReviewComment, ReviewedAt, ReviewedBy, ConfirmedAt, ConfirmedBy, Status
                  FROM pay.hire_salary WHERE Id=?""", sal_id)
    s = pc.fetchone(); pn.close()
    return s

@app.post("/api/salary/confirm/view")
async def salary_confirm_view(req: Request):
    b = await req.json()
    r, err = _sal_confirm_auth((b.get("t") or "").strip(), (b.get("user") or "").strip(), b.get("pwd") or "")
    if err: return err
    s = _sal_row(r.RefId)
    if not s: raise HTTPException(404)
    return {"ok": True, "data": {"name": s.CandName, "job": s.JobTitle, "dept": s.Dept, "corp": s.CorporationId,
            "onboard": str(s.OnboardDate or ""), "base": s.BaseSalary, "pos": s.PositionAllow, "meal": s.MealAllow,
            "total": s.TotalSalary, "comment": s.ReviewComment or "",
            "reviewer": s.ReviewedBy or "", "reviewed_at": str(s.ReviewedAt or "")[:16]}}

@app.post("/api/salary/confirm")
async def salary_confirm_do(req: Request):
    b = await req.json()
    user = (b.get("user") or "").strip()
    r, err = _sal_confirm_auth((b.get("t") or "").strip(), user, b.get("pwd") or "")
    if err: return err
    if r.UsedAt: return JSONResponse({"ok": False, "msg": "已複核過"}, status_code=409)
    s = _sal_row(r.RefId)
    if not s: raise HTTPException(404)
    pn = db_pay(); pc = pn.cursor()
    pc.execute("UPDATE pay.hire_salary SET ConfirmedAt=SYSDATETIME(), ConfirmedBy=?, Status=N'已核定' WHERE Id=?", user, r.RefId)
    pn.commit(); pn.close()
    cn = db(); cur = cn.cursor()
    cur.execute("UPDATE rec.form_token SET UsedAt=SYSDATETIME() WHERE Token=?", (b.get("t") or "").strip())
    cur.execute("UPDATE rec.candidate SET Stage=N'薪資已核定', LastContactAt=SYSDATETIME() WHERE Id=?", s.CandidateId)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('salary_confirmed', ?, ?)", s.CandidateId,
                json.dumps({"sal_id": r.RefId, "by": user}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'system', 'in', N'薪資核定完成', ?)",
                s.CandidateId, user)
    cn.commit(); cn.close()
    return {"ok": True}

@app.post("/api/salary/pdf")
async def salary_pdf(req: Request):
    b = await req.json()
    r, err = _sal_confirm_auth((b.get("t") or "").strip(), (b.get("user") or "").strip(), b.get("pwd") or "")
    if err: return err
    s = _sal_row(r.RefId)
    if not s: raise HTTPException(404)
    fp = os.path.join(HIRE_DIR, f"salary_{r.RefId}.pdf")
    _docgen.build_salary_pdf(dict(name=s.CandName, job=s.JobTitle, dept=s.Dept, corp=s.CorporationId,
        onboard_date=str(s.OnboardDate or ""), base=s.BaseSalary, pos_allow=s.PositionAllow,
        meal_allow=s.MealAllow, total=s.TotalSalary,
        reviewer=s.ReviewedBy or "", reviewed_at=str(s.ReviewedAt or "")[:16],
        confirmer=s.ConfirmedBy or "", confirmed_at=str(s.ConfirmedAt or "")[:16]), fp)
    return FileResponse(fp, media_type="application/pdf", filename=f"薪資核定單_{s.CandName}.pdf")

@app.post("/api/hr/salary/offline")
async def salary_offline(req: Request):
    """線下核薪：主管以 email 回覆核定、人事轉寄存查後，僅推進狀態，不寫任何薪資數字"""
    u = me(req); b = await req.json()
    cid = int(b.get("id", 0))
    note = (b.get("note") or "").strip()
    if not cid: raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Stage FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    cur.execute("UPDATE rec.candidate SET Stage=N'薪資已核定', LastContactAt=SYSDATETIME() WHERE Id=?", cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('salary_offline_confirmed', ?, ?)", cid,
                json.dumps({"by": u, "note": note}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'system', 'in', ?, ?)",
                cid, "薪資線下核定完成" + (f"（{note}）" if note else "（email 存查）"), u)
    cn.commit(); cn.close()
    return {"ok": True}

# ══ A.1 補強：報到改期（輕量版）／連結重取／流程說明頁 ══
def _latest_event(cur, cid, etype):
    cur.execute("SELECT TOP 1 Payload FROM rec.event WHERE EventType=? AND CandidateId=? ORDER BY Id DESC", etype, cid)
    r = cur.fetchone()
    return json.loads(r.Payload) if r and r.Payload and r.Payload.startswith("{") else None

@app.post("/api/hr/reschedule")
async def reschedule(req: Request):
    u = me(req); b = await req.json()
    import datetime as _dt
    cid = int(b.get("id", 0))
    nd, nt, ns = (b.get("date") or "").strip(), (b.get("time") or "").strip(), b.get("site", "")
    if not (cid and nd and nt) or ns not in SITE_INFO: raise HTTPException(400)
    cn = db(); cur = cn.cursor()
    cur.execute("SELECT Name, Email, Stage FROM rec.candidate WHERE Id=?", cid)
    c = cur.fetchone()
    if not c: cn.close(); raise HTTPException(404)
    if (c.Stage or "") != "錄取":
        cn.close(); return JSONResponse({"ok": False, "msg": "僅「錄取」狀態可改期"}, status_code=400)
    d0 = _latest_event(cur, cid, "hire_doc_created") or {}
    n0 = _latest_event(cur, cid, "hire_notice_sent") or {}
    honor = d0.get("honorific", "先生")
    old_disp = f"{n0.get('date','')} {n0.get('time','')}" if n0 else "（原通知）"
    rd = _dt.date.fromisoformat(nd)
    wd = "一二三四五六日"[rd.weekday()]
    ampm = "上午" if int(nt[:2]) < 12 else "下午"
    date_disp = f"民國 {rd.year-1911} 年 {rd.month:02d} 月 {rd.day:02d} 日（星期{wd}）{ampm} {nt}"
    S = SITE_INFO[ns]
    body = f"""<div style="font-family:'Microsoft JhengHei','PingFang TC',sans-serif;font-size:15px;color:#1F2933;line-height:2">
    <p>{c.Name} {honor} 您好：</p>
    <p style="border-left:4px solid #B7791F;padding-left:12px">先前通知之報到時間（{old_disp}）因故調整，造成不便敬請見諒。調整後報到資訊如下：</p>
    <p style="margin:6px 0">
    【報到時間】{date_disp}<br>
    【報到地點】{S['addr']}／{S['name']}<br>
    【連絡人員】{S['contact']}　{S['tel']}{('　(' + S['ext'] + ')') if S['ext'] else ''}</p>
    <p>原錄取通知所列繳交證件及文件與其他事項均不變。若時間仍有困難，歡迎直接回覆本信與我們協調。</p>
    {_mail_footer_html()}</div>"""
    if not _send_mail((c.Email or "").strip(), f"Kinetics睿普工程【報到日期變更通知】{c.Name}｜新報到日 {rd.strftime('%Y/%m/%d')}(星期{wd}) {nt}",
                      body, [_logo_attachment()]):
        cn.close(); return JSONResponse({"ok": False, "msg": "寄信失敗"}, status_code=502)
    ot = n0.get("token")
    if ot:
        exp = _dt.datetime.combine(rd, _dt.time(23, 59)) + _dt.timedelta(days=3)
        cur.execute("UPDATE rec.form_token SET ExpiresAt=? WHERE Token=? AND Kind='onboard'", exp, ot)
    cur.execute("UPDATE rec.candidate SET LastContactAt=SYSDATETIME() WHERE Id=?", cid)
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('onboard_rescheduled', ?, ?)", cid,
                json.dumps({"old": {"date": n0.get("date"), "time": n0.get("time"), "site": n0.get("site")},
                            "new": {"date": nd, "time": nt, "site": ns}, "by": u}, ensure_ascii=False))
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'out', ?, ?)",
                cid, f"報到改期通知 {old_disp} → {nd} {nt} @{S['name']}", u)
    cn.commit(); cn.close()
    return {"ok": True}

@app.get("/api/hr/links")
def hr_links(id: int, req: Request):
    """重取簽核/核薪連結（免翻信、免撈 SQL）"""
    me(req)
    cn = db(); cur = cn.cursor()
    out = {}
    cur.execute("""SELECT TOP 1 Token FROM rec.form_token
                   WHERE Kind='approve' AND RefId=? AND UsedAt IS NULL AND ExpiresAt > SYSDATETIME()
                   ORDER BY CreatedAt DESC""", id)
    r = cur.fetchone()
    if r: out["approve"] = f"http://192.168.0.69:9100/approve?t={r.Token}"
    ev = _latest_event(cur, id, "salary_review_started")
    if ev and ev.get("sal_id"):
        for kind, path in (("salreview", "salary/review"), ("salconfirm", "salary/confirm")):
            cur.execute("""SELECT TOP 1 Token FROM rec.form_token
                           WHERE Kind=? AND RefId=? AND UsedAt IS NULL AND ExpiresAt > SYSDATETIME()
                           ORDER BY CreatedAt DESC""", kind, ev["sal_id"])
            r = cur.fetchone()
            if r: out[kind] = f"http://192.168.0.69:9100/{path}?t={r.Token}"
    cn.close()
    return {"ok": True, "links": out}

@app.get("/flow")
def flow_page():
    fp = os.path.join(BASE, "static", "hr_flow.html")
    if not os.path.isfile(fp): raise HTTPException(404)
    return FileResponse(fp, media_type="text/html")
