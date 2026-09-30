#!/usr/bin/env python3
"""End-to-end test of the complete GramVet vaccine inventory flow.

Run:  python test_e2e_vaccine_flow.py

It copies the project to a temp folder and starts the REAL app (`python app.py`) there, with its own
throw-away database and MongoDB switched off, then drives it over real HTTP: real signups, animals,
symptom reports (ML prediction + vet assignment), vaccinations, shipments, server restarts and
concurrent requests. Your gramvet.db and your MongoDB are never touched.
Requires: requests (already in requirements.txt's dependency tree).
"""
import concurrent.futures as cf
import os, shutil, signal, socket, sqlite3, subprocess, sys, tempfile, threading, time
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SYMPTOMS = ["Fever / High Body Temperature", "Cough", "Nasal Discharge"]
CODES = ("FMD", "HS", "LSD", "BQ")
RESULTS = []          # (item, description, passed)


def check(item, desc, cond, detail=""):
    RESULTS.append((item, desc, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  [{item}] {desc}" + (f"    <-- {detail}" if not cond and detail else ""))


def section(t):
    print(f"\n=== {t} ===")


# ----------------------------------------------------------------------------- server under test
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Server:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="gramvet_e2e_")
        shutil.copytree(HERE, self.root, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            ".env", "*.db", "*.db-journal", "__pycache__", "media", ".venv", "data", "*.zip", "test_*.py", "*.log", ".DS_Store"))
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.logpath = os.path.join(self.root, "server.log")
        self.p = None

    def start(self):
        env = dict(os.environ, PORT=str(self.port), HOST="127.0.0.1", MONGO_URI="", SECRET_KEY="e2e-test-secret", PYTHONUNBUFFERED="1")
        env.pop("GOVERNMENT_AUTH_CODE", None)
        with open(self.logpath, "ab") as log:
            self.p = subprocess.Popen([sys.executable, "app.py"], cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        t = time.time()
        while time.time() - t < 120:
            if self.p.poll() is not None:
                raise RuntimeError("server exited early, see " + self.logpath)
            try:
                requests.get(self.base + "/api/me", timeout=2); return
            except requests.RequestException:
                time.sleep(0.4)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.p and self.p.poll() is None:
            os.killpg(self.p.pid, signal.SIGTERM)
            try: self.p.wait(15)
            except subprocess.TimeoutExpired: os.killpg(self.p.pid, signal.SIGKILL)

    def restart(self):
        self.stop(); self.start()

    def db(self):
        c = sqlite3.connect(os.path.join(self.root, "gramvet.db"), timeout=30)
        c.row_factory = sqlite3.Row
        return c


SRV = Server()


def q(sql, *p):
    c = SRV.db()
    try: return c.execute(sql, p).fetchall()
    finally: c.close()


def q1(sql, *p):
    r = q(sql, *p)
    return r[0][0] if r else None


# ----------------------------------------------------------------------------- actors
class Actor:
    def __init__(self, label):
        self.label, self.s, self.id = label, requests.Session(), None

    def call(self, method, path, body=None, raw=None, **params):
        kw = dict(timeout=90, params=params or None)
        if raw is not None: kw.update(data=raw, headers={"Content-Type": "application/json"})
        elif body is not None: kw.update(json=body)
        r = self.s.request(method, SRV.base + path, **kw)
        try: return r.status_code, r.json()
        except ValueError: return r.status_code, None

    def get(self, path, **params): return self.call("GET", path, **params)
    def post(self, path, body=None, raw=None): return self.call("POST", path, body, raw)

    def login(self, phone, pw):
        st, j = self.post("/api/login", {"phone": phone, "password": pw}); assert st == 200, (st, j); self.id = j["user"]["id"]; return self

    def signup(self, **f):
        st, j = self.post("/api/signup", f); assert st == 200, (self.label, st, j); self.id = j["user"]["id"]; return self

    def sessions(self, n):  # n independent HTTP sessions sharing this login
        out = []
        for _ in range(n):
            s = requests.Session(); s.cookies.update(self.s.cookies); out.append(s)
        return out


def strict_json_ok(actor, path):
    """True if the endpoint's body parses as REAL JSON (browsers reject bare NaN / Infinity)."""
    import json
    def boom(tok): raise ValueError(tok)
    r = actor.s.get(SRV.base + path, timeout=90)
    try: json.loads(r.text, parse_constant=boom); return r.status_code == 200
    except ValueError: return False


def area(who):
    """(status, parsed strict JSON or None) for the vet's 'My Area' map data."""
    import json
    def boom(tok): raise ValueError(tok)
    r = who.s.get(SRV.base + "/api/vet/outbreak-map", timeout=90)
    try: return r.status_code, json.loads(r.text, parse_constant=boom)
    except ValueError: return r.status_code, None


def burst(fn, n):
    """Run fn(i) in n threads that all start at the same instant."""
    barrier = threading.Barrier(n)
    def w(i):
        barrier.wait()
        try: return fn(i)
        except Exception as e: return (599, {"error": repr(e)})
    with cf.ThreadPoolExecutor(n) as ex:
        return list(ex.map(w, range(n)))


def hit(sess, method, path, body=None):
    r = sess.request(method, SRV.base + path, json=body, timeout=90)
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, None


# ----------------------------------------------------------------------------- domain helpers
def stock(vet): st, j = vet.get("/api/vet/vaccine-inventory"); assert st == 200, (st, j); return {s["vaccine"]: s["quantity"] for s in j["stock"]}
def central(gov): st, j = gov.get("/api/govt/vaccine-inventory"); assert st == 200, (st, j); return {c["vaccine"]: c["quantity"] for c in j["central"]}
def admin(vet, case, vac, **extra): return vet.post(f"/api/vet/cases/{case}/vaccine", {"vaccine_name": vac, **extra})   # exactly what the UI sends
def release(gov, vet_id, vac, qty, **extra): return gov.post("/api/govt/vaccine-shipments", {"vet_id": vet_id, "vaccine": vac, "quantity": qty, **extra})
def receive(vet, sid): return vet.post(f"/api/vet/vaccine-shipments/{sid}/receive")
def shipments(gov, **params): st, j = gov.get("/api/govt/vaccine-shipments", **params); assert st == 200, (st, j); return j["shipments"]
def open_alerts(gov, vet_id=None, vac=None):
    st, j = gov.get("/api/govt/vaccine-inventory"); assert st == 200
    return [a for a in j["open_alerts"] if (vet_id is None or a["vet_id"] == vet_id) and (vac is None or a["vaccine"] == vac)]
def low_notifs(gov, vet_id, vac):
    st, j = gov.get("/api/notifications"); assert st == 200
    return [n for n in j["notifications"] if n["kind"] == "LOW_STOCK" and (n.get("data") or {}).get("vet_id") == vet_id and n["data"].get("vaccine") == vac]
def vaccinations(vet_id, vac): return q1("SELECT COUNT(*) FROM vaccinations WHERE vet_id=? AND upper(vaccine_name)=?", vet_id, vac)
def alerts_total(vet_id, vac): return q1("SELECT COUNT(*) FROM vaccine_alerts WHERE vet_id=? AND vaccine=?", vet_id, vac)
def db_stock(vet_id, vac): return q1("SELECT quantity FROM vet_vaccine_stock WHERE vet_id=? AND vaccine=?", vet_id, vac)
def five_xx(results): return [r for r in results if r[0] >= 500]

_tag = iter(range(1, 10**6))
def make_case(farmer, expect_vet):
    st, j = farmer.post("/api/animals", {"tag": f"E2E-{next(_tag)}", "name": "Bessie", "species": "Cattle", "village": farmer.village, "ward": "W1", "pincode": farmer.pincode})
    assert st == 200, (st, j)
    aid = j["animal_id"]
    st, j = farmer.post("/api/cases/report", {"animal_id": aid, "symptoms": SYMPTOMS})
    assert st == 200, ("symptom report failed", st, j)
    case = q1("SELECT id FROM cases WHERE animal_id=?", aid)
    assigned = q1("SELECT vet_id FROM cases WHERE id=?", case)
    assert assigned == expect_vet, f"case {case} assigned to vet {assigned}, expected {expect_vet}"
    return case


def snapshot(gov, vets):
    return {"vets": {v.label: stock(v) for v in vets}, "central": central(gov),
            "shipments": [(s["id"], s["vet_id"], s["vaccine"], s["quantity"], s["status"]) for s in shipments(gov)],
            "alerts": sorted((a["vet_id"], a["vaccine"], a["stock_at_alert"]) for a in open_alerts(gov)),
            "ledger": q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE txn_type!='INITIAL'"), "vaccinations": q1("SELECT COUNT(*) FROM vaccinations")}


# ============================================================================= the test
def main():
    print("Starting the real GramVet server from a temp copy of the project ...")
    SRV.start()
    print(f"  {SRV.base}  (db: {SRV.root}/gramvet.db)")

    section("SETUP: real signups, animals, symptom reports (ML), vet assignment")
    gov1 = Actor("gov1").login("+919000000001", "12345")
    gov2 = Actor("gov2").signup(name="Second Officer", phone="9100000002", password="Gov@1234", role="government", govt_auth_id="GOV-26128")
    def mkvet(label, phone, lat, lon):
        return Actor(label).signup(name=f"Dr. {label}", phone=phone, password="Vet@1234", role="vet", vet_auth_id=f"V-{label}", pincode="422001",
                                   village=f"Vet{label}", address="Centre", latitude=lat, longitude=lon, district="X", state="Maharashtra")
    vetA = mkvet("Alpha", "9100000011", 19.9975, 73.7898)     # Nashik
    vetB = mkvet("Beta", "9100000012", 18.5204, 73.8567)      # Pune (~150 km away)
    def mkfarmer(label, phone, lat, lon):
        a = Actor(label).signup(name=label, phone=phone, password="Farm@1234", role="farmer", village=f"Village{label}", pincode="422002",
                                ward="W1", address="Farm", latitude=lat, longitude=lon, district="X", state="Maharashtra")
        a.village, a.pincode = f"Village{label}", "422002"; return a
    farmA = mkfarmer("FarmerA", "9100000021", 19.9980, 73.7900)
    farmB = mkfarmer("FarmerB", "9100000022", 18.5210, 73.8570)
    caseA = make_case(farmA, vetA.id)
    caseB = make_case(farmB, vetB.id)
    print(f"  vetA id={vetA.id} case={caseA} | vetB id={vetB.id} case={caseB} | govs: {gov1.id},{gov2.id}")

    # ------------------------------------------------------------------ 1
    section("1. Every vet starts with 25 of each disease (government has its own stock)")
    check(1, "vet A: 25 of each of the 4 diseases", stock(vetA) == dict.fromkeys(CODES, 25), stock(vetA))
    check(1, "vet B: 25 of each of the 4 diseases", stock(vetB) == dict.fromkeys(CODES, 25), stock(vetB))
    check(1, "government central stock exists for each of the 4 diseases", set(central(gov1)) == set(CODES) and all(v > 0 for v in central(gov1).values()), central(gov1))
    C0 = central(gov1)

    # ------------------------------------------------------------------ 2
    section("2. Administering one vaccine: 25 -> 24")
    st, j = admin(vetA, caseA, "FMD")
    check(2, "administer returns 200 and remaining_stock 24", st == 200 and j.get("remaining_stock") == 24, (st, j))
    check(2, "vet A FMD is now 24, other diseases still 25", stock(vetA) == {**dict.fromkeys(CODES, 25), "FMD": 24}, stock(vetA))
    check(2, "vet B untouched", stock(vetB) == dict.fromkeys(CODES, 25))
    row = q("SELECT quantity_change,balance_after FROM vaccine_stock_transactions WHERE vet_id=? AND vaccine='FMD' AND txn_type='ADMINISTERED'", vetA.id)
    check(2, "one vaccination record and one ledger row (-1, balance 24)", vaccinations(vetA.id, "FMD") == 1 and len(row) == 1 and tuple(row[0]) == (-1, 24))

    # ------------------------------------------------------------------ 3
    section("3. At exactly 10: NO low-stock alert")
    for _ in range(14):
        st, _j = admin(vetA, caseA, "FMD"); assert st == 200
    check(3, "stock is exactly 10", stock(vetA)["FMD"] == 10, stock(vetA))
    check(3, "no open alert for vet A / FMD", not open_alerts(gov1, vetA.id, "FMD"))
    check(3, "no alert row was ever created, no government notification", alerts_total(vetA.id, "FMD") == 0 and not low_notifs(gov1, vetA.id, "FMD") and not low_notifs(gov2, vetA.id, "FMD"))
    check(3, "vet inventory does not flag 10 as low", not [s for s in vetA.get("/api/vet/vaccine-inventory")[1]["stock"] if s["vaccine"] == "FMD"][0]["low"])

    # ------------------------------------------------------------------ 4
    section("4. At 9: government receives a low-stock alert")
    st, j = admin(vetA, caseA, "FMD")
    check(4, "administer 10 -> 9 succeeds", st == 200 and j["remaining_stock"] == 9, (st, j))
    al = open_alerts(gov1, vetA.id, "FMD")
    check(4, "open alert exists: vet, disease, stock 9", len(al) == 1 and al[0]["stock_at_alert"] == 9 and al[0]["current_stock"] == 9 and al[0]["created_at"], al)
    for g in (gov1, gov2):
        n = low_notifs(g, vetA.id, "FMD")
        check(4, f"{g.label} got exactly one CRITICAL LOW_STOCK notification", len(n) == 1 and n[0]["severity"] == "CRITICAL", n)
    check(4, "vet A is told too, and their inventory flags it low", len(low_notifs(vetA, vetA.id, "FMD")) == 1 and [s for s in vetA.get("/api/vet/vaccine-inventory")[1]["stock"] if s["vaccine"] == "FMD"][0]["low"])

    # ------------------------------------------------------------------ 5, 13, 14
    section("5. No duplicate unresolved alerts  /  13-14. Zero stock blocks administration, never negative")
    for _ in range(9):
        st, _j = admin(vetA, caseA, "FMD"); assert st == 200
    check(5, "stock walked 9 -> 0; still exactly ONE open alert (not one per dose)", len(open_alerts(gov1, vetA.id, "FMD")) == 1 and alerts_total(vetA.id, "FMD") == 1)
    check(5, "still exactly one notification per government user", len(low_notifs(gov1, vetA.id, "FMD")) == 1 and len(low_notifs(gov2, vetA.id, "FMD")) == 1)
    check(5, "alert keeps the level it was raised at (9)", open_alerts(gov1, vetA.id, "FMD")[0]["stock_at_alert"] == 9)
    check(13, "25 vaccinations were recorded for 25 stock", stock(vetA)["FMD"] == 0 and vaccinations(vetA.id, "FMD") == 25)
    outs = [admin(vetA, caseA, "FMD") for _ in range(6)]
    check(13, "vet with 0 vaccines cannot administer (6 attempts, all 400)", all(s == 400 and "Insufficient" in j["error"] for s, j in outs), outs[:1])
    check(14, "stock stays 0 (never negative), no extra vaccination records", stock(vetA)["FMD"] == 0 and db_stock(vetA.id, "FMD") == 0 and vaccinations(vetA.id, "FMD") == 25)
    check(5, "refused attempts created no alert and no notification", alerts_total(vetA.id, "FMD") == 1 and len(low_notifs(gov1, vetA.id, "FMD")) == 1)

    # ------------------------------------------------------------------ 6, 7, 8
    section("6-8. Government release: stock down, shipment IN_TRANSIT, vet stock unchanged")
    st, j = release(gov1, vetA.id, "FMD", 40)
    sid = (j or {}).get("shipment", {}).get("id")
    check(6, "release accepted (201)", st == 201 and sid, (st, j))
    check(6, "government FMD stock decreased by exactly 40", central(gov1)["FMD"] == C0["FMD"] - 40, central(gov1))
    check(6, "other diseases' government stock unchanged", all(central(gov1)[v] == C0[v] for v in CODES if v != "FMD"))
    sh = [s for s in shipments(gov1) if s["id"] == sid][0]
    check(7, "shipment exists with status IN_TRANSIT (vet, disease, quantity, release time)", sh["status"] == "IN_TRANSIT" and sh["vet_id"] == vetA.id and sh["vaccine"] == "FMD" and sh["quantity"] == 40 and sh["released_at"] and not sh["received_at"], sh)
    check(8, "vet FMD inventory still 0 while IN_TRANSIT (API and database)", stock(vetA)["FMD"] == 0 and db_stock(vetA.id, "FMD") == 0)
    check(8, "vet still cannot administer FMD while it is only in transit", admin(vetA, caseA, "FMD")[0] == 400)
    check(8, "vet sees it as an incoming shipment", any(s["id"] == sid and s["status"] == "IN_TRANSIT" for s in vetA.get("/api/vet/vaccine-inventory")[1]["shipments"]))

    # ------------------------------------------------------------------ 9, 10
    section("9-10. Vet confirms receipt: inventory increases, shipment RECEIVED")
    st, j = receive(vetA, sid)
    check(9, "receive returns 200 with new_stock 40", st == 200 and j["new_stock"] == 40 and j["status"] == "RECEIVED", (st, j))
    check(9, "vet FMD inventory is exactly 40 (API and database)", stock(vetA)["FMD"] == 40 and db_stock(vetA.id, "FMD") == 40)
    mine = [s for s in vetA.get("/api/vet/vaccine-inventory")[1]["shipments"] if s["id"] == sid][0]
    check(10, "shipment is RECEIVED with a received_at time", mine["status"] == "RECEIVED" and mine["received_at"], mine)
    check(9, "government stock did not change on receipt", central(gov1)["FMD"] == C0["FMD"] - 40)
    check(9, "ledger has exactly one +40 receipt row", [tuple(r) for r in q("SELECT quantity_change,balance_after FROM vaccine_stock_transactions WHERE shipment_id=? AND txn_type='SHIPMENT_RECEIVED'", sid)] == [(40, 40)])
    al = q("SELECT status,resolved_by_shipment_id FROM vaccine_alerts WHERE vet_id=? AND vaccine='FMD'", vetA.id)
    check(9, "the low-stock alert resolved itself (stock is back above 10)", not open_alerts(gov1, vetA.id, "FMD") and [tuple(r) for r in al] == [("RESOLVED", sid)], [tuple(r) for r in al])
    check(9, "vet can administer again: 40 -> 39", admin(vetA, caseA, "FMD")[1].get("remaining_stock") == 39)

    # ------------------------------------------------------------------ 11
    section("11. Receiving twice does not duplicate inventory")
    again = [receive(vetA, sid) for _ in range(3)]
    check(11, "repeat receives are refused (409)", all(s == 409 for s, _j in again), again)
    check(11, "inventory unchanged (39), still one receipt row in the ledger", stock(vetA)["FMD"] == 39 and q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE shipment_id=? AND txn_type='SHIPMENT_RECEIVED'", sid) == 1)

    # ------------------------------------------------------------------ 12
    section("12. Government sees RECEIVED")
    for g in (gov1, gov2):
        s = [x for x in shipments(g) if x["id"] == sid][0]
        check(12, f"{g.label} sees RECEIVED with release + received times, vet name", s["status"] == "RECEIVED" and s["released_at"] and s["received_at"] and s["vet_name"] == "Dr. Alpha", s)
    check(12, "status filters work", sid in [x["id"] for x in shipments(gov1, status="RECEIVED")] and sid not in [x["id"] for x in shipments(gov1, status="IN_TRANSIT")])
    check(12, "both government users were notified of the delivery", all(any(n["title"] == "Vaccine shipment delivered" for n in g.get("/api/notifications")[1]["notifications"]) for g in (gov1, gov2)))

    # ------------------------------------------------------------------ 15
    section("15. A vet cannot reach another vet's inventory or shipments")
    st, j = release(gov1, vetA.id, "BQ", 7); sidA = j["shipment"]["id"]
    before = (stock(vetA), stock(vetB), central(gov1))
    st, j = receive(vetB, sidA)
    check(15, "vet B cannot receive vet A's shipment (404, indistinguishable from unknown id)", st == 404 and j["error"] == "Shipment not found.", (st, j))
    st, j = vetB.get("/api/vet/vaccine-inventory", vet_id=vetA.id)
    check(15, "vet B's inventory endpoint ignores a vet_id parameter and shows only B's own data", st == 200 and {s["vaccine"]: s["quantity"] for s in j["stock"]} == stock(vetB) and stock(vetB)["FMD"] == 25 and all(s["vet_id"] == vetB.id for s in j["shipments"]), j)
    check(15, "vet A's shipments are not visible to vet B", sidA not in [s["id"] for s in vetB.get("/api/vet/vaccine-inventory")[1]["shipments"]] and sid not in [s["id"] for s in vetB.get("/api/vet/vaccine-inventory")[1]["shipments"]])
    st, j = admin(vetB, caseA, "FMD")
    check(15, "vet B cannot vaccinate on vet A's case (404) and no stock moves", st == 404 and (stock(vetA), stock(vetB), central(gov1)) == before)
    check(15, "vet A's shipment is still IN_TRANSIT and BQ not credited", q1("SELECT status FROM vaccine_shipments WHERE id=?", sidA) == "IN_TRANSIT" and stock(vetA)["BQ"] == 25)
    st, j = vetB.get(f"/api/cases/{caseA}")
    print(f"  INFO  vet B opening vet A's case detail (pre-existing route, not part of the inventory feature): HTTP {st}")
    check(15, "vet A can still receive their own shipment afterwards (BQ 25 -> 32)", receive(vetA, sidA)[0] == 200 and stock(vetA)["BQ"] == 32)

    # ------------------------------------------------------------------ 16
    section("16. Vet / farmer / anonymous cannot perform government operations")
    anon = Actor("anonymous")
    before = (central(gov1), len(shipments(gov1)), stock(vetA), stock(vetB))
    attempts = [("POST", "/api/govt/vaccine-shipments", {"vet_id": vetA.id, "vaccine": "FMD", "quantity": 5}),
                ("GET", "/api/govt/vaccine-shipments", None), ("GET", "/api/govt/vaccine-inventory", None),
                ("POST", "/api/govt/inventory", {"resource_name": "FMD", "available_qty": 99999, "unit": "dose"}),
                ("GET", "/api/govt/overview", None), ("POST", f"/api/govt/resource-request/1", {"status": "DISPATCHED"})]
    for who, want in ((vetA, 403), (farmA, 403), (anon, 401)):
        got = [who.call(m, p, b)[0] for m, p, b in attempts]
        check(16, f"{who.label}: every government endpoint refused ({want})", all(s == want for s in got), got)
    st, _j = vetA.post("/api/govt/vaccine-shipments", {"vet_id": vetA.id, "vaccine": "FMD", "quantity": 100})
    check(16, "a vet cannot release vaccines to themselves", st == 403)
    check(16, "vets cannot use the vet-only receive as government / anonymous cannot receive", gov1.post(f"/api/vet/vaccine-shipments/{sidA}/receive")[0] == 403 and anon.post(f"/api/vet/vaccine-shipments/{sidA}/receive")[0] == 401 and farmA.get("/api/vet/vaccine-inventory")[0] == 403)
    check(16, "none of the refused requests changed any state", (central(gov1), len(shipments(gov1)), stock(vetA), stock(vetB)) == before)

    # ------------------------------------------------------------------ My Area
    section("MY AREA: the vet's area / farmers map data (real server, started with `python app.py`)")
    st, ja = area(vetA); st_b, jb = area(vetB)
    check("MyArea", "GET /api/vet/outbreak-map exists and returns valid JSON (it used to be a 404: the route was defined below app.run())", st == 200 and ja is not None and st_b == 200 and jb is not None, (st, st_b))
    check("MyArea", "the vet's own map point is returned", ja and (ja["vet"]["latitude"], ja["vet"]["longitude"]) == (19.9975, 73.7898) and ja["vet"]["name"] == "Dr. Alpha", ja and ja["vet"])
    fa = ja["farmers"][0] if ja and ja["farmers"] else {}
    check("MyArea", "vet A sees exactly their own farmer, with map position, animals and open cases", ja and [f["name"] for f in ja["farmers"]] == ["FarmerA"] and fa["lat"] is not None and fa["lon"] is not None and fa["animals"] >= 1 and fa["open_cases"] >= 1, ja and ja["farmers"])
    vil = [v for v in (ja or {"villages": []})["villages"] if v["name"] == "VillageFarmerA"]
    check("MyArea", "vet A's village is listed with a position, animals, an outbreak-risk level and score", len(vil) == 1 and vil[0]["lat"] is not None and vil[0]["animals"] >= 1 and vil[0]["outbreak_level"] in ("LOW", "MODERATE", "HIGH", "CRITICAL") and 0 <= vil[0]["outbreak_risk_score"] <= 100, vil)
    check("MyArea", "vet B sees only their own farmer and village, nothing of vet A's", jb and [f["name"] for f in jb["farmers"]] == ["FarmerB"] and all(v["name"] != "VillageFarmerA" for v in jb["villages"]), jb and jb["farmers"])
    check("MyArea", "farmer / government / anonymous cannot read a vet's area (403 / 403 / 401)", [farmA.get("/api/vet/outbreak-map")[0], gov1.get("/api/vet/outbreak-map")[0], anon.get("/api/vet/outbreak-map")[0]] == [403, 403, 401])

    # ------------------------------------------------------------------ hardening
    section("H. Hostile / malformed input must be refused cleanly (400) and change nothing")
    base_state = (stock(vetA), stock(vetB), central(gov1), len(shipments(gov1)), vaccinations(vetA.id, "LSD"))
    junk = ['{"vaccine_name":"LSD","dose":NaN}', '{"vaccine_name":"LSD","dose":Infinity}', '{"vaccine_name":"LSD","dose":1e999}', '{"vaccine_name":"LSD","dose":"nan"}',
            '{"vaccine_name":"LSD","dose":1e308}', '{"vaccine_name":"LSD","dose":2000000}', '{"vaccine_name":"LSD","dose":0}', '{"vaccine_name":"LSD","dose":-1}',
            '{"vaccine_name":"LSD","dose":"abc","unit":"barrels"}', '{"vaccine_name":"LSD","dose":null}']
    lsd0, n0 = stock(vetA)["LSD"], vaccinations(vetA.id, "LSD")
    res = [vetA.post(f"/api/vet/cases/{caseA}/vaccine", raw=r)[0] for r in junk]
    rows = q("SELECT dose,unit FROM vaccinations WHERE vet_id=? AND upper(vaccine_name)='LSD' ORDER BY id DESC LIMIT ?", vetA.id, len(junk))
    check("H", "a client-supplied dose/unit is ignored: 10 junk values (NaN, Infinity, 1e999, 1e308, 2 million, 0, -1, text, null) are each just ONE vaccination of exactly 1 dose",
          all(s == 200 for s in res) and vaccinations(vetA.id, "LSD") == n0 + len(junk) and all(tuple(r) == (1.0, "dose") for r in rows), (res, [tuple(r) for r in rows][:3]))
    check("H", "...and each one cost exactly 1 vaccine", stock(vetA)["LSD"] == lsd0 - len(junk), (lsd0, stock(vetA)["LSD"]))
    check("H", "the government dashboard payloads are still valid JSON after those attempts (a browser can parse them)",
          strict_json_ok(gov1, "/api/govt/overview") and strict_json_ok(gov1, "/api/govt/vaccine-inventory") and strict_json_ok(gov1, "/api/govt/vaccine-shipments"))
    res = [vetA.post(f"/api/vet/cases/{caseA}/vaccine", {"vaccine_name": "LSD", "dose": 1, "administered_at": v})[0] for v in (12345, ["x"], {"a": 1})]
    check("H", "non-text administered_at is a clean 400, not a server error", all(s == 400 for s in res), res)
    check("H", "vaccine names are case-insensitive but only the 4 diseases are accepted", admin(vetA, caseA, "lsd")[0] == 200 and admin(vetA, caseA, "RABIES")[0] == 400)
    res = [release(gov1, v, "FMD", 1)[0] for v in (2.7, True, "abc", [], None, -1, 0, 10**12)] + [release(gov1, vetA.id, "FMD", q)[0] for q in (0, -5, 2.5, True, "x", None, 10**12)]
    check("H", "release with fractional/boolean/garbage vet or quantity is refused (400/404), never rounded to a real vet", all(s in (400, 404) for s in res), res)
    res = [gov1.post("/api/govt/inventory", raw=f'{{"resource_name":"FMD","available_qty":{v},"unit":"dose"}}')[0] for v in ("NaN", "Infinity", "-Infinity", "10.5", "true")]
    res.append(gov1.post("/api/govt/inventory", raw='{"resource_name":"Syringes","available_qty":NaN,"unit":"units"}')[0])
    st_c, jc = gov1.get("/api/govt/vaccine-inventory")
    check("H", "legacy 'Manage inventory' refuses NaN/Infinity/fractions/booleans (any resource: no more 500s; vaccines: whole doses only)", all(s == 400 for s in res) and st_c == 200 and all(float(c["quantity"]).is_integer() for c in jc["central"]), (res, st_c))
    check("H", "none of the hostile requests changed any stock or created a shipment", (stock(vetA)["FMD"], stock(vetB), len(shipments(gov1))) == (base_state[0]["FMD"], base_state[1], base_state[3]) and central(gov1)["FMD"] == base_state[2]["FMD"])
    check("H", "in total the 10 junk-dose requests + 1 lowercase-name request cost exactly 11 vaccines (no partial effects from any refused request)", stock(vetA)["LSD"] == lsd0 - len(junk) - 1 and vaccinations(vetA.id, "LSD") == n0 + len(junk) + 1)

    # ------------------------------------------------------------------ 17
    section("17. Refresh / restart never resets inventory")
    fleet = [vetA, vetB]
    st_before = snapshot(gov1, fleet)
    for _ in range(2): assert stock(vetA) == st_before["vets"]["Alpha"]          # "refresh": repeated reads are stable
    SRV.restart()
    for a in (gov1, gov2, vetA, vetB): a.login({"gov1": "+919000000001", "gov2": "9100000002", "Alpha": "9100000011", "Beta": "9100000012"}[a.label], "12345" if a is gov1 else ("Gov@1234" if a is gov2 else "Vet@1234"))
    st_after = snapshot(gov1, fleet)
    check(17, "after restart #1 every vet stock, government stock, shipment, alert and ledger row is identical", st_after == st_before, {k: (st_before[k], st_after[k]) for k in st_before if st_before[k] != st_after[k]})
    check(17, "vet A's FMD is still what it was (not back to 25)", stock(vetA)["FMD"] == st_before["vets"]["Alpha"]["FMD"] != 25)
    vetD = mkvet("Delta", "9100000014", 21.1458, 79.0882)
    check(17, "a vet who registers after the restart starts at 25 without disturbing anyone", stock(vetD) == dict.fromkeys(CODES, 25) and snapshot(gov1, fleet) == st_before)

    section("17b. Legacy data: a vet with pre-existing vaccination history, first start with the inventory feature")
    vetC = mkvet("Gamma", "9100000013", 20.0, 74.0)
    farmer_id = farmA.id; animal_id = q1("SELECT id FROM animals WHERE farmer_id=?", farmA.id)
    check("17b", "vet C has no stock rows yet (feature has never seen them)", q1("SELECT COUNT(*) FROM vet_vaccine_stock WHERE vet_id=?", vetC.id) == 0)
    SRV.stop()
    c = SRV.db()
    for i in range(18):
        c.execute("INSERT INTO vaccinations(animal_id,farmer_id,vet_id,vaccine_name,dose,unit,administered_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (animal_id, farmer_id, vetC.id, "FMD", 1, "dose", "2026-01-01T00:00:00+05:30", "2026-01-01T00:00:00+05:30"))
    c.commit(); c.close()
    SRV.start()
    for a, ph, pw in ((gov1, "+919000000001", "12345"), (gov2, "9100000002", "Gov@1234"), (vetA, "9100000011", "Vet@1234"), (vetB, "9100000012", "Vet@1234"), (vetC, "9100000013", "Vet@1234")):
        a.login(ph, pw)
    check("17b", "restart #2: vet C's stock = 25 - 18 prior vaccinations = 7 (not reset to 25)", stock(vetC)["FMD"] == 7 and db_stock(vetC.id, "FMD") == 7, stock(vetC))
    check("17b", "government is alerted about vet C's low stock (7 < 10) even though no vaccine was given today", len(open_alerts(gov1, vetC.id, "FMD")) == 1 and len(low_notifs(gov1, vetC.id, "FMD")) == 1 and len(low_notifs(gov2, vetC.id, "FMD")) == 1, open_alerts(gov1, vetC.id))
    check("17b", "no alert for vet C's other, untouched vaccines", not [a for a in open_alerts(gov1, vetC.id) if a["vaccine"] != "FMD"])
    check("MyArea", "My Area is still served after a restart (route registration does not depend on how the server starts)", area(vetA)[0] == 200 and [f["name"] for f in area(vetA)[1]["farmers"]] == ["FarmerA"])
    check(17, "restart #2 left vets A and B exactly as they were", {v.label: stock(v) for v in fleet} == st_before["vets"] and central(gov1) == st_before["central"])

    # ------------------------------------------------------------------ 18
    section("18. Concurrent requests cannot double-spend")
    allres = []
    # (a) exactly one vaccine left, 25 racers
    for _ in range(stock(vetA)["HS"] - 1):
        assert admin(vetA, caseA, "HS")[0] == 200
    check(18, "setup: vet A has exactly 1 HS left", stock(vetA)["HS"] == 1)
    ss = vetA.sessions(25); v0 = vaccinations(vetA.id, "HS")
    res = burst(lambda i: hit(ss[i], "POST", f"/api/vet/cases/{caseA}/vaccine", {"vaccine_name": "HS", "dose": 1}), 25); allres += res
    codes = sorted(s for s, _j in res)
    check(18, "(a) 25 simultaneous requests for the last vaccine: exactly ONE succeeds", codes.count(200) == 1 and codes.count(400) == 24, codes)
    check(18, "(a) stock is 0 (not negative), exactly one vaccination record was added", stock(vetA)["HS"] == 0 and db_stock(vetA.id, "HS") == 0 and vaccinations(vetA.id, "HS") == v0 + 1)
    # (b) one shipment, 25 simultaneous receive clicks
    sid3 = release(gov1, vetA.id, "HS", 10)[1]["shipment"]["id"]
    ss = vetA.sessions(25)
    res = burst(lambda i: hit(ss[i], "POST", f"/api/vet/vaccine-shipments/{sid3}/receive"), 25); allres += res
    codes = sorted(s for s, _j in res)
    check(18, "(b) 25 simultaneous 'Vaccine Received' clicks: exactly ONE credits the stock", codes.count(200) == 1 and codes.count(409) == 24, codes)
    check(18, "(b) HS went 0 -> exactly 10 (not 250), one ledger receipt row", stock(vetA)["HS"] == 10 and q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE shipment_id=? AND txn_type='SHIPMENT_RECEIVED'", sid3) == 1)
    # (c) government over-draw: central HS forced to 100 through the existing 'Manage inventory' call, 20 racers x 30
    check(18, "(c) setup: government sets central HS stock to 100", gov1.post("/api/govt/inventory", {"resource_name": "HS", "available_qty": 100, "unit": "dose"})[0] == 200 and central(gov1)["HS"] == 100)
    n_ship = len(shipments(gov1)); gs = gov1.sessions(20); targets = [vetA.id, vetB.id]
    res = burst(lambda i: hit(gs[i], "POST", "/api/govt/vaccine-shipments", {"vet_id": targets[i % 2], "vaccine": "HS", "quantity": 30}), 20); allres += res
    codes = sorted(s for s, _j in res)
    check(18, "(c) 20 simultaneous releases of 30 against a stock of 100: exactly 3 succeed", codes.count(201) == 3 and codes.count(409) == 17, codes)
    check(18, "(c) government stock is 10 (never negative), exactly 3 shipments created", central(gov1)["HS"] == 10 and len(shipments(gov1)) == n_ship + 3)
    check(18, "(c) releasing did not touch either vet's stock (all IN_TRANSIT)", stock(vetA)["HS"] == 10 and stock(vetB)["HS"] == 25)
    # (d) mixed: administer and receive on the same vet/vaccine at the same instant
    for _ in range(stock(vetA)["LSD"] - 5):
        assert admin(vetA, caseA, "LSD")[0] == 200
    sid4 = release(gov1, vetA.id, "LSD", 6)[1]["shipment"]["id"]
    ss = vetA.sessions(15); start = stock(vetA)["LSD"]
    def mixed(i): return hit(ss[i], "POST", f"/api/vet/vaccine-shipments/{sid4}/receive") if i < 3 else hit(ss[i], "POST", f"/api/vet/cases/{caseA}/vaccine", {"vaccine_name": "LSD", "dose": 1})
    res = burst(mixed, 15); allres += res
    rcv = [s for s, _j in res[:3]]; adm = [s for s, _j in res[3:]]
    final = stock(vetA)["LSD"]
    check(18, "(d) mixed race: exactly one receive wins", sorted(rcv) == [200, 409, 409], rcv)
    check(18, "(d) mixed race: final = 5 + 6 - successful vaccinations, never negative, never more than 11 succeed", final == start + 6 - adm.count(200) and final >= 0 and adm.count(200) <= 11 and all(s in (200, 400) for s in adm), (start, final, adm))
    # (e) two vets, 44 simultaneous vaccinations, crossing the low-stock threshold under load
    bqA, bqB = stock(vetA)["BQ"], stock(vetB)["BQ"]
    sa, sb = vetA.sessions(24), vetB.sessions(20)
    def both(i): return hit(sa[i], "POST", f"/api/vet/cases/{caseA}/vaccine", {"vaccine_name": "BQ", "dose": 1}) if i < 24 else hit(sb[i - 24], "POST", f"/api/vet/cases/{caseB}/vaccine", {"vaccine_name": "BQ", "dose": 1})
    res = burst(both, 44); allres += res
    check(18, "(e) 44 simultaneous vaccinations by two vets all succeed and stay separate (A 32-24=8, B 25-20=5)", all(s == 200 for s, _j in res) and stock(vetA)["BQ"] == bqA - 24 == 8 and stock(vetB)["BQ"] == bqB - 20 == 5, (stock(vetA)["BQ"], stock(vetB)["BQ"]))
    check(5, "(e) crossing the threshold under load: exactly ONE alert and ONE notification per government user, per vet", all(len(open_alerts(gov1, v.id, "BQ")) == 1 and alerts_total(v.id, "BQ") == 1 and len(low_notifs(g, v.id, "BQ")) == 1 for v in (vetA, vetB) for g in (gov1, gov2)))
    check(18, "no request in any burst returned a server error (5xx / lock timeout)", not five_xx(allres), five_xx(allres)[:2])

    # ------------------------------------------------------------------ closing invariants
    section("14/18. Global invariants after everything above")
    c = SRV.db()
    check(14, "no vet stock row is negative", c.execute("SELECT COUNT(*) FROM vet_vaccine_stock WHERE quantity<0").fetchone()[0] == 0)
    check(14, "no government stock is negative or fractional", c.execute("SELECT COUNT(*) FROM resource_inventory WHERE resource_name IN ('FMD','HS','LSD','BQ') AND (available_qty<0 OR available_qty!=CAST(available_qty AS INTEGER))").fetchone()[0] == 0)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE vet_vaccine_stock SET quantity=-1 WHERE vet_id=? AND vaccine='FMD'", (vetA.id,)); neg = True
    except sqlite3.IntegrityError: neg = False
    finally: c.rollback()
    check(14, "even a direct SQL write of a negative quantity is rejected by the database", neg is False)
    bad = [(r["vet_id"], r["vaccine"]) for r in c.execute("SELECT s.vet_id,s.vaccine FROM vet_vaccine_stock s WHERE s.quantity != COALESCE((SELECT SUM(quantity_change) FROM vaccine_stock_transactions t WHERE t.party='VET' AND t.vet_id=s.vet_id AND t.vaccine=s.vaccine),0)")]
    check(18, "every vet's stock equals the sum of its ledger (no lost or double-counted vaccine)", not bad, bad)
    bad = [(r["vet_id"], r["vaccine"]) for r in c.execute("""SELECT s.vet_id,s.vaccine FROM vet_vaccine_stock s WHERE
        (SELECT COUNT(*) FROM vaccinations v WHERE v.vet_id=s.vet_id AND upper(v.vaccine_name)=s.vaccine) !=
        (SELECT COUNT(*) FROM vaccine_stock_transactions t WHERE t.vet_id=s.vet_id AND t.vaccine=s.vaccine AND t.txn_type='ADMINISTERED')
        + CASE WHEN s.vet_id=(SELECT id FROM users WHERE phone='+919100000013') AND s.vaccine='FMD' THEN 18 ELSE 0 END""")]
    check(18, "every vaccination record has exactly one ledger row (treatment and inventory never diverge)", not bad, bad)
    bad = [r["vaccine"] for r in c.execute("""SELECT r.resource_name vaccine FROM resource_inventory r WHERE r.resource_name IN ('FMD','HS','LSD','BQ') AND r.available_qty !=
        (SELECT COALESCE(SUM(quantity_change),0) FROM vaccine_stock_transactions t WHERE t.party='GOVT' AND t.vaccine=r.resource_name)""")]
    check(18, "government stock equals the sum of its ledger (initial + manual edit + releases)", not bad, bad)
    bad = c.execute("SELECT COUNT(*) FROM (SELECT vet_id,vaccine FROM vaccine_alerts WHERE status='OPEN' GROUP BY vet_id,vaccine HAVING COUNT(*)>1)").fetchone()[0]
    check(5, "never more than one open alert per vet+disease anywhere in the database", bad == 0)
    c.close()
    section("Existing GramVet functionality still responds")
    ok = all(x for x in [farmA.get("/api/animals")[0] == 200, farmA.get("/api/cases")[0] == 200, vetA.get("/api/cases")[0] == 200, vetA.get("/api/vet/overview")[0] == 200,
                         gov1.get("/api/govt/overview")[0] == 200, gov1.get("/api/notifications")[0] == 200, vetA.get(f"/api/cases/{caseA}")[0] == 200,
                         vetA.post(f"/api/vet/cases/{caseA}/action", {"action_type": "NOTE", "details": "still works"})[0] == 200,
                         vetA.post(f"/api/vet/cases/{caseA}/action", {"action_type": "VACCINE", "details": "x"})[0] == 400])
    check("regress", "animals, cases, notifications, both overviews, case detail and clinical notes still work", ok)


def report():
    print("\n" + "=" * 78)
    names = {1: "Vet starts with 25 of each disease", 2: "Administering one vaccine: 25 -> 24", 3: "At exactly 10: no alert", 4: "At 9: government alert",
             5: "No duplicate unresolved alerts", 6: "Release decreases government stock", 7: "Release creates IN_TRANSIT shipment",
             8: "Vet stock NOT increased while IN_TRANSIT", 9: "Receipt increases vet inventory correctly", 10: "Shipment becomes RECEIVED",
             11: "Receiving twice does not duplicate", 12: "Government sees RECEIVED", 13: "Vet with 0 cannot administer", 14: "Inventory never negative",
             15: "Vet cannot reach another vet's inventory/shipment", 16: "Vet cannot do government operations", 17: "Restart/refresh does not reset inventory",
             18: "Concurrent requests cannot double-spend", "17b": "Legacy vet history + first start", "H": "Hostile input / client dose ignored", "MyArea": "Vet My Area map data works", "regress": "Existing features intact"}
    bad_total = 0
    for k in list(range(1, 19)) + ["17b", "H", "MyArea", "regress"]:
        rs = [r for r in RESULTS if r[0] == k]
        good = sum(1 for r in rs if r[2]); bad_total += len(rs) - good
        print(f"  {'PASS' if rs and good == len(rs) else 'FAIL'}  {str(k):>7}  {names[k]:<52} ({good}/{len(rs)} checks)")
    print(f"\n  {sum(1 for r in RESULTS if r[2])} of {len(RESULTS)} checks passed")
    return bad_total


if __name__ == "__main__":
    code = 1
    try:
        main()
        code = 1 if report() else 0
    finally:
        SRV.stop()
        if os.environ.get("KEEP_E2E") != "1": shutil.rmtree(SRV.root, ignore_errors=True)
        else: print("kept:", SRV.root)
    sys.exit(code)
