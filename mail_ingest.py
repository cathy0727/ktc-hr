"""recruit@ 收件匣撈履歷 → rec.candidate；跑一次處理一批未讀信"""
import os, re, base64, json, pyodbc, msal, requests

BASE = os.path.dirname(os.path.abspath(__file__))
env = dict(l.strip().split("=",1) for l in open(os.path.join(BASE,".env")) if "=" in l)
CS = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={env['DB_HOST']};DATABASE={env['DB_NAME']};"
      f"UID={env['DB_USER']};PWD={env['DB_PASS']};TrustServerCertificate=yes")
MB = env["RECRUIT_MAILBOX"]
G = "https://graph.microsoft.com/v1.0"

def token():
    app = msal.ConfidentialClientApplication(
        env["GRAPH_CLIENT"],
        authority="https://login.microsoftonline.com/" + env["GRAPH_TENANT"],
        client_credential=env["GRAPH_SECRET"])
    r = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in r:
        raise SystemExit("token 取得失敗: " + r.get("error", "unknown"))
    return r["access_token"]

def parse_subject(subj, sender_name):
    job = None
    m = re.search(r"應徵.*?[「『【]?([\w\s／/]+?(?:工程師|會計|助理|專員|主任|課長|經理|技師|人員|師))[」』】]?", subj or "")
    if m: job = m.group(1).strip()
    name = None
    m = re.search(r"([\u4e00-\u9fff]{2,4})\s*(?:先生|小姐)?\s*應徵", subj or "")
    if m: name = m.group(1)
    if not name and sender_name and not re.search(r"104|人力銀行|1111", sender_name):
        name = sender_name[:20]
    return (name or (subj or "未知")[:20]), (job or "")

REPLY_PAT = re.compile(r"^(Re:|RE:|回覆[:：]|Fw:|FW:|轉寄[:：])|面試邀請")

def handle_reply(cur, m, sender):
    """邀約回信：依寄件人歸戶、判讀意向、更新狀態"""
    addr = sender.get("address", "")
    if not addr:
        return False
    cur.execute("SELECT TOP 1 Id, Name FROM rec.candidate WHERE Email=? ORDER BY Id DESC", addr)
    r = cur.fetchone()
    if not r:
        return False
    body = (m.get("bodyPreview") or "")[:300]
    text = (m.get("subject","") + " " + body)
    if re.search(r"確認|參加|出席|如約|準時|OK|ok|沒問題", text):
        stage, verdict = "已確認出席", "確認參加"
    elif re.search(r"改期|延期|調整|另約|無法|不克|取消|抱歉", text):
        stage, verdict = "需改期", "要求改期/婉拒"
    else:
        stage, verdict = "已回覆", "已回覆(內容待閱)"
    cur.execute("UPDATE rec.candidate SET Stage=?, LastContactAt=SYSDATETIME() WHERE Id=?", stage, r.Id)
    cur.execute("INSERT INTO rec.contact_log(CandidateId, Channel, Direction, Summary, Actor) VALUES(?, 'email', 'in', ?, 'system')",
                r.Id, (verdict + "｜" + body)[:490])
    cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('invite_reply', ?, ?)", r.Id, verdict)
    print(f"回覆歸戶: #{r.Id} {r.Name} → {stage}")
    return True

