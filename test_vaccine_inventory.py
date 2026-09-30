"""Vaccine inventory tests. Run:  python test_vaccine_inventory.py

Uses a throw-away SQLite file, so it never touches gramvet.db.
"""
import itertools, os, re, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as gv

TMP = tempfile.mkdtemp(prefix="gramvet_test_")
gv.DB = os.path.join(TMP, "test.db")
gv.BASE = TMP                       # init() writes its reset marker under BASE
gv.app.config["TESTING"] = True
# SAFETY: report_case() mirrors cases to MongoDB using MONGO_URI from .env. Never let tests write to a real database.
gv.MONGO_URI = None

_TAGS = itertools.count(1)
GOVT_PHONE, VET1_PHONE = "+919000000001", "+919000000002"
VET2_PHONE = "+919000000009"


def db():
    return gv.conn()


def q(sql, *p):
    c = db()
    try:
        return c.execute(sql, p).fetchall()
    finally:
        c.close()


def q1(sql, *p):
    r = q(sql, *p)
    return r[0][0] if r else None


def client_for(phone, password="12345"):
    cl = gv.app.test_client()
    r = cl.post("/api/login", json={"phone": phone, "password": password})
    assert r.status_code == 200, r.get_json()
    return cl


def setUpModule():
    # init() once, with vet2 already present so the "existing vets get stock" migration path is exercised.
    gv.init()
    c = db()
    c.execute("INSERT INTO users(name,phone,password_hash,role,pincode,village,address,vet_auth_id,created_at) "
              "VALUES('Dr. Two',?,?,'vet','506001','Rampur','x','VET-002',?)", (VET2_PHONE, gv.hp("12345"), gv.now()))
    c.commit(); c.close()
    gv.init()   # a second start: must create vet2's rows and leave everything else alone


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.govt = q1("SELECT id FROM users WHERE phone=?", GOVT_PHONE)
        cls.vet1 = q1("SELECT id FROM users WHERE phone=?", VET1_PHONE)
        cls.vet2 = q1("SELECT id FROM users WHERE phone=?", VET2_PHONE)
        cls.farmer = q1("SELECT id FROM users WHERE role='farmer' LIMIT 1")

    @classmethod
    def make_case(cls, vet_id):
        cls._n = next(_TAGS)
        c = db()
        try:
            vid = c.execute("SELECT id FROM villages LIMIT 1").fetchone()[0]
            c.execute("INSERT INTO animals(tag,name,species,farmer_id,village_id,ward,created_at) VALUES(?,?,?,?,?,?,?)",
                      (f"T{cls._n}", f"Cow{cls._n}", "Cattle", cls.farmer, vid, "W1", gv.now()))
            aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO cases(animal_id,farmer_id,vet_id,status,opened_at) VALUES(?,?,?, 'OPEN',?)",
                      (aid, cls.farmer, vet_id, gv.now()))
            cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
            return cid
        finally:
            c.close()

    def stock(self, vet_id, vaccine):
        return q1("SELECT quantity FROM vet_vaccine_stock WHERE vet_id=? AND vaccine=?", vet_id, vaccine)

    def govt_stock(self, vaccine):
        return int(q1("SELECT available_qty FROM resource_inventory WHERE resource_name=?", vaccine))

    def set_govt_stock(self, vaccine, qty):
        c = db(); c.execute("UPDATE resource_inventory SET available_qty=? WHERE resource_name=?", (qty, vaccine))
        c.commit(); c.close()

    def set_vet_stock(self, vet_id, vaccine, qty):
        c = db(); c.execute("UPDATE vet_vaccine_stock SET quantity=? WHERE vet_id=? AND vaccine=?", (qty, vet_id, vaccine))
        c.commit(); c.close()

    def administer(self, cl, case_id, vaccine="FMD"):
        return cl.post(f"/api/vet/cases/{case_id}/vaccine", json={"vaccine_name": vaccine})   # exactly what the UI sends

    def release(self, cl, vet_id, vaccine, qty, **extra):
        return cl.post("/api/govt/vaccine-shipments", json={"vet_id": vet_id, "vaccine": vaccine, "quantity": qty, **extra})

    def open_alerts(self, vet_id, vaccine):
        return q1("SELECT COUNT(*) FROM vaccine_alerts WHERE vet_id=? AND vaccine=? AND status='OPEN'", vet_id, vaccine)


