# docgen.py — 錄取通知＋聘僱書 PDF 套印（pymupdf，免外部字型）
import os, datetime
import pymupdf

BASE = os.path.dirname(os.path.abspath(__file__))
LOGO = os.path.join(BASE, "static", "kinetics_logo.png")
BAR  = os.path.join(BASE, "static", "kinetics_bar.png")
SIG  = os.path.join(BASE, "static", "jones_sig.png")

W, H = 595, 842  # A4 pt
ML, MR = 60, 535  # 左右邊界
F = pymupdf.Font("cjk")
INK = (0.12, 0.16, 0.2)
GRAY = (0.35, 0.42, 0.46)

SITE_LIST = [
    ("taipei",    "新北市汐止區新台五路一段 79 號 18 樓之 8 / 台北總公司"),
    ("kaohsiung", "高雄市仁武區京吉一路 36 號 / 高雄分公司"),
    ("huwei",     "雲林縣虎尾鎮成德街 95 號 / 虎尾分公司"),
    ("mailiao",   "雲林縣麥寮鄉西濱路二段 260-7 號 / 麥寮分公司"),
]
CONTACTS = [
    ("台北連絡人員/連絡電話：", "Stacy",  "02-2698-0688", "(分機：213)"),
    ("高雄連絡人員/連絡電話：", "DarYeh", "07-373-0626", "(分機：24)"),
    ("虎尾連絡人員/連絡電話：", "Apple",  "05-632-5597", ""),
    ("麥寮連絡人員/連絡電話：", "Dora",   "05-693-9910", ""),
]
DOCS = ["01. 離職證明", "02. 學歷證件", "03. 退伍證件 (女性免)", "04. 身分證及健保卡",
        "05. 兩吋大頭照 1 張 + 電子檔案", "06. 兆豐國際商業銀行存摺",
        "07. 勞工體檢表 (勞動部認可醫療機構)", "08. 附件_聘僱書、同意書",
        "09. 附件_員工薪資所得受領人免稅額申報表", "10. 附件_勞健保加保申請書"]
NATURES = ["任用人員", "試用人員（試用期間：三個月）", "特聘人員", "臨時性人員"]


def _tw(page):
    return pymupdf.TextWriter(page.rect, color=INK)

def _text(tw, x, y, s, size=10.5):
    tw.append((x, y), s, font=F, fontsize=size)

def _center(tw, y, s, size=16, spread=None):
    disp = (" " * 3).join(list(s)) if spread else s
    w = F.text_length(disp, fontsize=size)
    tw.append(((W - w) / 2, y), disp, font=F, fontsize=size)

def _wrap(tw, x, y, s, size=10.5, width=None, lh=19):
    width = width or (MR - x)
    line = ""
    for ch in s:
        if F.text_length(line + ch, fontsize=size) > width:
            _text(tw, x, y, line, size); y += lh; line = ch
        else:
            line += ch
    if line:
        _text(tw, x, y, line, size); y += lh
    return y

def _header(page):
    if os.path.isfile(LOGO):
        page.insert_image(pymupdf.Rect(ML, 42, ML + 118, 66), filename=LOGO, keep_proportion=True)

def _footer(page):
    if os.path.isfile(BAR):
        page.insert_image(pymupdf.Rect(MR - 90, H - 78, MR, H - 38), filename=BAR, keep_proportion=True)

def _uline(page, x0, x1, y):
    page.draw_line((x0, y), (x1, y), color=INK, width=0.7)


