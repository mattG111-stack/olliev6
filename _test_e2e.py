"""End-to-end run of the journeys that have actually broken, against a database
shaped like production: the original schema, missing every table added since,
and a legacy user row with capitals in the email."""
import sys
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import inspect, text
from app.db import Base, engine, SessionLocal
import app.models as M
from app.security import hash_password

OK, BAD = [], []
def chk(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"{'  ok  ' if cond else ' FAIL '} {name}" + (f"   -- {detail}" if detail and not cond else ""))

# --- build a production-shaped database -------------------------------------
Base.metadata.create_all(engine)
with engine.begin() as c:
    for t in ("assistant_logs", "app_settings", "bug_reports", "parcel_cache",
              "building_overrides"):
        c.execute(text(f"DROP TABLE IF EXISTS {t}"))
db = SessionLocal()
for t in (M.WishList, M.VerificationCode, M.AgentContact, M.IngestJob, M.User):
    db.query(t).delete()
db.commit()
# a legacy account written with capitals and a trailing space
db.add(M.User(email="  Matt.Grant@Outlook.co.NZ ", password_hash=hash_password("legacy123"),
              full_name="Matt", role="admin", status="approved"))
db.commit(); db.close()
print("database: original schema only, 5 later tables dropped, 1 legacy user row\n")

from app.main import app
from fastapi.testclient import TestClient

with TestClient(app, raise_server_exceptions=False) as c:
    have = set(inspect(engine).get_table_names())
    chk("startup creates the missing tables",
        all(t in have for t in ("assistant_logs", "app_settings", "bug_reports",
                                "parcel_cache", "building_overrides")))

    # --- login -------------------------------------------------------------
    r = c.post("/api/auth/sign-in", data={"username": "matt.grant@outlook.co.nz",
                                          "password": "legacy123"})
    chk("legacy mixed-case account signs in", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    tok = r.json().get("access_token")
    H = {"Authorization": f"Bearer {tok}"}
    chk("wrong password still refused",
        c.post("/api/auth/sign-in", data={"username": "matt.grant@outlook.co.nz",
                                          "password": "nope"}).status_code == 401)
    chk("/api/auth/me works", c.get("/api/auth/me", headers=H).status_code == 200)

    # --- admin: users ------------------------------------------------------
    r = c.post("/api/admin/users", headers=H,
               json={"email": "staff@example.com", "password": "secret123", "full_name": "Staff"})
    chk("admin creates a user", r.status_code == 201, f"{r.status_code} {r.text[:90]}")
    uid = r.json().get("id")
    chk("that user signs in",
        c.post("/api/auth/sign-in", data={"username": "staff@example.com",
                                          "password": "secret123"}).status_code == 200)
    chk("admin sets their password",
        c.post(f"/api/admin/users/{uid}/password", headers=H,
               json={"password": "newpass456"}).status_code == 200)
    chk("the new password works",
        c.post("/api/auth/sign-in", data={"username": "staff@example.com",
                                          "password": "newpass456"}).status_code == 200)
    chk("admin edits them",
        c.patch(f"/api/admin/users/{uid}", headers=H,
                json={"full_name": "Staff Member", "role": "user"}).status_code == 200)
    chk("admin deletes them",
        c.delete(f"/api/admin/users/{uid}", headers=H).status_code == 204)
    chk("cannot delete the last admin",
        c.delete(f"/api/admin/users/{c.get('/api/auth/me', headers=H).json()['id']}",
                 headers=H).status_code == 409)

    # --- admin: assistant key ---------------------------------------------
    chk("assistant key page loads", c.get("/api/admin/assistant/key", headers=H).status_code == 200)
    chk("usage loads instead of erroring",
        c.get("/api/admin/assistant/usage", headers=H).status_code == 200)
    chk("the daily limit saves",
        c.put("/api/admin/assistant/limit", headers=H, json={"daily_limit": 20}).status_code == 200)
    import app.assistant.providers as P
    real = P.run
    P.run = lambda **kw: type("R", (), {"text": "ok", "tools_used": [], "iterations": 1, "queries": []})()
    try:
        r = c.put("/api/admin/assistant/key", headers=H,
                  json={"provider": "anthropic", "api_key": "sk-ant-" + "x" * 40, "daily_limit": 20})
        chk("the API key saves", r.status_code == 200, f"{r.status_code} {r.text[:90]}")
        chk("and never comes back in the clear", "sk-ant-x" not in r.text)
        chk("Ask Ollie answers on the shared key",
            c.post("/api/assistant", headers=H,
                   json={"question": "how many listings", "history": []}).status_code == 200)
        chk("the quota readout works", c.get("/api/assistant/quota", headers=H).status_code == 200)
    finally:
        P.run = real

    # --- bug log -----------------------------------------------------------
    chk("bug log lists", c.get("/api/admin/bugs", headers=H).status_code == 200)
    chk("a bug can be filed",
        c.post("/api/bugs", headers=H, json={"title": "smoke test report"}).status_code == 201)
    chk("a browser crash records itself",
        c.post("/api/bugs/client", headers=H, json={"message": "boom"}).status_code == 202)
    chk("the CSV exports", c.get("/api/admin/bugs/export.csv", headers=H).status_code == 200)

    # --- diagnostics + versions -------------------------------------------
    d = c.get("/api/admin/diagnostics", headers=H)
    chk("diagnostics answers", d.status_code == 200)
    chk("it reports no missing tables", d.json().get("tables_missing") == [],
        f"missing {d.json().get('tables_missing')}")
    chk("it confirms password hashing works",
        d.json()["password_hashing"]["hash"].startswith("ok"), f"{d.json()['password_hashing']}")
    chk("/api/version needs no login", c.get("/api/version").status_code == 200)

    # --- the product screens ----------------------------------------------
    for path in ("/api/dashboards/headline", "/api/dashboards/today",
                 "/api/properties/suburbs", "/api/properties?page=1",
                 "/api/dashboards/market-history"):
        chk(f"GET {path}", c.get(path, headers=H).status_code == 200,
            f"got {c.get(path, headers=H).status_code}")

print(f"\n==== {len(OK)} passed, {len(BAD)} failed ====")
if BAD:
    print("FAILED:", BAD)
    sys.exit(1)