class T1_InitialStock(Base):
    def test_everyone_starts_with_25_of_each_and_govt_has_central_stock(self):
        for v in gv.VACCINE_CODES:
            self.assertEqual(self.stock(self.vet1, v), 25)
            self.assertEqual(self.stock(self.vet2, v), 25)
            self.assertEqual(self.govt_stock(v), gv.GOVT_START_STOCK)
        self.assertEqual(q1("SELECT COUNT(*) FROM vet_vaccine_stock WHERE vet_id=?", self.vet1), 4)

    def test_restart_does_not_reset_stock(self):
        cl = client_for(VET1_PHONE)
        case = self.make_case(self.vet1)
        self.assertEqual(self.administer(cl, case, "LSD").status_code, 200)
        self.assertEqual(self.stock(self.vet1, "LSD"), 24)
        self.set_govt_stock("LSD", 123)
        gv.init(); gv.init()                     # simulate two restarts
        self.assertEqual(self.stock(self.vet1, "LSD"), 24)
        self.assertEqual(self.govt_stock("LSD"), 123)
        self.set_govt_stock("LSD", gv.GOVT_START_STOCK); self.set_vet_stock(self.vet1, "LSD", 25)

    def test_vet_registered_later_gets_25_without_writing_on_get(self):
        c = db()
        c.execute("INSERT INTO users(name,phone,password_hash,role,pincode,village,address,vet_auth_id,created_at) "
                  "VALUES('Dr. Late','+919000000008',?,'vet','506001','Rampur','x','V3',?)", (gv.hp("12345"), gv.now()))
        c.commit(); vid = c.execute("SELECT id FROM users WHERE phone='+919000000008'").fetchone()[0]; c.close()
        j = client_for("+919000000008").get("/api/vet/vaccine-inventory").get_json()
        self.assertEqual([s["quantity"] for s in j["stock"]], [25, 25, 25, 25])
        self.assertEqual(q1("SELECT COUNT(*) FROM vet_vaccine_stock WHERE vet_id=?", vid), 0)  # GET is read-only
        case = self.make_case(vid)
        self.assertEqual(self.administer(client_for("+919000000008"), case, "BQ").status_code, 200)
        self.assertEqual(self.stock(vid, "BQ"), 24)
        self.assertEqual(self.stock(vid, "FMD"), 25)