def _page_offer(doc, d):
    """錄取通知（第 1 頁，簽名頁）"""
    page = doc.new_page(width=W, height=H)
    _header(page); tw = _tw(page)
    _center(tw, 118, "錄取通知", 17, spread=True)

    y = 152
    nm = f"{d['name']} {d['honorific']}"
    _text(tw, ML, y, f"{nm} 您好：", 11)
    _uline(page, ML, ML + F.text_length(nm, fontsize=11) + 2, y + 3)
    y += 28
    y = _wrap(tw, ML, y,
        "歡迎加入睿普工程股份有限公司！為了使您順利完成報到手續，敬請攜帶以下證/文件，依下列規定之時間及地點，準時前來辦理報到手續：", 10.5)
    y += 8

    rd = d["date"]  # datetime.date
    roc, ampm = rd.year - 1911, ("上午" if int(d["time"][:2]) < 12 else "下午")
    line = f"A. 報到時間：民國  {roc}  年  {rd.month:02d}  月  {rd.day:02d}  日   {ampm}  {d['time']}"
    _text(tw, ML, y, line, 10.5)
    _uline(page, ML + F.text_length("A. 報到時間：民國 ", fontsize=10.5), ML + F.text_length(line, fontsize=10.5) + 4, y + 3)
    y += 24

    _text(tw, ML, y, "B. 報到地點：", 10.5)
    bx = ML + F.text_length("B. 報到地點：", fontsize=10.5)
    for key, label in SITE_LIST:
        mark = "■" if key == d["site"] else "□"
        _text(tw, bx, y, f"{mark} {label}", 10.5); y += 21
    y += 4

    _text(tw, ML, y, "C.", 10.5)
    for cap, who, tel, ext in CONTACTS:
        _text(tw, ML + 18, y, cap, 10.5)
        _text(tw, ML + 210, y, who, 10.5)
        _uline(page, ML + 208, ML + 210 + F.text_length(who, fontsize=10.5) + 4, y + 3)
        _text(tw, ML + 275, y, tel, 10.5)
        if ext: _text(tw, ML + 370, y, ext, 10.5)
        y += 21
    y += 8

    _text(tw, ML, y, "D. 繳交證件及文件 (空白表格如附件)：", 10.5); y += 21
    for i in range(5):
        _text(tw, ML + 18, y, DOCS[i], 10.5)
        _text(tw, ML + 250, y, DOCS[i + 5], 10.5)
        y += 21
    y += 6

    _text(tw, ML, y, "E. 公司簡介：", 10.5); y += 21
    y = _wrap(tw, ML + 18, y, "為了使您對公司背景、文化、經營理念、各部門職掌及功能有一個概略的認識，報到當日將安排公司介紹。", 10.5)
    y += 12
    y = _wrap(tw, ML, y,
        "茲隨函附上聘僱書(一式二份)及同意書，簽章後請於報到日交付管理部。若您有任何問題，歡迎與本公司的人力資源管理團隊聯繫。在此，謝謝您的配合及再一次竭誠歡迎您加入本公司。", 10.5)

    sy = y + 70
    _text(tw, MR - 220, sy, "總經理：", 11)
    _uline(page, MR - 165, MR - 5, sy + 3)
    tw.write_text(page)
    _footer(page)
    return page, sy  # 簽名 y 座標供蓋簽


def _pages_contract(doc, d, label):
    """聘僱書（正本/副本，各 2 頁）"""
    page = doc.new_page(width=W, height=H)
    _header(page); tw = _tw(page)
    # 版本標籤
    page.draw_rect(pymupdf.Rect(ML, 100, ML + 46, 120), color=None, fill=(0.75, 0.75, 0.75))
    tw.append((ML + 8, 114), label, font=F, fontsize=11)
    _center(tw, 118, "聘僱書", 17, spread=True)

    y = 152
    nm = f"{d['name']} {d['honorific']}"
    _text(tw, ML, y, f"{nm} 您好：", 11)
    _uline(page, ML, ML + F.text_length(nm, fontsize=11) + 2, y + 3)
    y += 28
    y = _wrap(tw, ML, y, "歡迎加入睿普工程股份有限公司！很高興地，針對您在本公司的工作、薪資、人事規章及福利，我們為您做以下概略性的說明：", 10.5)
    y += 6

    rd = d["date"]; roc = rd.year - 1911
    ind = ML + 22
    items = []
    items.append(("一、", f"到職日期：您將從民國  {roc}  年  {rd.month:02d}  月  {rd.day:02d}  日起，開始服務於本公司。"))
    nat = "".join(("☑" if n == d["nature"] else "□") + n + " " for n in NATURES)
    items.append(("二、", "聘僱性質：" + nat))
    items.append(("三、", f"職位及服務單位：您的職位為 {d['job']}；服務單位為 {d['dept']}。"))
    items.append(("四、", "工作時間：星期一 ~ 五　上午 8:30 ～ 下午 5:30"))
    items.append(("五、", "午休時間：上午 12:30 ～ 下午 1:30"))
    items.append(("六、", "全民健康保險與勞工保險：本公司將依您的月投保薪資額，提供您全民健康保險福利和勞工保險福利。"))
    items.append(("七、", "團體綜合保險：關於您本人，本公司另提供意外險及醫療險福利。"))
    items.append(("八、", "退休金：本公司依勞動基準法及相關規定提供您退休金。"))
    items.append(("九、", "特別休假：本公司依勞動基準法之特別休假相關規定，給予您特別休假。"))
    items.append(("十、", "教育訓練：本公司將提供您職等和職能方面的教育訓練課程。"))
    items.append(("十一、", "試用期間(適用於試用人員)：您將接受本公司為期三個月的試用，在這期間，本公司將針對您工作能力、績效表現與對本公司之適應情況等來進行評估，經考核合格者改支任用薪。若您未通過試用，本公司將於試用期滿前以書面通知您。"))
    items.append(("十二、", "工作規則：於服務期間，您應遵循本公司工作規則之一切規定。"))
    for no, body in items:
        _text(tw, ind, y, no, 10.5)
        y = _wrap(tw, ind + F.text_length("十一、", fontsize=10.5) + 4, y, body, 10.5)
        y += 5
    tw.write_text(page)
    _footer(page)

    # 第二頁：變更權利＋同意＋簽章欄
    page2 = doc.new_page(width=W, height=H)
    _header(page2); tw = _tw(page2)
    y = 130
    y = _wrap(tw, ML, y, "有關上述工作、薪資、人事規章及福利之說明，本公司保留視公司實際經營決策之需要而加以變更之權利。", 10.5)
    y += 14
    y = _wrap(tw, ML, y, "再一次地，歡迎加入睿普工程股份有限公司！並期盼與您一同攜手努力，為您個人未來工作生涯與本公司未來成長，共創美好的雙贏成果！為了確認您將接受此聘僱書所述之工作、薪資、人事規章及福利並成為我們的工作伙伴，請您在以下的空白處簽章並註明日期。", 10.5)
    y += 24
    page2.draw_line((ML, y), (MR, y), color=GRAY, width=0.5, dashes="[1 2] 0")
    y += 30
    y = _wrap(tw, ML, y, "我同意接受此聘僱書之一切內容並保證不將此內容透露給第三者，如有違者，我願意接受解僱之處分。", 10.5)
    sy = y + 150
    _uline(page2, ML + 10, ML + 220, sy)
    _text(tw, ML + 80, sy + 18, "簽　　　章", 10.5)
    _uline(page2, MR - 220, MR - 10, sy)
    _text(tw, MR - 160, sy + 18, "日　　　期", 10.5)
    tw.write_text(page2)
    _footer(page2)


