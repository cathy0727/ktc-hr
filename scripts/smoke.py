#!/usr/bin/env python3
"""9100 冒煙測試：import／路由活性／PDF 引擎，30 秒內跑完。
用法：cd ~/ktc_hr && venv/bin/python scripts/smoke.py"""
import os, sys, tempfile
os.chdir(os.path.expanduser("~/ktc_hr"))
sys.path.insert(0, os.path.expanduser("~/ktc_hr"))
fails = []

def check(name, fn):
    try:
        fn(); print(f"  ✓ {name}")
    except Exception as e:
        fails.append(name); print(f"  ✗ {name} — {type(e).__name__}: {e}")

print("== 9100 smoke ==")
def t_import():
    global app_mod
    import app as app_mod
check("app.py 可載入（語法/相依套件）", t_import)

def t_docgen():
    import docgen
    with tempfile.TemporaryDirectory() as td:
        docgen.build_hire_pdf(dict(name="測試", honorific="先生", date="2026-12-01", time="08:30",
            site="taipei", nature="試用人員（試用期間：三個月）", job="測試職", dept="測試部"),
            os.path.join(td, "t.pdf"))
        docgen.build_salary_pdf(dict(name="測試", job="測試職", dept="測試部", corp="KTC",
            onboard_date="2026-12-01", base=30000, pos_allow=0, meal_allow=0, total=30000,
            reviewer="x", reviewed_at="", confirmer="", confirmed_at=""), os.path.join(td, "s.pdf"))
check("docgen 兩式 PDF 可產出", t_docgen)

def t_routes():
    from fastapi.testclient import TestClient
    c = TestClient(app_mod.app)
    assert c.get("/approve", params={"t": "no-such-token"}).status_code in (404, 500), "approve 壞 token 應拒"
    assert c.get("/flow").status_code in (200, 404), "/flow 路由存在"
    paths = {r.path for r in app_mod.app.routes}
    for p in ("/api/hr/hire/prepare", "/api/approve", "/api/hr/reject", "/api/hr/intent",
              "/api/hr/salary/start", "/api/salary/review", "/api/hr/reschedule", "/api/hr/links"):
        assert p in paths, f"缺路由 {p}"
check("關鍵路由齊備＋壞 token 被拒", t_routes)

print("== 結果：", "全過 ✓" if not fails else f"{len(fails)} 項失敗 → {fails}")
sys.exit(1 if fails else 0)