def main():
    tk = token()
    H = {"Authorization": "Bearer " + tk}
    r = requests.get(f"{G}/users/{MB}/mailFolders/inbox/messages",
                     params={"$filter": "isRead eq false", "$top": "20",
                             "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,bodyPreview"},
                     headers=H, timeout=30)
    r.raise_for_status()
    msgs = r.json().get("value", [])
    if not msgs:
        print("無未讀信"); return
    cn = pyodbc.connect(CS); cur = cn.cursor()
    done = 0
    for m in msgs:
        try:
            sender = (m.get("from", {}).get("emailAddress", {}) or {})
            if REPLY_PAT.search(m.get("subject","")):
                if handle_reply(cur, m, sender):
                    requests.patch(f"{G}/users/{MB}/messages/{m['id']}",
                                   json={"isRead": True}, headers={**H, "Content-Type": "application/json"}, timeout=30).raise_for_status()
                    done += 1; cn.commit(); continue
                # 對不到人的回信：標讀＋略過（不建新檔）
                requests.patch(f"{G}/users/{MB}/messages/{m['id']}",
                               json={"isRead": True}, headers={**H, "Content-Type": "application/json"}, timeout=30)
                print("回信對不到應徵者，略過"); cn.commit(); continue
            name, job = parse_subject(m.get("subject",""), sender.get("name",""))
            cur.execute("""SET NOCOUNT ON;
                           INSERT INTO rec.candidate(CorporationId, Name, JobTitle, Source, AiSummary, Email)
                           VALUES('KTC', ?, ?, '104', ?, ?);
                           SELECT CAST(SCOPE_IDENTITY() AS INT) AS Id;""",
                        name, job, (m.get("bodyPreview") or "")[:500], sender.get("address",""))
            cid = int(cur.fetchone()[0])
            cur.execute("INSERT INTO rec.interview(CandidateId) VALUES(?)", cid)
            paths = []
            if m.get("hasAttachments"):
                ra = requests.get(f"{G}/users/{MB}/messages/{m['id']}/attachments", headers=H, timeout=60)
                for a in ra.json().get("value", []):
                    if a.get("@odata.type","").endswith("fileAttachment") and a.get("name","").lower().endswith((".pdf",".doc",".docx",".jpg",".jpeg",".png")):
                        fn = str(cid) + "_" + re.sub(r'[^0-9A-Za-z\u4e00-\u9fff._-]', '_', a['name'])
                        fp = os.path.join(BASE, "resumes", fn)
                        with open(fp, "wb") as f:
                            f.write(base64.b64decode(a["contentBytes"]))
                        paths.append(fp)
            if paths:
                cur.execute("UPDATE rec.candidate SET ResumePath=? WHERE Id=?", ";".join(paths), cid)
            others = []
            for rc in (m.get("toRecipients", []) + m.get("ccRecipients", [])):
                ad = (rc.get("emailAddress", {}) or {}).get("address", "")
                if ad and ad.lower() != MB.lower() and "104.com" not in ad:
                    others.append(ad)
            if others and job:
                cur.execute("""MERGE rec.job_posting AS t
                    USING (SELECT ? AS JobTitle) AS s ON t.JobTitle = s.JobTitle
                    WHEN MATCHED AND (t.InterviewerMail IS NULL OR t.InterviewerMail='') THEN UPDATE SET InterviewerMail=?
                    WHEN NOT MATCHED THEN INSERT(CorporationId, JobTitle, InterviewerMail, CcEmails)
                         VALUES('KTC', s.JobTitle, ?, ?);""",
                    job, others[0], others[0], ",".join(others[1:]))
            cur.execute("INSERT INTO rec.event(EventType, CandidateId, Payload) VALUES('resume_received', ?, ?)",
                        cid, json.dumps({"subject": m.get("subject","")[:200],
                                         "from": sender.get("address",""),
                                         "received": m.get("receivedDateTime","")}, ensure_ascii=False))
            ack = {"message": {
                "subject": "Kinetics 已收到您的應徵資料" + (f"｜{job}" if job else ""),
                "body": {"contentType": "HTML", "content":
                    f"<div style='font-family:Microsoft JhengHei;font-size:15px;line-height:1.9'>"
                    f"<p>{name} 您好：</p><p>感謝您應徵 Kinetics 集團" + (f"「<b>{job}</b>」職務" if job else "") +
                    "，我們已收到您的履歷。<br>相關資料審閱後，將由人事單位主動與您聯繫後續安排。</p>"
                    "<p style='color:#5A6B76'>此為系統自動回覆，請勿直接回信。<br>Kinetics 集團 人事單位</p></div>"},
                "toRecipients": [{"emailAddress": {"address": sender.get("address","")}}]},
                "saveToSentItems": True}
            if sender.get("address") and "104.com" not in sender.get("address",""):
                requests.post(f"{G}/users/{MB}/sendMail",
                              json=ack, headers={**H, "Content-Type": "application/json"}, timeout=30)
            rp = requests.patch(f"{G}/users/{MB}/messages/{m['id']}",
                           json={"isRead": True}, headers={**H, "Content-Type": "application/json"}, timeout=30)
            rp.raise_for_status()
            done += 1
        except Exception as e:
            print("單封失敗:", type(e).__name__); cn.rollback(); continue
        cn.commit()
    cn.close()
    print(f"處理 {done}/{len(msgs)} 封")

if __name__ == "__main__":
    main()