def build_hire_pdf(data, out_path, signed=False):
    """data: name/honorific/date(ISO str)/time/site/nature/job/dept"""
    d = dict(data)
    d["date"] = datetime.date.fromisoformat(d["date"])
    doc = pymupdf.open()
    _, sig_y = _page_offer(doc, d)
    _pages_contract(doc, d, "正本")
    _pages_contract(doc, d, "副本")
    if signed and os.path.isfile(SIG):
        p = doc[0]
        p.insert_image(pymupdf.Rect(MR - 175, sig_y - 46, MR - 15, sig_y + 2), filename=SIG, keep_proportion=True)
    doc.save(out_path, deflate=True)
    doc.close()
    return out_path


def stamp_sign(src_path, out_path):
    """對既有未簽 PDF 蓋總經理簽名（第 1 頁右下簽名線上方）"""
    doc = pymupdf.open(src_path)
    p = doc[0]
    # 簽名線位置＝產件時固定寫在頁面 dict 不可得，改以掃描底部線：直接用產件同座標區間
    words = p.get_text("words")
    ty = None
    for w in words:
        if "總經理" in w[4]:
            ty = w[3]; break
    y = (ty or H - 160)
    p.insert_image(pymupdf.Rect(MR - 175, y - 46, MR - 15, y + 2), filename=SIG, keep_proportion=True)
    doc.save(out_path, deflate=True)
    doc.close()
    return out_path


def build_salary_pdf(d, out_path):
    """新進人員薪資核定單（保密文件，pay 圈專用）
    d: name/job/dept/corp/onboard_date/base/pos_allow/meal_allow/total/
       reviewer/reviewed_at/confirmer/confirmed_at"""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    _header(page); tw = _tw(page)
    _center(tw, 118, "新進人員薪資核定單", 16, spread=True)
    tw.append((MR - 90, 92), "機密文件", font=F, fontsize=10)
    page.draw_rect(pymupdf.Rect(MR - 98, 78, MR - 30, 98), color=(0.75, 0.22, 0.17), width=1)

    y = 158
    rows1 = [("姓名", d["name"]), ("公司", d.get("corp", "KTC")), ("職位", d["job"]),
             ("服務單位", d["dept"]), ("預計報到日", d.get("onboard_date") or "—")]
    for k, v in rows1:
        _text(tw, ML, y, f"{k}：", 11)
        _text(tw, ML + 90, y, str(v), 11)
        _uline(page, ML + 88, MR - 180, y + 3)
        y += 26
    y += 12

    _text(tw, ML, y, "核定薪資（月薪，新台幣元）", 11.5); y += 24
    money = [("本薪", d.get("base")), ("職務加給", d.get("pos_allow")),
             ("伙食津貼", d.get("meal_allow")), ("合計", d.get("total"))]
    for k, v in money:
        big = (k == "合計")
        _text(tw, ML + 20, y, f"{k}：", 12 if big else 11)
        amt = f"{int(v):,}" if v not in (None, "") else "—"
        _text(tw, ML + 130, y, amt, 13 if big else 11)
        _uline(page, ML + 126, ML + 280, y + 3)
        y += 30 if big else 26
    y += 20

    _text(tw, ML, y, f"部門主管核定：{d.get('reviewer','')}　{d.get('reviewed_at','')}", 10.5); y += 24
    _text(tw, ML, y, f"薪資複核確認：{d.get('confirmer','')}　{d.get('confirmed_at','')}", 10.5); y += 24
    _text(tw, ML, y, "（以上核定經系統 AD 身分驗證留痕）", 9.5); y += 50
    _uline(page, ML + 10, ML + 230, y)
    _text(tw, ML + 70, y + 18, "本人簽收", 10.5)
    _uline(page, MR - 240, MR - 10, y)
    _text(tw, MR - 170, y + 18, "日　　期", 10.5)
    tw.write_text(page)
    _footer(page)
    doc.save(out_path, deflate=True)
    doc.close()
    return out_path
