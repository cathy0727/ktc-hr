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
    subj = subj or ""
    job = None
    m = re.search(r"【(.+?)】", subj)   # 104 制式：【職務】整串原樣，不切地區尾綴
    if not m:
        m = re.search(r"應徵.*?[「『]?([\w\s／/\-]+?(?:工程師|會計|助理|專員|主任|課長|經理|技師|人員|師))[」』]?", subj)
    if m: job = m.group(1).strip()
    name = None
    # 104 制式主旨「104應徵履歷【職務】姓名(居住地)」→ 取】後、括號前的姓名
    m = re.search(r"[】\]]\s*([\u4e00-\u9fff·]{2,10})\s*[（(]", subj)
    if m: name = m.group(1)
    if not name:
        m = re.search(r"([\u4e00-\u9fff]{2,4})\s*(?:先生|小姐)?\s*應徵", subj)
        if m: name = m.group(1)
    if not name and sender_name and not re.search(r"104|人力銀行|1111", sender_name):
        # 104 寄件者顯示名長相「王小明應徵履歷」→ 去尾綴再用
        cand = re.sub(r"(應徵履歷|的?履歷|應徵)$", "", sender_name.strip())
        if cand: name = cand[:20]
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

def extract_links(body_html):
    """抽信內 104 連結（邀約鈕、履歷頁等），去重取前 10 條"""
    links = []
    for u in re.findall(r'href="(https?://[^"]+)"', body_html or ""):
        if "104.com" in u and u not in links:
            links.append(u)
    return links[:10]

def save_mail(cid, m):
    """存整封 HTML body 到 resumes/{cid}_mail.html（頁首加信件摘要與 104 連結），回傳路徑"""
    from html import escape
    body = ((m.get("body") or {}).get("content") or "")
    sender = (m.get("from", {}).get("emailAddress", {}) or {})
    links = extract_links(body)
    link_html = "".join(f'<li><a href="{escape(u)}" target="_blank">{escape(u[:90])}</a></li>' for u in links)
    banner = ("<div style='font-family:Microsoft JhengHei;background:#F4F7F8;border-bottom:2px solid #0E7A81;"
              "padding:10px 14px;font-size:13px;color:#3A4A55'>"
              f"<b>主旨：</b>{escape(m.get('subject') or '')}<br>"
              f"<b>寄件者：</b>{escape(sender.get('name',''))} &lt;{escape(sender.get('address',''))}&gt;　"
              f"<b>收件：</b>{escape((m.get('receivedDateTime') or '')[:16].replace('T',' '))}"
              + (f"<br><b>信內 104 連結：</b><ul style='margin:4px 0 0 18px'>{link_html}</ul>" if links else "")
              + "</div>\n")
    os.makedirs(os.path.join(BASE, "resumes"), exist_ok=True)
    fp = os.path.join(BASE, "resumes", f"{cid}_mail.html")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(banner + body)
    return fp

def refetch(cid):
    """一次性回補：對已入庫的應徵者，回收件匣（含已讀）找原信重存 body → MailPath"""
    tk = token(); H = {"Authorization": "Bearer " + tk}
    cn = pyodbc.connect(CS); cur = cn.cursor()
    cur.execute("SELECT Name FROM rec.candidate WHERE Id=?", cid)
    row = cur.fetchone()
    if not row: raise SystemExit(f"查無 {cid} 號應徵者")
    subj = None
    cur.execute("SELECT TOP 1 Payload FROM rec.event WHERE CandidateId=? AND EventType='resume_received' ORDER BY Id DESC", cid)
    ev = cur.fetchone()
    if ev and ev.Payload:
        try: subj = json.loads(ev.Payload).get("subject")
        except Exception: pass
    r = requests.get(f"{G}/users/{MB}/mailFolders/inbox/messages",
                     params={"$top": "50", "$orderby": "receivedDateTime desc",
                             "$select": "id,subject,receivedDateTime"},
                     headers=H, timeout=30)
    r.raise_for_status()
    hit = None
    for m in r.json().get("value", []):
        ms = m.get("subject", "")
        if (subj and ms[:60] == subj[:60]) or (not subj and row.Name and row.Name in ms):
            hit = m; break
    if not hit:
        raise SystemExit("收件匣最近 50 封找不到對應信（主旨比對失敗）")
    rm = requests.get(f"{G}/users/{MB}/messages/{hit['id']}",
                      params={"$select": "id,subject,from,receivedDateTime,body"},
                      headers=H, timeout=60)
    rm.raise_for_status()
    fp = save_mail(cid, rm.json())
    cur.execute("UPDATE rec.candidate SET MailPath=? WHERE Id=?", fp, cid)
    cn.commit(); cn.close()
    print(f"回補完成: #{cid} → {fp}")

def main():
    tk = token()
    H = {"Authorization": "Bearer " + tk}
    r = requests.get(f"{G}/users/{MB}/mailFolders/inbox/messages",
                     params={"$filter": "isRead eq false", "$top": "20",
                             "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,bodyPreview,body"},
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
            cur.execute("UPDATE rec.candidate SET MailPath=? WHERE Id=?", save_mail(cid, m), cid)
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
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--refetch":
        refetch(int(sys.argv[2]))
    else:
        main()