class T2_Administration(Base):
    def test_only_actual_administration_deducts_and_threshold_is_below_10(self):
        cl, govt = client_for(VET1_PHONE), client_for(GOVT_PHONE)
        case = self.make_case(self.vet1)
        others_before = {v: self.stock(self.vet1, v) for v in ("HS", "LSD", "BQ")}
        vet2_before = self.stock(self.vet2, "FMD")
        self.set_vet_stock(self.vet1, "FMD", 25)
        # a clinical VACCINE note is not an administration: it is refused and nothing is deducted
        r = cl.post(f"/api/vet/cases/{case}/action", json={"action_type": "VACCINE", "details": "planned FMD"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock(self.vet1, "FMD"), 25)
        # a TEST/NOTE clinical action is fine and deducts nothing
        self.assertEqual(cl.post(f"/api/vet/cases/{case}/action", json={"action_type": "NOTE", "details": "n"}).status_code, 200)
        self.assertEqual(self.stock(self.vet1, "FMD"), 25)
        for _ in range(15):                                   # 25 -> 10 : NO alert
            self.assertEqual(self.administer(cl, case, "FMD").status_code, 200)
        self.assertEqual(self.stock(self.vet1, "FMD"), 10)
        self.assertEqual(self.open_alerts(self.vet1, "FMD"), 0)
        n_before = q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='LOW_STOCK'", self.govt)
        r = self.administer(cl, case, "FMD")                   # 10 -> 9 : alert
        self.assertEqual(r.get_json()["remaining_stock"], 9)
        self.assertEqual(self.open_alerts(self.vet1, "FMD"), 1)
        self.assertEqual(q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='LOW_STOCK'", self.govt), n_before + 1)
        for _ in range(3):                                     # 9 -> 6 : still exactly one alert, no duplicates
            self.administer(cl, case, "FMD")
        self.assertEqual(self.stock(self.vet1, "FMD"), 6)
        self.assertEqual(self.open_alerts(self.vet1, "FMD"), 1)
        self.assertEqual(q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND kind='LOW_STOCK'", self.govt), n_before + 1)
        # other diseases are untouched
        for v, before in others_before.items():
            self.assertEqual(self.stock(self.vet1, v), before)
        self.assertEqual(self.stock(self.vet2, "FMD"), vet2_before)
        # government sees the alert
        j = govt.get("/api/govt/vaccine-inventory").get_json()
        self.assertTrue(any(a["vet_id"] == self.vet1 and a["vaccine"] == "FMD" for a in j["open_alerts"]))

    def test_never_negative_and_no_record_when_empty(self):
        cl = client_for(VET2_PHONE)
        case = self.make_case(self.vet2)
        self.set_vet_stock(self.vet2, "HS", 2)
        before = q1("SELECT COUNT(*) FROM vaccinations WHERE vet_id=?", self.vet2)
        self.assertEqual(self.administer(cl, case, "HS").status_code, 200)
        self.assertEqual(self.administer(cl, case, "HS").status_code, 200)
        r = self.administer(cl, case, "HS")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient", r.get_json()["error"])
        self.assertEqual(self.stock(self.vet2, "HS"), 0)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccinations WHERE vet_id=?", self.vet2), before + 2)
        self.set_vet_stock(self.vet2, "HS", 25)

    def test_bad_vaccine_rejected_and_transactions_recorded(self):
        cl = client_for(VET1_PHONE); case = self.make_case(self.vet1)
        self.assertEqual(self.administer(cl, case, "RABIES").status_code, 400)
        self.administer(cl, case, "BQ")
        row = q("SELECT quantity_change,balance_after,txn_type,vaccination_id,case_id FROM vaccine_stock_transactions "
                "WHERE vet_id=? AND vaccine='BQ' AND txn_type='ADMINISTERED' ORDER BY id DESC LIMIT 1", self.vet1)[0]
        self.assertEqual(row["quantity_change"], -1)
        self.assertEqual(row["balance_after"], self.stock(self.vet1, "BQ"))
        self.assertEqual(row["case_id"], case)
        self.assertIsNotNone(row["vaccination_id"])


class T3_Permissions(Base):
    def test_role_and_login_protection(self):
        anon = gv.app.test_client()
        vet, govt = client_for(VET1_PHONE), client_for(GOVT_PHONE)
        farmer = client_for("+919000000003")
        body = {"vet_id": self.vet1, "vaccine": "FMD", "quantity": 5}
        for cl, code in ((anon, 401), (vet, 403), (farmer, 403)):
            self.assertEqual(cl.post("/api/govt/vaccine-shipments", json=body).status_code, code)
            self.assertEqual(cl.get("/api/govt/vaccine-shipments").status_code, code)
            self.assertEqual(cl.get("/api/govt/vaccine-inventory").status_code, code)
        for cl, code in ((anon, 401), (govt, 403), (farmer, 403)):
            self.assertEqual(cl.post("/api/vet/vaccine-shipments/1/receive").status_code, code)
            self.assertEqual(cl.get("/api/vet/vaccine-inventory").status_code, code)


class T4_ReleaseAndReceive(Base):
    def test_full_shipment_lifecycle(self):
        govt, vet1, vet2 = client_for(GOVT_PHONE), client_for(VET1_PHONE), client_for(VET2_PHONE)
        g0, v0 = self.govt_stock("HS"), self.stock(self.vet1, "HS")
        r = self.release(govt, self.vet1, "HS", 40)
        self.assertEqual(r.status_code, 201)
        ship = r.get_json()["shipment"]
        self.assertEqual(ship["status"], "IN_TRANSIT")
        self.assertEqual(self.govt_stock("HS"), g0 - 40)          # government deducted on release
        self.assertEqual(self.stock(self.vet1, "HS"), v0)         # vet NOT credited yet
        # vet sees it as incoming; government sees IN_TRANSIT
        inc = vet1.get("/api/vet/vaccine-inventory").get_json()
        self.assertTrue(any(s["id"] == ship["id"] and s["status"] == "IN_TRANSIT" for s in inc["shipments"]))
        gl = govt.get("/api/govt/vaccine-shipments?status=IN_TRANSIT").get_json()["shipments"]
        self.assertTrue(any(s["id"] == ship["id"] for s in gl))
        # another vet cannot confirm it, and nothing changes
        self.assertEqual(vet2.post(f"/api/vet/vaccine-shipments/{ship['id']}/receive").status_code, 404)
        self.assertEqual(self.stock(self.vet1, "HS"), v0)
        self.assertEqual(self.stock(self.vet2, "HS"), 25)
        # owner confirms
        r = vet1.post(f"/api/vet/vaccine-shipments/{ship['id']}/receive")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock(self.vet1, "HS"), v0 + 40)
        # second confirmation must not add again
        r = vet1.post(f"/api/vet/vaccine-shipments/{ship['id']}/receive")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.stock(self.vet1, "HS"), v0 + 40)
        # government sees RECEIVED with a delivery time, and was notified
        rec = [s for s in govt.get("/api/govt/vaccine-shipments").get_json()["shipments"] if s["id"] == ship["id"]][0]
        self.assertEqual(rec["status"], "RECEIVED"); self.assertTrue(rec["received_at"])
        self.assertTrue(q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND title='Vaccine shipment delivered'", self.govt))
        self.assertEqual(self.govt_stock("HS"), g0 - 40)
        self.set_vet_stock(self.vet1, "HS", 25); self.set_govt_stock("HS", g0)

    def test_release_validation(self):
        govt = client_for(GOVT_PHONE)
        g0 = self.govt_stock("FMD")
        bad = [{"vet_id": self.vet1, "vaccine": "FMD", "quantity": q} for q in (0, -3, 1.5, "abc", True, None)]
        bad += [{"vet_id": self.vet1, "vaccine": "XYZ", "quantity": 5}, {"vaccine": "FMD", "quantity": 5}]
        for b in bad:
            self.assertEqual(govt.post("/api/govt/vaccine-shipments", json=b).status_code, 400, b)
        self.assertEqual(self.release(govt, 99999, "FMD", 5).status_code, 404)
        self.assertEqual(self.release(govt, self.farmer, "FMD", 5).status_code, 404)   # not a vet
        r = self.release(govt, self.vet1, "FMD", g0 + 1)                                # more than government has
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.govt_stock("FMD"), g0)
        n = q1("SELECT COUNT(*) FROM vaccine_shipments")
        self.assertEqual(self.release(govt, self.vet1, "FMD", g0).status_code, 201)     # exactly all of it is allowed
        self.assertEqual(self.govt_stock("FMD"), 0)
        self.assertEqual(self.release(govt, self.vet1, "FMD", 1).status_code, 409)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_shipments"), n + 1)
        c = db(); c.execute("DELETE FROM vaccine_shipments WHERE id=(SELECT MAX(id) FROM vaccine_shipments)"); c.commit(); c.close()
        self.set_govt_stock("FMD", g0)

    def test_idempotency_key_prevents_double_release(self):
        govt = client_for(GOVT_PHONE); g0 = self.govt_stock("BQ")
        r1 = self.release(govt, self.vet2, "BQ", 7, idempotency_key="abc-1")
        r2 = self.release(govt, self.vet2, "BQ", 7, idempotency_key="abc-1")
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertTrue(r2.get_json()["duplicate"])
        self.assertEqual(r1.get_json()["shipment"]["id"], r2.get_json()["shipment"]["id"])
        self.assertEqual(self.govt_stock("BQ"), g0 - 7)
        self.assertEqual(self.release(govt, self.vet2, "BQ", 8, idempotency_key="abc-1").status_code, 409)
        self.assertEqual(self.govt_stock("BQ"), g0 - 7)
        self.set_govt_stock("BQ", g0)

    def test_alert_stays_open_until_stock_is_back_to_threshold_then_resolves(self):
        govt, vet = client_for(GOVT_PHONE), client_for(VET2_PHONE)
        case = self.make_case(self.vet2)
        self.set_vet_stock(self.vet2, "LSD", 10)
        self.administer(vet, case, "LSD")                                  # 10 -> 9 : alert
        self.assertEqual(self.open_alerts(self.vet2, "LSD"), 1)
        s = self.release(govt, self.vet2, "LSD", 0 + 1).get_json()["shipment"]   # 9 + 1 = 10 -> resolves
        vet.post(f"/api/vet/vaccine-shipments/{s['id']}/receive")
        self.assertEqual(self.stock(self.vet2, "LSD"), 10)
        self.assertEqual(self.open_alerts(self.vet2, "LSD"), 0)
        self.administer(vet, case, "LSD")                                  # 10 -> 9 again : a NEW alert is allowed
        self.assertEqual(self.open_alerts(self.vet2, "LSD"), 1)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_alerts WHERE vet_id=? AND vaccine='LSD'", self.vet2), 2)
        s = self.release(govt, self.vet2, "LSD", 0 + 2).get_json()["shipment"]   # 9 + 2 = 11
        vet.post(f"/api/vet/vaccine-shipments/{s['id']}/receive")
        self.assertEqual(self.open_alerts(self.vet2, "LSD"), 0)
        self.set_vet_stock(self.vet2, "LSD", 25)


class T5_Concurrency(Base):
    def run_threads(self, fn, n):
        results, barrier = [None] * n, threading.Barrier(n)
        def work(i):
            barrier.wait()
            try: results[i] = fn(i)
            except Exception as e: results[i] = e
        ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        [t.start() for t in ts]; [t.join() for t in ts]
        for r in results:
            self.assertFalse(isinstance(r, Exception), r)
        return results

    def test_same_shipment_received_concurrently_credits_once(self):
        govt = client_for(GOVT_PHONE)
        v0 = self.stock(self.vet1, "FMD"); g0 = self.govt_stock("FMD")
        sid = self.release(govt, self.vet1, "FMD", 20).get_json()["shipment"]["id"]
        clients = [client_for(VET1_PHONE) for _ in range(12)]
        codes = [r.status_code for r in self.run_threads(lambda i: clients[i].post(f"/api/vet/vaccine-shipments/{sid}/receive"), 12)]
        self.assertEqual(sorted(codes).count(200), 1, codes)
        self.assertEqual(sorted(codes).count(409), 11, codes)
        self.assertEqual(self.stock(self.vet1, "FMD"), v0 + 20)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE shipment_id=? AND txn_type='SHIPMENT_RECEIVED'", sid), 1)
        self.set_vet_stock(self.vet1, "FMD", v0); self.set_govt_stock("FMD", g0)

    def test_concurrent_releases_never_overdraw_government_stock(self):
        self.set_govt_stock("HS", 100)
        clients = [client_for(GOVT_PHONE) for _ in range(10)]
        n0 = q1("SELECT COUNT(*) FROM vaccine_shipments")
        codes = [r.status_code for r in self.run_threads(lambda i: self.release(clients[i], self.vet1, "HS", 30), 10)]
        self.assertEqual(codes.count(201), 3, codes)          # 3 x 30 = 90 fits, the 4th would need 120
        self.assertEqual(codes.count(409), 7, codes)
        self.assertEqual(self.govt_stock("HS"), 10)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_shipments"), n0 + 3)
        self.set_govt_stock("HS", gv.GOVT_START_STOCK)

    def test_concurrent_administrations_never_go_negative_or_double_alert(self):
        case = self.make_case(self.vet2)
        self.set_vet_stock(self.vet2, "BQ", 12)
        v0 = q1("SELECT COUNT(*) FROM vaccinations WHERE vet_id=?", self.vet2)
        a0 = q1("SELECT COUNT(*) FROM vaccine_alerts WHERE vet_id=? AND vaccine='BQ'", self.vet2)
        clients = [client_for(VET2_PHONE) for _ in range(20)]
        codes = [r.status_code for r in self.run_threads(lambda i: self.administer(clients[i], case, "BQ"), 20)]
        self.assertEqual(codes.count(200), 12, codes)
        self.assertEqual(codes.count(400), 8, codes)
        self.assertEqual(self.stock(self.vet2, "BQ"), 0)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccinations WHERE vet_id=?", self.vet2), v0 + 12)
        self.assertEqual(self.open_alerts(self.vet2, "BQ"), 1)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_alerts WHERE vet_id=? AND vaccine='BQ'", self.vet2), a0 + 1)
        self.set_vet_stock(self.vet2, "BQ", 25)


class T6_Consistency(Base):
    def test_ledger_reconciles_with_stock_and_existing_dashboards_work(self):
        # Ledger vs balance is only meaningful before tests poke stock directly, so check the invariant on a fresh vet.
        c = db()
        c.execute("INSERT INTO users(name,phone,password_hash,role,pincode,village,address,vet_auth_id,created_at) "
                  "VALUES('Dr. Fresh','+919000000007',?,'vet','506001','Rampur','x','V4',?)", (gv.hp("12345"), gv.now()))
        c.commit(); vid = c.execute("SELECT id FROM users WHERE phone='+919000000007'").fetchone()[0]; c.close()
        vet, govt = client_for("+919000000007"), client_for(GOVT_PHONE)
        case = self.make_case(vid)
        for _ in range(5): self.administer(vet, case, "FMD")
        sid = self.release(govt, vid, "FMD", 9).get_json()["shipment"]["id"]
        vet.post(f"/api/vet/vaccine-shipments/{sid}/receive")
        vet.post(f"/api/vet/vaccine-shipments/{sid}/receive")
        for v in gv.VACCINE_CODES:
            ledger = q1("SELECT COALESCE(SUM(quantity_change),0) FROM vaccine_stock_transactions WHERE party='VET' AND vet_id=? AND vaccine=?", vid, v)
            self.assertEqual(ledger, self.stock(vid, v), v)
        self.assertEqual(self.stock(vid, "FMD"), 25 - 5 + 9)
        # existing dashboards still respond and now reflect real stock
        j = vet.get("/api/vet/overview").get_json()
        self.assertEqual(j["resources_left"], 29 + 75.0)
        self.assertEqual(j["resources_sent"], 100 + 9.0)
        self.assertEqual(govt.get("/api/govt/overview").status_code, 200)
        self.assertEqual(vet.get("/api/vet/resource-status").status_code, 200)


FARMER_PHONE = "+919000000003"


class T7_TreatmentWorkflow(Base):
    """Inventory wired into the EXISTING treatment workflow (no duplicate flow)."""

    def snap(self):
        stock = [tuple(r) for r in q("SELECT vet_id,vaccine,quantity FROM vet_vaccine_stock ORDER BY 1,2")]
        return (stock, q1("SELECT COUNT(*) FROM vaccinations"),
                q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE txn_type='ADMINISTERED'"))

    def case_rows(self, case_id):
        return (q1("SELECT COUNT(*) FROM vaccinations WHERE case_id=?", case_id),
                q1("SELECT COUNT(*) FROM case_actions WHERE case_id=? AND action_type='VACCINE'", case_id),
                q1("SELECT COUNT(*) FROM vaccine_stock_transactions WHERE case_id=?", case_id))

    def test_real_workflow_deducts_only_on_administration(self):
        farmer, vet, govt = client_for(FARMER_PHONE), client_for(VET1_PHONE), client_for(GOVT_PHONE)
        self.set_vet_stock(self.vet1, "FMD", 25)
        base = self.snap()

        # animal registered (also with a self-reported last_vaccinated date): no deduction
        r = farmer.post("/api/animals", json={"tag": "FLOW-1", "name": "Gauri", "species": "Cattle", "village": "Kothapalli",
                                               "ward": "Ward 1", "pincode": "506001", "last_vaccinated": "2026-01-01"})
        self.assertEqual(r.status_code, 200, r.get_json())
        aid = r.get_json()["animal_id"]
        self.assertEqual(self.snap(), base)

        # disease suspected + predicted by the real model, case assigned to the vet
        r = farmer.post("/api/cases/report", json={"animal_id": aid, "symptoms": gv.SYMPTOMS[:3]})
        self.assertEqual(r.status_code, 200, r.get_json())
        case = q1("SELECT id FROM cases WHERE animal_id=?", aid)
        self.assertEqual(q1("SELECT vet_id FROM cases WHERE id=?", case), self.vet1)   # assigned to vet1
        self.assertEqual(self.snap(), base)

        # animal / case pages opened by farmer and vet; dashboards; farmer edits last_vaccinated
        self.assertEqual(farmer.get(f"/api/animals/{aid}/history").status_code, 200)
        self.assertEqual(farmer.get("/api/animals").status_code, 200)
        self.assertEqual(vet.get("/api/cases").status_code, 200)
        self.assertEqual(vet.get(f"/api/cases/{case}").status_code, 200)
        self.assertEqual(vet.get("/api/vet/cases").status_code, 200)
        self.assertEqual(vet.get("/api/vet/overview").status_code, 200)
        self.assertEqual(govt.get("/api/govt/overview").status_code, 200)
        r = farmer.put(f"/api/animals/{aid}", json={"name": "Gauri", "sex": "Female", "last_vaccinated": "2026-02-02"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.snap(), base)

        # treatment steps that are NOT a vaccine administration
        for typ in ("TEST", "MEDICATION", "NOTE"):
            self.assertEqual(vet.post(f"/api/vet/cases/{case}/action", json={"action_type": typ, "details": f"{typ} done"}).status_code, 200)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/model-review", json={"correct": True, "predicted": "FMD"}).status_code, 200)
        self.assertEqual(self.snap(), base)

        # a VACCINE clinical note can no longer bypass inventory
        r = vet.post(f"/api/vet/cases/{case}/action", json={"action_type": "VACCINE", "details": "gave FMD"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.case_rows(case), (0, 0, 0))
        self.assertEqual(self.snap(), base)

        # ---- the actual administration ----
        # someone else's vet cannot administer on this case
        self.assertEqual(self.administer(client_for(VET2_PHONE), case, "FMD").status_code, 404)
        self.assertEqual(self.snap(), base)
        r = self.administer(vet, case, "FMD")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["remaining_stock"], 24)
        self.assertEqual(self.stock(self.vet1, "FMD"), 24)
        for v in ("HS", "LSD", "BQ"):                                     # only that disease's vaccine moved
            self.assertEqual(self.stock(self.vet1, v), dict((k[1], k[2]) for k in base[0] if k[0] == self.vet1)[v])
        self.assertEqual(self.case_rows(case), (1, 1, 1))                 # vaccination + timeline entry + ledger row
        t = q("SELECT quantity_change,balance_after,animal_id,vaccination_id FROM vaccine_stock_transactions WHERE case_id=?", case)[0]
        self.assertEqual((t[0], t[1], t[2]), (-1, 24, aid))
        self.assertEqual(q1("SELECT id FROM vaccinations WHERE case_id=?", case), t[3])
        self.assertNotEqual(q1("SELECT last_vaccinated FROM animals WHERE id=?", aid), "2026-02-02")  # treatment updated it
        self.assertTrue(q1("SELECT COUNT(*) FROM notifications WHERE user_id=? AND title='Vaccination recorded'", self.farmer))
        # existing treatment flow continues: timeline shows it, case can still be cured and closed
        detail = vet.get(f"/api/cases/{case}").get_json()["case"]
        self.assertEqual(len(detail["vaccinations"]), 1)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/action", json={"action_type": "CURE", "details": "recovered"}).status_code, 200)
        before_close = self.snap()
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/close").status_code, 200)
        self.assertEqual(self.snap(), before_close)                        # closing does not touch stock

    def test_threshold_alert_through_real_case(self):
        vet, govt = client_for(VET1_PHONE), client_for(GOVT_PHONE)
        case = self.make_case(self.vet1)
        self.set_vet_stock(self.vet1, "HS", 11)
        c = db(); c.execute("UPDATE vaccine_alerts SET status='RESOLVED' WHERE vet_id=? AND vaccine='HS'", (self.vet1,)); c.commit(); c.close()
        self.administer(vet, case, "HS")                                   # 11 -> 10: no alert
        self.assertEqual(self.open_alerts(self.vet1, "HS"), 0)
        self.administer(vet, case, "HS")                                   # 10 -> 9: alert
        self.assertEqual(self.open_alerts(self.vet1, "HS"), 1)
        n = q("SELECT message FROM notifications WHERE user_id=? AND kind='LOW_STOCK' ORDER BY id DESC LIMIT 1", self.govt)[0][0]
        self.assertIn("HS", n); self.assertIn("9", n)
        self.set_vet_stock(self.vet1, "HS", 25)

    def test_zero_stock_fails_cleanly_and_treatment_is_untouched(self):
        vet = client_for(VET1_PHONE)
        case = self.make_case(self.vet1)
        self.set_vet_stock(self.vet1, "LSD", 0)
        aid = q1("SELECT animal_id FROM cases WHERE id=?", case)
        lv_before = q1("SELECT last_vaccinated FROM animals WHERE id=?", aid)
        notif_before = q1("SELECT COUNT(*) FROM notifications WHERE user_id=?", self.farmer)
        base = self.snap()
        r = self.administer(vet, case, "LSD")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Insufficient", r.get_json()["error"])
        self.assertEqual(self.snap(), base)
        self.assertEqual(self.stock(self.vet1, "LSD"), 0)                  # not negative
        self.assertEqual(self.case_rows(case), (0, 0, 0))                  # no half-recorded treatment
        self.assertEqual(q1("SELECT last_vaccinated FROM animals WHERE id=?", aid), lv_before)
        self.assertEqual(q1("SELECT COUNT(*) FROM notifications WHERE user_id=?", self.farmer), notif_before)
        # other vaccines still work for the same case
        self.assertEqual(self.administer(vet, case, "FMD").status_code, 200)
        self.set_vet_stock(self.vet1, "LSD", 25)

    def test_client_dose_and_unit_are_ignored_one_vaccine_per_administration(self):
        # Exactly 1 vaccine is applied per administration; a dose/unit sent by a client can neither change the
        # deduction nor the stored record (and can never poison the dose totals with NaN/Infinity).
        vet = client_for(VET1_PHONE); case = self.make_case(self.vet1)
        self.set_vet_stock(self.vet1, "BQ", 25)
        r = vet.post(f"/api/vet/cases/{case}/vaccine", json={"vaccine_name": "bq", "dose": 2.5, "unit": "ml"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock(self.vet1, "BQ"), 24)
        self.assertEqual(tuple(q("SELECT dose,unit FROM vaccinations WHERE case_id=?", case)[0]), (1.0, "dose"))
        for junk in ({"dose": 0}, {"dose": -1}, {"dose": "x"}, {"dose": None}, {"dose": 1e308}, {}):
            self.assertEqual(vet.post(f"/api/vet/cases/{case}/vaccine", json={"vaccine_name": "BQ", **junk}).status_code, 200, junk)
        self.assertEqual(self.stock(self.vet1, "BQ"), 24 - 6)                       # each cost exactly 1
        self.assertEqual({tuple(r) for r in q("SELECT dose,unit FROM vaccinations WHERE case_id=?", case)}, {(1.0, "dose")})
        self.set_vet_stock(self.vet1, "BQ", 25)

    def _fail_and_check(self, target, stock_before):
        """Make `target` blow up mid-vaccination; everything must roll back together."""
        vet = client_for(VET1_PHONE)
        case = self.make_case(self.vet1)
        self.set_vet_stock(self.vet1, "FMD", stock_before)
        aid = q1("SELECT animal_id FROM cases WHERE id=?", case)
        lv = q1("SELECT last_vaccinated FROM animals WHERE id=?", aid)
        alerts = q1("SELECT COUNT(*) FROM vaccine_alerts")
        base = self.snap()
        original = getattr(gv, target)
        def boom(*a, **k): raise RuntimeError("injected failure")
        setattr(gv, target, boom)
        try:
            with self.assertRaises(RuntimeError):
                self.administer(vet, case, "FMD")
        finally:
            setattr(gv, target, original)
        self.assertEqual(self.snap(), base)                                # stock, vaccinations, ledger unchanged
        self.assertEqual(self.stock(self.vet1, "FMD"), stock_before)
        self.assertEqual(self.case_rows(case), (0, 0, 0))                  # no timeline entry either
        self.assertEqual(q1("SELECT last_vaccinated FROM animals WHERE id=?", aid), lv)
        self.assertEqual(q1("SELECT COUNT(*) FROM vaccine_alerts"), alerts)
        # and the system is healthy afterwards: the very same request now succeeds
        self.assertEqual(self.administer(vet, case, "FMD").status_code, 200)
        self.assertEqual(self.stock(self.vet1, "FMD"), stock_before - 1)
        self.set_vet_stock(self.vet1, "FMD", 25)

    def test_failure_after_deduction_rolls_back_everything(self):
        self._fail_and_check("_stock_txn", 25)          # dies after stock -1, vaccination and timeline rows are written
        self._fail_and_check("create_notification", 25)  # dies at the very last step

    def test_failure_while_raising_alert_rolls_back_alert_and_deduction(self):
        self._fail_and_check("create_notification", 10)  # 10 -> 9 would raise the alert; it and the deduction both vanish


class T8_LegacyFlowsUntouched(Base):
    def test_farmer_and_generic_actions_still_work(self):
        vet = client_for(VET1_PHONE); case = self.make_case(self.vet1)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/action", json={"action_type": "NOTE", "details": "hello"}).status_code, 200)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/action", json={"action_type": "BOGUS", "details": "x"}).status_code, 400)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/action", json={"action_type": "NOTE", "details": ""}).status_code, 400)
        self.assertEqual(vet.post(f"/api/vet/cases/{case}/close").status_code, 400)   # still needs a CURE first


class T9_RouteRegistration(unittest.TestCase):
    """Regression: routes written below `app.run()` are silently never registered when the app is started with
    `python app.py`. That is exactly why the vet 'My Area' page failed with 'Request failed'."""

    def test_no_route_is_defined_after_the_app_run_block(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8", errors="replace").read()
        tail = src[src.index("if __name__"):]
        self.assertEqual(re.findall(r"(?m)^@app\.(?:get|post|put|delete|route)\(", tail), [], "a route is defined after app.run(): it will never exist")

    def test_vet_area_route_exists_and_is_vet_only(self):
        self.assertIn("/api/vet/outbreak-map", {r.rule for r in gv.app.url_map.iter_rules()})
        self.assertEqual(gv.app.test_client().get("/api/vet/outbreak-map").status_code, 401)
        self.assertEqual(client_for("+919000000003").get("/api/vet/outbreak-map").status_code, 403)     # farmer
        self.assertEqual(client_for(GOVT_PHONE).get("/api/vet/outbreak-map").status_code, 403)
        j = client_for(VET1_PHONE).get("/api/vet/outbreak-map")
        self.assertEqual(j.status_code, 200)
        self.assertEqual(set(j.get_json()), {"villages", "farmers", "vet"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
