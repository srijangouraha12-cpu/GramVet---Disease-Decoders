

import os, json, sqlite3, hashlib, math
import sys
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, session
import requests
import math

def get_maharashtra_coords(village_name, pincode):
    # Geocode strictly within Maharashtra
    query = f"{village_name}, {pincode}, Maharashtra, India"
    url = f"https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1"
    try:
        res = requests.get(url, headers={'User-Agent': 'GramVetApp'}).json()
        return float(res[0]['lat']), float(res[0]['lon'])
    except:
        # Fallback to Maharashtra geographic center if API fails
        return 19.7515, 75.7139 

def is_within_5km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    distance = R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
    return distance <= 5.0

def get_season_india(dt=None):
    # Indian Meteorological Department style seasons, used as the model's "season" input.
    dt = dt or datetime.now(timezone.utc)
    m = dt.month
    if m in (12, 1, 2): return "Winter"
    if m in (3, 4, 5): return "Summer"
    if m in (6, 7, 8, 9): return "Monsoon"
    return "Post-Monsoon"

def fetch_weather(lat, lon):
    # Live weather for the disease/outbreak model's 4 inputs: temperature, humidity, rainfall, season.
    if lat is None or lon is None:
        return {"available": False, "error": "No coordinates available for this report.", "season": get_season_india()}
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,relative_humidity_2m,precipitation"
               "&daily=precipitation_sum&forecast_days=1&timezone=auto")
        res = requests.get(url, timeout=8).json()
        cur = res.get("current", {}) or {}
        daily = res.get("daily", {}) or {}
        rainfall = None
        if daily.get("precipitation_sum"):
            rainfall = daily["precipitation_sum"][0]
        if rainfall is None:
            rainfall = cur.get("precipitation")
        return {
            "available": True,
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "rainfall_mm": rainfall,
            "season": get_season_india(),
            "latitude": lat, "longitude": lon,
            "source": "open-meteo",
            "fetched_at": now()
        }
    except Exception as e:
        return {"available": False, "error": "Weather lookup failed", "detail": str(e),
                "season": get_season_india(), "latitude": lat, "longitude": lon}

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "gramvet.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gramvet-local-dev-secret")

SYMPTOMS = [
    "Fever","Cough","Nasal discharge","Difficulty breathing","Reduced appetite",
    "Weakness / lethargy","Diarrhoea","Vomiting","Dehydration","Excessive salivation",
    "Mouth lesions / sores","Lameness / difficulty walking","Swelling","Skin lesions / rash",
    "Eye discharge / redness","Abnormal milk production","Abortion / reproductive problem",
    "Weight loss","High body temperature","Ticks / external parasites"
]

ALLOWED_SPECIES = {"Cattle", "Buffalo"}
RADIUS_KM = 5.0
ML_DIR = os.path.join(BASE, "ml_package")
if ML_DIR not in sys.path:
    sys.path.insert(0, ML_DIR)
ML_IMPORT_ERROR = None
try:
    from predict_disease import predict_health_status
except Exception as e:
    predict_health_status = None
    ML_IMPORT_ERROR = str(e)
    print(f"[GramVet] ML package import failed: {ML_IMPORT_ERROR}", flush=True)

def now():
    return datetime.now(timezone.utc).isoformat()

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def hp(password):
    return hashlib.sha256((password + app.secret_key).encode()).hexdigest()

def ensure_column(c, table, column, definition):
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('farmer','vet','government')),
      pincode TEXT, village TEXT, ward TEXT, address TEXT,
      vet_auth_id TEXT, govt_auth_id TEXT, created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS villages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, pincode TEXT NOT NULL,
      latitude REAL, longitude REAL, created_at TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS villages (
    id INTEGER PRIMARY KEY,
    name TEXT,
    pincode TEXT,
    lat REAL, 
    lon REAL
    );

    CREATE TABLE IF NOT EXISTS vet_villages(
      vet_id INTEGER NOT NULL, village_id INTEGER NOT NULL,
      PRIMARY KEY(vet_id,village_id),
      FOREIGN KEY(vet_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(village_id) REFERENCES villages(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS animals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      species TEXT NOT NULL CHECK(species IN ('Cattle','Buffalo')),
      breed TEXT, age TEXT, farmer_id INTEGER NOT NULL, village_id INTEGER NOT NULL,
      ward TEXT NOT NULL, latitude REAL, longitude REAL, last_vaccinated TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(farmer_id) REFERENCES users(id),
      FOREIGN KEY(village_id) REFERENCES villages(id)
    );

    CREATE TABLE IF NOT EXISTS cases(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      animal_id INTEGER NOT NULL, farmer_id INTEGER NOT NULL, vet_id INTEGER,
      status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','IN_PROGRESS','CLOSED')),
      opened_at TEXT NOT NULL, closed_at TEXT,
      FOREIGN KEY(animal_id) REFERENCES animals(id),
      FOREIGN KEY(farmer_id) REFERENCES users(id),
      FOREIGN KEY(vet_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS health_reports(
      id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER,
      animal_id INTEGER NOT NULL, symptoms_json TEXT NOT NULL, notes TEXT,
      reported_at TEXT NOT NULL, ai_prediction_json TEXT, ai_model_version TEXT,
      FOREIGN KEY(case_id) REFERENCES cases(id),
      FOREIGN KEY(animal_id) REFERENCES animals(id)
    );

    CREATE TABLE IF NOT EXISTS case_actions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, vet_id INTEGER NOT NULL,
      action_type TEXT NOT NULL, details TEXT NOT NULL, action_at TEXT NOT NULL,
      FOREIGN KEY(case_id) REFERENCES cases(id),
      FOREIGN KEY(vet_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS vaccinations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, animal_id INTEGER NOT NULL, case_id INTEGER,
      farmer_id INTEGER NOT NULL, vet_id INTEGER NOT NULL, vaccine_name TEXT NOT NULL,
      dose REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'dose', administered_at TEXT NOT NULL,
      batch_no TEXT, notes TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(animal_id) REFERENCES animals(id),
      FOREIGN KEY(case_id) REFERENCES cases(id),
      FOREIGN KEY(farmer_id) REFERENCES users(id),
      FOREIGN KEY(vet_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS inventory_transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, resource_name TEXT NOT NULL, quantity_change REAL NOT NULL,
      unit TEXT NOT NULL DEFAULT 'units', reason TEXT NOT NULL, vet_id INTEGER, animal_id INTEGER,
      case_id INTEGER, created_at TEXT NOT NULL,
      FOREIGN KEY(vet_id) REFERENCES users(id), FOREIGN KEY(animal_id) REFERENCES animals(id),
      FOREIGN KEY(case_id) REFERENCES cases(id)
    );

    CREATE TABLE IF NOT EXISTS outbreak_assessments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, vet_id INTEGER NOT NULL,
      village_id INTEGER NOT NULL, predicted TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
      notes TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(case_id) REFERENCES cases(id),
      FOREIGN KEY(vet_id) REFERENCES users(id),
      FOREIGN KEY(village_id) REFERENCES villages(id)
    );

    CREATE TABLE IF NOT EXISTS resource_inventory(
      id INTEGER PRIMARY KEY AUTOINCREMENT, resource_name TEXT UNIQUE NOT NULL,
      available_qty REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'units',
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS resource_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, vet_id INTEGER NOT NULL,
      village_id INTEGER NOT NULL, case_count INTEGER NOT NULL DEFAULT 0,
      resource_json TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
      created_at TEXT NOT NULL, reviewed_at TEXT, reviewed_by INTEGER,
      FOREIGN KEY(vet_id) REFERENCES users(id),
      FOREIGN KEY(village_id) REFERENCES villages(id),
      FOREIGN KEY(reviewed_by) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      title TEXT NOT NULL, message TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'INFO',
      kind TEXT NOT NULL, case_id INTEGER, village_id INTEGER, created_at TEXT NOT NULL,
      read_at TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(case_id) REFERENCES cases(id),
      FOREIGN KEY(village_id) REFERENCES villages(id)
    );
    """)
    # Migration from the earlier prototype
    ensure_column(c, "users", "address", "TEXT")
    ensure_column(c, "users", "govt_auth_id", "TEXT")
    ensure_column(c, "villages", "created_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(c, "animals", "created_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(c, 'animals', 'sex', "TEXT NOT NULL DEFAULT 'Female'")
    ensure_column(c, "health_reports", "case_id", "INTEGER")
    ensure_column(c, "health_reports", "weather_json", "TEXT")
    ensure_column(c, "outbreak_assessments", "model_correct", "INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_animals_farmer ON animals(farmer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_animals_village ON animals(village_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cases_animal ON cases(animal_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cases_vet ON cases(vet_id,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,read_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vaccinations_animal ON vaccinations(animal_id,administered_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vaccinations_vet ON vaccinations(vet_id,administered_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_tx_resource ON inventory_transactions(resource_name,created_at)")
    # Migrate reports created by the earlier prototype into permanent cases.
    orphan_reports = c.execute("""
      SELECT hr.id,hr.animal_id,a.farmer_id
      FROM health_reports hr JOIN animals a ON a.id=hr.animal_id
      WHERE hr.case_id IS NULL ORDER BY hr.reported_at,hr.id
    """).fetchall()
    for hr in orphan_reports:
        old_case = c.execute("SELECT id FROM cases WHERE animal_id=? AND status='OPEN' ORDER BY opened_at LIMIT 1",(hr["animal_id"],)).fetchone()
        if old_case:
            cid=old_case["id"]
        else:
            a0=c.execute("SELECT village_id FROM animals WHERE id=?",(hr["animal_id"],)).fetchone()
            vv=assigned_vet(c,a0["village_id"]) if a0 else None
            c.execute("INSERT INTO cases(animal_id,farmer_id,vet_id,status,opened_at) VALUES(?,?,?,?,?)",
                      (hr["animal_id"],hr["farmer_id"],vv["id"] if vv else None,"OPEN",hr["reported_at"]))
            cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("UPDATE health_reports SET case_id=? WHERE id=?",(cid,hr["id"]))
    merge_duplicate_open_cases(c)
    try:
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_one_open_per_animal "
            "ON cases(animal_id) WHERE status != 'CLOSED'"
        )
    except sqlite3.OperationalError as e:
        print(f"[GramVet] open-case unique index skipped: {e}", flush=True)
    # Seed demo villages only; their coordinates intentionally remain unset.
    for v in ["Kothapalli","Rampur","Narsampet","Lakshmipur","Venkatapur","Mallapur","Gopalpur"]:
        c.execute("INSERT OR IGNORE INTO villages(name,pincode,created_at) VALUES(?,?,?)",(v,"506001",now()))

    # Hackathon demo accounts (idempotent).
    demo_users = [
      ("Government Demo", "+919000000001", "Gov@26128", "government", "506001", None, None, "District Livestock Health Office", None, "GOV-26128"),
      ("Dr. Vet One", "+919000000002", "Vet@26128", "vet", "506001", "Kothapalli", None, "Government Veterinary Centre - Kothapalli", "VET-DEMO-001", None),
      ("Dr. Vet Two", "+919000000003", "Vet@26128", "vet", "506001", "Narsampet", None, "Government Veterinary Centre - Narsampet", "VET-DEMO-002", None),
      ("Dr. Vet Three", "+919000000004", "Vet@26128", "vet", "506001", "Venkatapur", None, "Government Veterinary Centre - Venkatapur", "VET-DEMO-003", None),
    ]
    for i in range(10):
        demo_users.append((f"Farmer {i+1}", f"+9190000000{i+5:02d}", "Farmer@26128", "farmer", "506001", ["Kothapalli","Kothapalli","Rampur","Narsampet","Narsampet","Lakshmipur","Venkatapur","Venkatapur","Mallapur","Gopalpur"][i], None, f"Demo farmer address {i+1}", None, None))

    for name,phone,password,role,pincode,village,ward,address,vet_auth,govt_auth in demo_users:
        c.execute("""
          INSERT OR IGNORE INTO users(name,phone,password_hash,role,pincode,village,ward,address,vet_auth_id,govt_auth_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,(name,phone,hp(password),role,pincode,village,ward,address,vet_auth,govt_auth,now()))

    # Assign the three demo vets across the demo villages.
    vet_ids = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM users WHERE role='vet' AND name LIKE 'Dr. Vet %'").fetchall()}
    village_ids = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM villages").fetchall()}
    assignments = {
      "Dr. Vet One": ["Kothapalli", "Rampur", "Lakshmipur"],
      "Dr. Vet Two": ["Narsampet", "Mallapur"],
      "Dr. Vet Three": ["Venkatapur", "Gopalpur"],
    }
    for vet_name, village_names in assignments.items():
        for vn in village_names:
            if vet_name in vet_ids and vn in village_ids:
                c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)",(vet_ids[vet_name], village_ids[vn]))

    c.commit()
    c.close()

def current_user(c=None):
    own = c is None
    c = c or conn()
    uid = session.get("uid")
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    if own: c.close()
    return u

def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            c = conn()
            u = current_user(c)
            if not u:
                c.close()
                return jsonify(error="Login required"), 401
            if u["role"] not in roles:
                c.close()
                return jsonify(error="Access denied for this role."), 403
            try:
                return fn(u, c, *args, **kwargs)
            finally:
                # Endpoint may have closed only in exceptional paths; keep simple and robust.
                try: c.close()
                except: pass
        return wrapper
    return deco

def create_notification(c, user_id, title, message, severity="INFO", kind="GENERAL", case_id=None, village_id=None):
    c.execute("""
      INSERT INTO notifications(user_id,title,message,severity,kind,case_id,village_id,created_at)
      VALUES(?,?,?,?,?,?,?,?)
    """,(user_id,title,message,severity,kind,case_id,village_id,now()))

def village_for(c, name, pincode, lat=None, lon=None):
    pincode = pincode or ""
    c.execute("INSERT OR IGNORE INTO villages(name,pincode,latitude,longitude,created_at) VALUES(?,?,?,?,?)",
              (name,pincode,lat,lon,now()))
    v = c.execute("SELECT * FROM villages WHERE name=? AND pincode=?",(name,pincode)).fetchone()
    # Store coordinates only when supplied. Never invent coordinates.
    if v and lat is not None and lon is not None:
        c.execute("UPDATE villages SET latitude=?,longitude=? WHERE id=?",(lat,lon,v["id"]))
        v = c.execute("SELECT * FROM villages WHERE id=?",(v["id"],)).fetchone()
    return v

def accessible_animals(c, u):
    if u["role"] == "farmer":
        return c.execute("""
          SELECT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone
          FROM animals a JOIN villages v ON v.id=a.village_id
          JOIN users u ON u.id=a.farmer_id
          WHERE a.farmer_id=? AND a.species IN ('Cattle','Buffalo') ORDER BY a.id DESC
        """,(u["id"],)).fetchall()
    if u["role"] == "vet":
        return c.execute("""
          SELECT DISTINCT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone
          FROM animals a JOIN villages v ON v.id=a.village_id
          JOIN users u ON u.id=a.farmer_id
          JOIN vet_villages vv ON vv.village_id=a.village_id
          WHERE vv.vet_id=? AND a.species IN ('Cattle','Buffalo') ORDER BY v.name,a.name
        """,(u["id"],)).fetchall()
    return c.execute("""
      SELECT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone
      FROM animals a JOIN villages v ON v.id=a.village_id
      JOIN users u ON u.id=a.farmer_id ORDER BY a.id DESC
    """).fetchall()

def latest_report(c, animal_id):
    r = c.execute("SELECT * FROM health_reports WHERE animal_id=? ORDER BY reported_at DESC,id DESC LIMIT 1",(animal_id,)).fetchone()
    if not r: return None
    d = dict(r)
    d["symptoms"] = json.loads(d.pop("symptoms_json") or "[]")
    d["prediction"] = json.loads(d.pop("ai_prediction_json") or "null")
    d["weather"] = json.loads(d.pop("weather_json") or "null") if "weather_json" in d else None
    return d

def active_case(c, animal_id):
    return c.execute("SELECT * FROM cases WHERE animal_id=? AND status!='CLOSED' ORDER BY opened_at DESC LIMIT 1",(animal_id,)).fetchone()

def assigned_vet(c, village_id):
    row = c.execute("""
      SELECT u.* FROM users u JOIN vet_villages vv ON vv.vet_id=u.id
      WHERE vv.village_id=? AND u.role='vet' ORDER BY u.id LIMIT 1
    """,(village_id,)).fetchone()
    if row:
        return row
    # Signup villages may miss seed vet_villages — auto-link first demo vet.
    # Do NOT commit here; caller owns the transaction.
    vet = c.execute("SELECT * FROM users WHERE role='vet' ORDER BY id LIMIT 1").fetchone()
    if vet and village_id:
        exists = c.execute(
            "SELECT 1 FROM vet_villages WHERE vet_id=? AND village_id=?",
            (vet["id"], village_id),
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO vet_villages(vet_id,village_id) VALUES(?,?)",
                (vet["id"], village_id),
            )
        return vet
    return None

def merge_duplicate_open_cases(c):
    """Keep newest OPEN/IN_PROGRESS case per animal; close extras and reattach child rows."""
    dupes = c.execute("""
      SELECT animal_id FROM cases
      WHERE status!='CLOSED' GROUP BY animal_id HAVING COUNT(*)>1
    """).fetchall()
    for row in dupes:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM cases WHERE animal_id=? AND status!='CLOSED' ORDER BY id ASC",
            (row["animal_id"],),
        )]
        keep = ids[-1]
        for oid in ids[:-1]:
            c.execute("UPDATE health_reports SET case_id=? WHERE case_id=?", (keep, oid))
            c.execute("UPDATE case_actions SET case_id=? WHERE case_id=?", (keep, oid))
            c.execute("UPDATE outbreak_assessments SET case_id=? WHERE case_id=?", (keep, oid))
            c.execute("UPDATE notifications SET case_id=? WHERE case_id=?", (keep, oid))
            try:
                c.execute("UPDATE vaccinations SET case_id=? WHERE case_id=?", (keep, oid))
            except sqlite3.OperationalError:
                pass
            c.execute(
                "UPDATE cases SET status='CLOSED', closed_at=? WHERE id=?",
                (now(), oid),
            )

def parse_predicted_outbreak(prediction):
    if not isinstance(prediction, dict): return None
    for k in ("outbreak_status","outbreak","status"):
        v=prediction.get(k)
        if isinstance(v,bool): return "YES" if v else "NO"
        if isinstance(v,str):
            s=v.upper()
            if "HIGH" in s or s in ("YES","TRUE","CONFIRMED","LIKELY"): return "YES"
            if s in ("NO","FALSE","LOW","UNLIKELY","NONE"): return "NO"
    p = prediction.get("outbreak_probability")
    if isinstance(p,(int,float)): return "YES" if p >= 0.5 else "NO"
    return None

def _age_months(a):
    raw = a["age"] if "age" in a.keys() else ""
    try:
        val=float(str(raw).strip())
        if val < 1: return 12
        if val <= 30: return int(round(val*12))
        return int(round(val))
    except Exception:
        return 36

def _recent_treatment(c, animal_id):
    row=c.execute("SELECT details FROM case_actions ca JOIN cases cs ON cs.id=ca.case_id WHERE cs.animal_id=? AND ca.action_type='MEDICATION' ORDER BY ca.action_at DESC,ca.id DESC LIMIT 1",(animal_id,)).fetchone()
    if not row: return "Untreated"
    text=(row["details"] or "").lower()
    if "antibiotic" in text: return "Antibiotics"
    if "deworm" in text: return "Dewormed"
    return "Supportive Care"

def _vaccination_flags(c, animal_id):
    flags={"Vaccinated_FMD":0,"Vaccinated_HS":0,"Vaccinated_LSD":0,"Vaccinated_BQ":0,"Days_Since_Last_Vaccination":999}
    rows=c.execute("SELECT vaccine_name, administered_at FROM vaccinations WHERE animal_id=? ORDER BY administered_at DESC,id DESC",(animal_id,)).fetchall()
    latest=None
    for r in rows:
        name=(r["vaccine_name"] or "").upper()
        if "FMD" in name: flags["Vaccinated_FMD"]=1
        if "HS" in name or "HEMORRHAGIC" in name: flags["Vaccinated_HS"]=1
        if "LSD" in name or "LUMPY" in name: flags["Vaccinated_LSD"]=1
        if "BQ" in name or "BLACK QUARTER" in name: flags["Vaccinated_BQ"]=1
        latest = latest or r["administered_at"]
    if latest:
        try:
            last=datetime.fromisoformat(latest.replace("Z","+00:00"))
            flags["Days_Since_Last_Vaccination"]=max(0,(datetime.now(timezone.utc)-last.astimezone(timezone.utc)).days)
        except Exception: pass
    return flags

def _symptom_flags(symptoms):
    s={x.lower() for x in symptoms}
    return {
      "Fever": int("fever" in s or "high body temperature" in s),
      "Salivation": int("excessive salivation" in s),
      "Blisters_Mouth_Teats": int("mouth lesions / sores" in s),
      "Lameness": int("lameness / difficulty walking" in s),
      "Skin_Nodules": int("skin lesions / rash" in s),
      "Udder_Swelling": int("swelling" in s or "abnormal milk production" in s),
      "Respiratory_Distress": int("difficulty breathing" in s),
      "Abnormal_Milk": int("abnormal milk production" in s),
      "Diarrhea": int("diarrhoea" in s),
      "Bleeding_Orifices": 0,
    }

def call_disease_model(a, symptoms, weather=None, c=None):
    weather=weather or {}
    if predict_health_status is None:
        return {
            "connected":False,
            "error":"Disease model failed to run",
            "message":"Local disease model package is unavailable.",
            "detail":ML_IMPORT_ERROR,
            "predictions":[],
            "triage_risk":"Unknown",
            "escalation_probability":None,
            "outbreak_probability":None,
            "model_version":"ml-package-unavailable",
        }
    try:
        c=c or conn()
        v=c.execute("SELECT COUNT(*) n FROM animals WHERE village_id=?",(a["village_id"],)).fetchone()["n"]
        sick=c.execute("SELECT COUNT(DISTINCT animal_id) n FROM cases WHERE status!='CLOSED' AND animal_id IN (SELECT id FROM animals WHERE village_id=?)",(a["village_id"],)).fetchone()["n"]
        flags=_vaccination_flags(c,a["id"])
        payload={
          "Animal_Species":"Cow" if a["species"]=="Cattle" else "Buffalo",
          "Age_Months":_age_months(a), "Sex":a["sex"] if a["sex"] in ("Male","Female") else "Female",
          "Herd_Size":max(1,int(v or 1)), **flags,
          "Recent_Treatment":_recent_treatment(c,a["id"]),
          "Latitude":a["latitude"] if a["latitude"] is not None else 19.7515,
          "Longitude":a["longitude"] if a["longitude"] is not None else 75.7139,
          "Dist_Nearest_Outbreak_km":5.0,"Dist_Waterbody_km":1.2,
          "Temperature_C":weather.get("temperature_c") if weather.get("temperature_c") is not None else 30.0,
          "Humidity_Percent":weather.get("humidity_pct") if weather.get("humidity_pct") is not None else 70.0,
          "Rainfall_mm":weather.get("rainfall_mm") if weather.get("rainfall_mm") is not None else 0.0,
          "Season":weather.get("season") or get_season_india(),
          "District_Outbreaks_30d":c.execute("SELECT COUNT(*) FROM outbreak_assessments WHERE confirmed=1 AND created_at>=?",((datetime.now(timezone.utc).date().toordinal()-30),)).fetchone()[0] if False else 0,
          "Symptom_Onset_Days":2,"Sick_Animal_Count":int(sick),"Mortality_Count":0,**_symptom_flags(symptoms)
        }
        result=predict_health_status(payload)
        result["connected"]=True
        result["model_version"]="GramVet ML Package / RandomForest+DecisionTree+LogisticRegression"
        result["input_summary"]={"age_months":payload["Age_Months"],"sex":payload["Sex"],"herd_size":payload["Herd_Size"],"recent_treatment":payload["Recent_Treatment"],"vaccinations":flags,"weather":{k:payload[k] for k in ("Temperature_C","Humidity_Percent","Rainfall_mm","Season")}}
        # Convert model output into UI-friendly ranked predictions.
        disease=result.pop("Predicted_Disease",None)
        chance=result.pop("Outbreak_Disease_Chance",None)
        ranked=result.pop("Disease_Probabilities",None)
        if isinstance(ranked,list) and ranked:
            result["predictions"]=ranked[:5]
        elif disease:
            try: prob=float(str(chance).rstrip('%'))/100
            except Exception: prob=None
            result["predictions"]=[{"disease":disease,"probability":prob}]
        triage=result.get("Triage_Risk_Level")
        escalation=result.get("Outbreak_Escalation_Risk")
        result["triage_risk"]=triage
        try: result["escalation_probability"]=float(str(escalation).rstrip('%'))/100
        except Exception: result["escalation_probability"]=None
        # Outbreak risk for vet/gov views = escalation model output (bundled; no second server).
        result["outbreak_probability"]=result["escalation_probability"]
        result["outbreak_status"]=(
            "HIGH" if (result["escalation_probability"] or 0) >= 0.7
            else "MODERATE" if (result["escalation_probability"] or 0) >= 0.4
            else "LOW" if result["escalation_probability"] is not None else "UNKNOWN"
        )
        result["message"]="AI-assisted screening only. A veterinarian must review and treat the animal before the case can be closed."
        return result
    except Exception as e:
        return {
            "connected":False,
            "error":"Disease model failed to run",
            "detail":str(e),
            "predictions":[],
            "triage_risk":"Unknown",
            "escalation_probability":None,
            "outbreak_probability":None,
            "model_version":"ml-inference-error",
        }

def call_outbreak_model(records):
    url = os.environ.get("OUTBREAK_MODEL_URL")
    if not url:
        # Derive outbreak signal from latest disease-package escalation scores in `records`.
        probs=[]
        for r in records or []:
            p=r.get("outbreak_probability")
            if p is None: p=r.get("escalation_probability")
            if isinstance(p,(int,float)): probs.append(float(p))
        maxp=max(probs) if probs else None
        status="UNKNOWN" if maxp is None else ("HIGH" if maxp>=0.7 else "MODERATE" if maxp>=0.4 else "LOW")
        return {
            "connected":True,
            "message":"Outbreak risk from bundled escalation model.",
            "status":status,
            "outbreak_probability":maxp,
            "model_version":"escalation-from-disease-package",
        }
    import urllib.request
    payload=json.dumps({
        "radius_km":RADIUS_KM,
        "animals":records
    }).encode()
    try:
        req=urllib.request.Request(url,data=payload,headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=25) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"connected":True,"error":"Outbreak model request failed","detail":str(e),"status":"UNKNOWN","model_version":"unknown"}

def haversine(lat1,lon1,lat2,lon2):
    r=6371.0
    p1,p2=math.radians(lat1),math.radians(lat2)
    dphi=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    x=math.sin(dphi/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(x))

def nearest_village_ids(c, source):
    if source["latitude"] is None or source["longitude"] is None:
        return [source["id"]]
    ids=[]
    for v in c.execute("SELECT * FROM villages"):
        if v["latitude"] is None or v["longitude"] is None: 
            continue
        if haversine(source["latitude"],source["longitude"],v["latitude"],v["longitude"]) <= RADIUS_KM:
            ids.append(v["id"])
    return ids or [source["id"]]

@app.route("/")
def home():
    init()
    return render_template("index.html", symptoms=SYMPTOMS)

@app.get("/api/me")
def api_me():
    c=conn(); u=current_user(c); c.close()
    if not u: return jsonify(authenticated=False,user=None)
    return jsonify(authenticated=True,user={
        k:u[k] for k in ["id","name","phone","role","pincode","village","ward","address"]
    })

@app.post("/api/signup")
def signup():
    d=request.json or {}
    name=(d.get("name") or "").strip()
    phone=(d.get("phone") or "").strip().replace(" ","")
    password=d.get("password") or ""
    role=d.get("role")
    if phone.isdigit() and len(phone)==10: phone="+91"+phone
    if not name or not phone or len(password)<4 or role not in ("farmer","vet","government"):
        return jsonify(error="Complete all required fields. Password must be at least 4 characters."),400
    if role=="vet" and not d.get("vet_auth_id"):
        return jsonify(error="Government authorisation ID is required for vet accounts."),400
    if role=="government":
        gov_code=os.environ.get("GOVERNMENT_AUTH_CODE","GOV-26128")
        if d.get("govt_auth_id") != gov_code:
            return jsonify(error="Invalid government authorisation code."),403
    c=conn()
    try:
        c.execute("""
          INSERT INTO users(name,phone,password_hash,role,pincode,village,ward,address,vet_auth_id,govt_auth_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,(name,phone,hp(password),role,d.get("pincode"),d.get("village"),d.get("ward"),
             d.get("address"),d.get("vet_auth_id"),d.get("govt_auth_id"),now()))
        uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        if d.get("village"):
            lat = d.get("latitude")
            lon = d.get("longitude")
            
            # If browser GPS wasn't used, fallback to text-based geocoding
            if not lat or not lon:
                lat, lon = get_maharashtra_coords(d.get("village"), d.get("pincode"))
                
            village_for(c, d["village"], d.get("pincode"), lat, lon)
        c.commit(); session["uid"]=uid
        u=c.execute("SELECT id,name,phone,role,pincode,village,ward,address FROM users WHERE id=?",(uid,)).fetchone()
        return jsonify(ok=True,user=dict(u))
    except sqlite3.IntegrityError:
        return jsonify(error="This phone number is already registered. Please login."),409
    finally:
        c.close()

@app.post("/api/login")
def login():
    d=request.json or {}
    phone=(d.get("phone") or "").strip().replace(" ","")
    password=d.get("password") or ""
    if phone.isdigit() and len(phone)==10: phone="+91"+phone
    c=conn()
    u=c.execute("SELECT * FROM users WHERE phone=? AND password_hash=?",(phone,hp(password))).fetchone()
    if not u:
        c.close(); return jsonify(error="Incorrect phone number or password."),401
    session["uid"]=u["id"]; c.close()
    return jsonify(ok=True,user={k:u[k] for k in ["id","name","phone","role","pincode","village","ward","address"]})

@app.post("/api/logout")
def logout():
    session.clear(); return jsonify(ok=True)

@app.get("/api/animals")
@require_role("farmer","vet","government")
def animals(u,c):
    out=[]
    for a in accessible_animals(c,u):
        d=dict(a)
        d["latest"]=latest_report(c,a["id"])
        d["active_case"]=dict(active_case(c,a["id"])) if active_case(c,a["id"]) else None
        d["assigned_vet"]=dict(assigned_vet(c,a["village_id"])) if assigned_vet(c,a["village_id"]) else None
        out.append(d)
    return jsonify(animals=out)

@app.post("/api/animals")
@require_role("farmer")
def add_animal(u,c):
    d=request.json or {}
    if any(not str(d.get(k) or "").strip() for k in ["tag","name","species","village","ward"]):
        return jsonify(error="Tag, animal name, species, village and ward are required."),400
    if d["species"] not in ALLOWED_SPECIES:
        return jsonify(error="Only Cattle (Cow) and Buffalo are supported by this model."),400
    v=village_for(c,d["village"].strip(),d.get("pincode") or u["pincode"],d.get("latitude"),d.get("longitude"))
    try:
        c.execute("""
          INSERT INTO animals(tag,name,species,breed,age,sex,farmer_id,village_id,ward,latitude,longitude,last_vaccinated,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(d["tag"].strip(),d["name"].strip(),d["species"],d.get("breed"),d.get("age"),d.get("sex") if d.get("sex") in ("Male","Female") else "Female",u["id"],v["id"],
             d["ward"],d.get("latitude"),d.get("longitude"),d.get("last_vaccinated"),now()))
        c.commit()
        return jsonify(ok=True,animal_id=c.execute("SELECT last_insert_rowid()").fetchone()[0])
    except sqlite3.IntegrityError:
        return jsonify(error="Animal ID/tag already exists. Use the ID assigned to this livestock."),409

@app.put("/api/animals/<int:animal_id>")
@require_role("farmer")
def edit_animal(u,c,animal_id):
    a=c.execute("SELECT * FROM animals WHERE id=? AND farmer_id=?",(animal_id,u["id"])).fetchone()
    if not a: return jsonify(error="Animal not found or not owned by this farmer."),404
    d=request.json or {}
    # Identity is stable. Farmer may update basic registration fields, never the system ID.
    if d.get("species") and d["species"] not in ALLOWED_SPECIES:
        return jsonify(error="Only Cattle and Buffalo are supported."),400
    c.execute("""
      UPDATE animals SET name=?,breed=?,age=?,sex=?,ward=?,last_vaccinated=?,latitude=?,longitude=?
      WHERE id=?
    """,(d.get("name",a["name"]),d.get("breed",a["breed"]),d.get("age",a["age"]),d.get("sex",a["sex"]),d.get("ward",a["ward"]),
         d.get("last_vaccinated",a["last_vaccinated"]),d.get("latitude",a["latitude"]),
         d.get("longitude",a["longitude"]),animal_id))
    c.commit()
    return jsonify(ok=True)

def resolve_location(c, a, lat=None, lon=None):
    """Best available coordinates for weather lookup: live GPS supplied now > the
    animal's registered location > its village's location."""
    if lat is not None and lon is not None:
        return lat, lon, "gps"
    if a["latitude"] is not None and a["longitude"] is not None:
        return a["latitude"], a["longitude"], "animal_record"
    v = c.execute("SELECT * FROM villages WHERE id=?",(a["village_id"],)).fetchone()
    if v and v["latitude"] is not None and v["longitude"] is not None:
        return v["latitude"], v["longitude"], "village_record"
    return None, None, "unavailable"

@app.get("/api/weather")
@require_role("farmer","vet","government")
def weather_preview(u,c):
    lat=request.args.get("lat",type=float)
    lon=request.args.get("lon",type=float)
    animal_id=request.args.get("animal_id",type=int)
    source="gps" if (lat is not None and lon is not None) else None
    if (lat is None or lon is None) and animal_id:
        a=c.execute("SELECT * FROM animals WHERE id=?",(animal_id,)).fetchone()
        if a:
            lat,lon,source=resolve_location(c,a)
    if lat is None or lon is None:
        return jsonify(error="Location unavailable. Allow GPS access or register the village's coordinates."),400
    wx=fetch_weather(lat,lon)
    wx["location_source"]=source
    return jsonify(weather=wx)

@app.post("/api/cases/report")
@require_role("farmer")
def report_case(u,c):
    d=request.json or {}
    aid=d.get("animal_id")
    a=c.execute("SELECT * FROM animals WHERE id=? AND farmer_id=?",(aid,u["id"])).fetchone()
    if not a: return jsonify(error="Animal not found."),404
    symptoms=d.get("symptoms",[])
    if not isinstance(symptoms,list) or not symptoms or len(symptoms)>20 or any(s not in SYMPTOMS for s in symptoms):
        return jsonify(error="Select valid symptoms from the provided 20 model inputs."),400

    # Pull live weather for wherever the report is being filed from, feeding the
    # model's temperature/humidity/rainfall/season inputs.
    lat,lon,loc_source=resolve_location(c,a,d.get("latitude"),d.get("longitude"))
    weather=fetch_weather(lat,lon)
    weather["location_source"]=loc_source

    prediction=call_disease_model(a,symptoms,weather,c)
    # One open case per animal. Unique index + re-read after race.
    existing=active_case(c,a["id"])
    case_id=existing["id"] if existing else None
    if not case_id:
        try:
            c.execute(
                "INSERT INTO cases(animal_id,farmer_id,vet_id,status,opened_at) VALUES(?,?,?,?,?)",
                (a["id"],u["id"],None,"OPEN",now()),
            )
            case_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        except sqlite3.IntegrityError:
            existing=active_case(c,a["id"])
            if not existing:
                raise
            case_id=existing["id"]
    vet=assigned_vet(c,a["village_id"])
    if vet:
        c.execute("UPDATE cases SET vet_id=?,status='OPEN' WHERE id=?",(vet["id"],case_id))
        # The notification carries the model result but does not claim diagnosis.
        top = (prediction.get("predictions") or [{}])[0] if isinstance(prediction,dict) else {}
        disease=top.get("disease","possible livestock illness")
        prob=top.get("probability")
        probtxt=f" ({round(float(prob)*100)}%)" if isinstance(prob,(int,float)) else ""
        create_notification(c,vet["id"],"New livestock health case",
                            f"{u['name']} reported {a['name']} ({a['species']}) in {u['village'] or 'your assigned village'}. Preliminary model: {disease}{probtxt}. Review the case.",
                            "HIGH" if (isinstance(prob,(int,float)) and prob>=0.7) else "INFO",
                            "CASE",case_id,a["village_id"])
    c.execute("""
      INSERT INTO health_reports(case_id,animal_id,symptoms_json,notes,reported_at,ai_prediction_json,ai_model_version,weather_json)
      VALUES(?,?,?,?,?,?,?,?)
    """,(case_id,a["id"],json.dumps(symptoms),d.get("notes"),now(),json.dumps(prediction),prediction.get("model_version","unknown"),json.dumps(weather)))
    c.commit()
    return jsonify(ok=True,case_id=case_id,prediction=prediction,weather=weather,
                   assigned_vet={"id":vet["id"],"name":vet["name"],"phone":vet["phone"],"address":vet["address"]} if vet else None)

@app.get("/api/animals/<int:animal_id>/history")
@require_role("farmer","vet","government")
def animal_history(u,c,animal_id):
    a=c.execute("SELECT a.*,v.name village_name,f.name farmer_name,f.phone farmer_phone FROM animals a JOIN villages v ON v.id=a.village_id JOIN users f ON f.id=a.farmer_id WHERE a.id=?",(animal_id,)).fetchone()
    if not a:return jsonify(error="Animal not found"),404
    if u["role"]=="farmer" and a["farmer_id"]!=u["id"]:return jsonify(error="Access denied"),403
    if u["role"]=="vet" and not c.execute("SELECT 1 FROM vet_villages WHERE vet_id=? AND village_id=?",(u["id"],a["village_id"])).fetchone():return jsonify(error="Access denied"),403
    cases=[]
    for cs in c.execute("SELECT * FROM cases WHERE animal_id=? ORDER BY opened_at DESC,id DESC",(animal_id,)):
        d=dict(cs);d["reports"]=[];d["actions"]=[]
        for r in c.execute("SELECT * FROM health_reports WHERE case_id=? ORDER BY reported_at",(cs["id"],)):
            z=dict(r);z["symptoms"]=json.loads(z.pop("symptoms_json") or "[]");z["prediction"]=json.loads(z.pop("ai_prediction_json") or "null");z["weather"]=json.loads(z.pop("weather_json") or "null") if "weather_json" in z else None;d["reports"].append(z)
        d["actions"]=[dict(x) for x in c.execute("SELECT ca.*,u.name vet_name FROM case_actions ca JOIN users u ON u.id=ca.vet_id WHERE case_id=? ORDER BY action_at,id",(cs["id"],))]
        cases.append(d)
    vaccinations=[dict(x) for x in c.execute("SELECT v.*,u.name vet_name FROM vaccinations v JOIN users u ON u.id=v.vet_id WHERE v.animal_id=? ORDER BY administered_at DESC,id DESC",(animal_id,))]
    return jsonify(animal=dict(a),case_count=len(cases),cured_count=sum(1 for x in cases if x["status"]=="CLOSED"),cases=cases,vaccinations=vaccinations)

@app.get("/api/cases")
@require_role("farmer","vet","government")
def list_cases(u,c):
    q=(request.args.get("q") or "").strip()
    status=request.args.get("status")
    params=[]
    where=[]
    sql="""
      SELECT cs.*,a.name animal_name,a.tag animal_tag,a.species,a.farmer_id,
             f.name farmer_name,f.phone farmer_phone,f.address farmer_address,
             v.name village_name,v.pincode,
             vet.name vet_name,vet.phone vet_phone,vet.address vet_address
      FROM cases cs
      JOIN animals a ON a.id=cs.animal_id
      JOIN users f ON f.id=cs.farmer_id
      JOIN villages v ON v.id=a.village_id
      LEFT JOIN users vet ON vet.id=cs.vet_id
    """
    if u["role"]=="farmer":
        where.append("cs.farmer_id=?"); params.append(u["id"])
    elif u["role"]=="vet":
        where.append("cs.vet_id=?"); params.append(u["id"])
    if q:
        where.append("(a.name LIKE ? OR a.tag LIKE ? OR f.name LIKE ?)")
        qq=f"%{q}%"; params += [qq,qq,qq]
    if status in ("OPEN","IN_PROGRESS","CLOSED"):
        where.append("cs.status=?"); params.append(status)
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY cs.opened_at DESC"
    rows=c.execute(sql,params).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        d["reports"]=[]
        for rr in c.execute("SELECT * FROM health_reports WHERE case_id=? ORDER BY reported_at ASC,id ASC",(r["id"],)):
            rd=dict(rr); rd["symptoms"]=json.loads(rd.pop("symptoms_json") or "[]"); rd["prediction"]=json.loads(rd.pop("ai_prediction_json") or "null")
            rd["weather"]=json.loads(rd.pop("weather_json") or "null") if "weather_json" in rd else None
            d["reports"].append(rd)
        d["actions"]=[dict(x) for x in c.execute("""
          SELECT ca.*,u.name vet_name FROM case_actions ca JOIN users u ON u.id=ca.vet_id
          WHERE case_id=? ORDER BY action_at ASC,id ASC
        """,(r["id"],))]
        d["outbreak_assessments"]=[dict(x) for x in c.execute("SELECT * FROM outbreak_assessments WHERE case_id=? ORDER BY created_at ASC,id ASC",(r["id"],))]
        out.append(d)
    return jsonify(cases=out)

@app.get("/api/cases/<int:case_id>")
@require_role("farmer","vet","government")
def case_detail(u,c,case_id):
    # Reuse list endpoint logic through direct checks.
    cs=c.execute("SELECT * FROM cases WHERE id=?",(case_id,)).fetchone()
    if not cs:return jsonify(error="Case not found"),404
    if u["role"]=="farmer" and cs["farmer_id"]!=u["id"]:return jsonify(error="Access denied"),403
    if u["role"]=="vet" and cs["vet_id"]!=u["id"]:return jsonify(error="Case is not assigned to you"),403
    # government can read all cases, but cannot alter them
    rr = c.execute("""
      SELECT cs.*,a.name animal_name,a.tag animal_tag,a.species,f.name farmer_name,f.phone farmer_phone,
             v.name village_name,vet.name vet_name,vet.phone vet_phone,vet.address vet_address
      FROM cases cs JOIN animals a ON a.id=cs.animal_id JOIN users f ON f.id=cs.farmer_id
      JOIN villages v ON v.id=a.village_id LEFT JOIN users vet ON vet.id=cs.vet_id WHERE cs.id=?
    """,(case_id,)).fetchone()
    d=dict(rr)
    d["reports"]=[]
    for x in c.execute("SELECT * FROM health_reports WHERE case_id=? ORDER BY reported_at",(case_id,)):
        z=dict(x);z["symptoms"]=json.loads(z.pop("symptoms_json"));z["prediction"]=json.loads(z.pop("ai_prediction_json") or "null")
        z["weather"]=json.loads(z.pop("weather_json") or "null") if "weather_json" in z else None
        d["reports"].append(z)
    d["actions"]=[dict(x) for x in c.execute("SELECT ca.*,u.name vet_name FROM case_actions ca JOIN users u ON u.id=ca.vet_id WHERE case_id=? ORDER BY action_at",(case_id,))]
    d["vaccinations"]=[dict(x) for x in c.execute("SELECT v.*,u.name vet_name FROM vaccinations v JOIN users u ON u.id=v.vet_id WHERE case_id=? ORDER BY administered_at,id",(case_id,))]
    return jsonify(case=d)

@app.post("/api/vet/cases/<int:case_id>/action")
@require_role("vet")
def vet_action(u,c,case_id):
    cs=c.execute("SELECT * FROM cases WHERE id=? AND vet_id=?",(case_id,u["id"])).fetchone()
    if not cs:return jsonify(error="Case not found or not assigned to you."),404
    d=request.json or {}; typ=(d.get("action_type") or "").upper()
    if typ not in ("TEST","VACCINE","MEDICATION","NOTE","CURE"):
        return jsonify(error="Action type must be TEST, VACCINE, MEDICATION, NOTE or CURE."),400
    details=(d.get("details") or "").strip()
    if not details:return jsonify(error="Please enter action details."),400
    c.execute("INSERT INTO case_actions(case_id,vet_id,action_type,details,action_at) VALUES(?,?,?,?,?)",
              (case_id,u["id"],typ,details,d.get("action_at") or now()))
    if typ in ("TEST","VACCINE","MEDICATION"):
        c.execute("UPDATE cases SET status='IN_PROGRESS' WHERE id=? AND status='OPEN'",(case_id,))
    c.commit()
    create_notification(c,cs["farmer_id"],"Case updated",
                        f"Veterinarian updated case #{case_id}: {typ}.", "INFO","CASE",case_id, None)
    c.commit()
    return jsonify(ok=True)

@app.post("/api/vet/cases/<int:case_id>/vaccine")
@require_role("vet")
def vet_vaccine(u,c,case_id):
    cs=c.execute("SELECT * FROM cases WHERE id=? AND vet_id=?",(case_id,u["id"])).fetchone()
    if not cs:return jsonify(error="Case not found or not assigned to you."),404
    d=request.json or {}; name=(d.get("vaccine_name") or "").strip(); unit=(d.get("unit") or "dose").strip()
    try: dose=float(d.get("dose",1))
    except Exception: return jsonify(error="Dose must be numeric."),400
    if not name or dose<=0:return jsonify(error="Provide vaccine name and a positive dose."),400
    inv=c.execute("SELECT * FROM resource_inventory WHERE lower(resource_name)=lower(?) LIMIT 1",(name,)).fetchone()
    if not inv:return jsonify(error=f"No government inventory found for '{name}'. Ask government to stock it first."),400
    if float(inv["available_qty"]) < dose:return jsonify(error=f"Insufficient stock: {inv['available_qty']} {inv['unit']} available."),400
    ts=d.get("administered_at") or now()
    c.execute("UPDATE resource_inventory SET available_qty=available_qty-?,updated_at=? WHERE id=?",(dose,now(),inv["id"]))
    c.execute("INSERT INTO vaccinations(animal_id,case_id,farmer_id,vet_id,vaccine_name,dose,unit,administered_at,batch_no,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (cs["animal_id"],case_id,cs["farmer_id"],u["id"],name,dose,unit,ts,d.get("batch_no"),d.get("notes"),now()))
    c.execute("UPDATE animals SET last_vaccinated=? WHERE id=?",(ts[:10] if len(ts)>=10 else ts,cs["animal_id"]))
    details=f"{name} — {dose:g} {unit}" + (f" · batch {d.get('batch_no')}" if d.get('batch_no') else "") + (f" · {d.get('notes')}" if d.get('notes') else "")
    c.execute("INSERT INTO case_actions(case_id,vet_id,action_type,details,action_at) VALUES(?,?,?,?,?)",(case_id,u["id"],"VACCINE",details,ts))
    c.execute("INSERT INTO inventory_transactions(resource_name,quantity_change,unit,reason,vet_id,animal_id,case_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(inv["resource_name"],-dose,inv["unit"],"Vaccine administered",u["id"],cs["animal_id"],case_id,now()))
    create_notification(c,cs["farmer_id"],"Vaccination recorded",f"{name} ({dose:g} {unit}) was administered to the animal in case #{case_id}.","INFO","CASE",case_id)
    c.commit()
    return jsonify(ok=True,remaining_stock=float(inv["available_qty"])-dose)

@app.post("/api/vet/cases/<int:case_id>/close")
@require_role("vet")
def close_case(u,c,case_id):
    cs=c.execute("SELECT * FROM cases WHERE id=? AND vet_id=?",(case_id,u["id"])).fetchone()
    if not cs:return jsonify(error="Case not found or not assigned to you."),404
    if cs["status"]=="CLOSED":return jsonify(ok=True)
    cure=c.execute("SELECT 1 FROM case_actions WHERE case_id=? AND vet_id=? AND action_type='CURE' ORDER BY action_at DESC,id DESC LIMIT 1",(case_id,u["id"])).fetchone()
    if not cure:
        return jsonify(error="The case can be closed only after the veterinarian records a CURE action."),400
    c.execute("UPDATE cases SET status='CLOSED',closed_at=? WHERE id=?",(now(),case_id))
    create_notification(c,cs["farmer_id"],"Case closed",
                        f"Veterinarian closed case #{case_id}. The full history remains available.", "INFO","CASE",case_id)
    c.commit()
    return jsonify(ok=True)

@app.post("/api/vet/cases/<int:case_id>/model-review")
@require_role("vet")
def vet_model_review(u,c,case_id):
    cs=c.execute("SELECT * FROM cases WHERE id=? AND vet_id=?",(case_id,u["id"])).fetchone()
    if not cs:return jsonify(error="Case not found or not assigned to you."),404
    d=request.json or {}
    val=d.get("correct")
    if val not in (True,False):return jsonify(error="Choose whether the model prediction was correct."),400
    a=c.execute("SELECT * FROM animals WHERE id=?",(cs["animal_id"],)).fetchone()
    v=c.execute("SELECT * FROM villages WHERE id=?",(a["village_id"],)).fetchone()
    c.execute("""
      INSERT INTO outbreak_assessments(case_id,vet_id,village_id,predicted,confirmed,model_correct,notes,created_at)
      VALUES(?,?,?,?,?,?,?,?)
    """,(case_id,u["id"],v["id"],d.get("predicted"),0,int(val),d.get("notes"),now()))
    c.commit()
    return jsonify(ok=True)

@app.post("/api/vet/cases/<int:case_id>/outbreak")
@require_role("vet")
def vet_outbreak(u,c,case_id):
    cs=c.execute("SELECT * FROM cases WHERE id=? AND vet_id=?",(case_id,u["id"])).fetchone()
    if not cs:return jsonify(error="Case not found or not assigned to you."),404
    a=c.execute("SELECT * FROM animals WHERE id=?",(cs["animal_id"],)).fetchone()
    v=c.execute("SELECT * FROM villages WHERE id=?",(a["village_id"],)).fetchone()
    d=request.json or {}
    confirmed=1 if d.get("confirmed") else 0
    c.execute("""
      INSERT INTO outbreak_assessments(case_id,vet_id,village_id,predicted,confirmed,notes,created_at)
      VALUES(?,?,?,?,?,?,?)
    """,(case_id,u["id"],v["id"],d.get("predicted"),confirmed,d.get("notes"),now()))
    if confirmed:
        villages=nearest_village_ids(c,v)
        # Heavy alerts to all farmers + vets in the affected village group.
        recipients=set()
        for uid in c.execute("""
          SELECT DISTINCT id FROM users
          WHERE id IN (SELECT farmer_id FROM animals WHERE village_id IN (%s))
             OR id IN (SELECT vet_id FROM vet_villages WHERE village_id IN (%s))
        """%(",".join("?"*len(villages)), ",".join("?"*len(villages))),
          villages+villages):
            recipients.add(uid["id"])
        for uid in recipients:
            create_notification(c,uid,
                "CONFIRMED OUTBREAK ALERT",
                f"Veterinarian {u['name']} confirmed an outbreak in {v['name']}. Maintain heightened vigilance. This alert covers villages within {RADIUS_KM:g} km when village coordinates are available.",
                "CRITICAL","OUTBREAK",case_id,v["id"])
        # Government is explicitly notified only when a vet confirms.
        govs=c.execute("SELECT id FROM users WHERE role='government'").fetchall()
        for g in govs:
            create_notification(c,g,"Confirmed outbreak reported",
                f"Vet {u['name']} confirmed an outbreak in {v['name']}. Immediate government review is recommended.",
                "CRITICAL","OUTBREAK",case_id,v["id"])
    c.commit()
    return jsonify(ok=True,confirmed=bool(confirmed),alerted_villages=villages if confirmed else [])

@app.get("/api/notifications")
@require_role("farmer","vet","government")
def notifications(u,c):
    rows=c.execute("""
      SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 100
    """,(u["id"],)).fetchall()
    unread=sum(1 for r in rows if not r["read_at"])
    return jsonify(unread=unread,notifications=[dict(r) for r in rows])

@app.post("/api/notifications/read")
@require_role("farmer","vet","government")
def notifications_read(u,c):
    d=request.json or {}
    if d.get("all"):
        c.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",(now(),u["id"]))
    elif d.get("id"):
        c.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?",(now(),d["id"],u["id"]))
    c.commit();return jsonify(ok=True)

@app.get("/api/vet/overview")
@require_role("vet")
def vet_overview(u,c):
    villages=c.execute("""
      SELECT v.id,v.name,v.pincode,COUNT(DISTINCT a.id) animals,COUNT(DISTINCT a.farmer_id) farmers,
             SUM(CASE WHEN cs.status!='CLOSED' THEN 1 ELSE 0 END) active_cases
      FROM villages v JOIN vet_villages vv ON vv.village_id=v.id AND vv.vet_id=?
      LEFT JOIN animals a ON a.village_id=v.id
      LEFT JOIN cases cs ON cs.animal_id=a.id
      GROUP BY v.id ORDER BY v.name
    """,(u["id"],)).fetchall()
    # Owner-searchable animal list for assigned villages.
    animals=[]
    for a in accessible_animals(c,u):
        animals.append({"id":a["id"],"name":a["name"],"tag":a["tag"],"species":a["species"],
                        "farmer_name":a["farmer_name"],"farmer_phone":a["farmer_phone"],"village":a["village_name"]})
    return jsonify(villages=[dict(x) for x in villages],animals=animals)

@app.post("/api/vet/assign")
@require_role("vet")
def assign_village(u,c):
    d=request.json or {}
    v=village_for(c,d.get("village","").strip(),d.get("pincode",""))
    c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)",(u["id"],v["id"]))
    c.commit();return jsonify(ok=True)

@app.get("/api/vet/cases")
@require_role("vet")
def vet_cases(u,c):
    q=request.args.get("q","").strip()
    params=[u["id"]]
    extra=""
    if q:
        extra=" AND (a.name LIKE ? OR a.tag LIKE ? OR f.name LIKE ?)"
        qq=f"%{q}%";params.extend([qq,qq,qq])
    rows=c.execute(f"""
      SELECT cs.id,cs.status,cs.opened_at,cs.closed_at,a.name animal_name,a.tag animal_tag,a.species,
             f.name farmer_name,f.phone farmer_phone,v.name village_name
      FROM cases cs JOIN animals a ON a.id=cs.animal_id JOIN users f ON f.id=cs.farmer_id
      JOIN villages v ON v.id=a.village_id WHERE cs.vet_id=? {extra}
      ORDER BY CASE cs.status WHEN 'OPEN' THEN 0 WHEN 'IN_PROGRESS' THEN 1 ELSE 2 END,cs.opened_at DESC
    """,params).fetchall()
    return jsonify(cases=[dict(x) for x in rows])

@app.post("/api/vet/resource-request")
@require_role("vet")
def resource_request(u,c):
    d=request.json or {}
    village_id=d.get("village_id")
    v=c.execute("SELECT * FROM villages WHERE id=?",(village_id,)).fetchone()
    if not v:return jsonify(error="Village not found"),404
    # Calculate context from live cases.
    case_count=c.execute("""
      SELECT COUNT(*) FROM cases cs JOIN animals a ON a.id=cs.animal_id
      WHERE a.village_id=? AND cs.status!='CLOSED'
    """,(village_id,)).fetchone()[0]
    resources=d.get("resources") or []
    if not isinstance(resources,list) or not resources:return jsonify(error="Add at least one resource request."),400
    c.execute("""
      INSERT INTO resource_requests(vet_id,village_id,case_count,resource_json,reason,created_at)
      VALUES(?,?,?,?,?,?)
    """,(u["id"],village_id,case_count,json.dumps(resources),d.get("reason"),now()))
    rid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    for g in c.execute("SELECT id FROM users WHERE role='government'"):
        create_notification(c,g,"Veterinary resource request",
            f"Vet {u['name']} requested resources for {v['name']} ({case_count} active cases).",
            "HIGH","RESOURCE",None,village_id)
    c.commit()
    return jsonify(ok=True,request_id=rid,active_cases=case_count)

@app.get("/api/vet/resource-status")
@require_role("vet")
def resource_status(u,c):
    rows=c.execute("""
      SELECT rr.*,v.name village_name FROM resource_requests rr JOIN villages v ON v.id=rr.village_id
      WHERE rr.vet_id=? ORDER BY rr.created_at DESC
    """,(u["id"],)).fetchall()
    return jsonify(requests=[dict(x) for x in rows])

@app.get("/api/govt/overview")
@require_role("government")
def govt_overview(u,c):
    total_farmers=c.execute("SELECT COUNT(*) FROM users WHERE role='farmer'").fetchone()[0]
    total_vets=c.execute("SELECT COUNT(*) FROM users WHERE role='vet'").fetchone()[0]
    total_animals=c.execute("SELECT COUNT(*) FROM animals").fetchone()[0]
    vaccinated=c.execute("SELECT COUNT(DISTINCT animal_id) FROM vaccinations").fetchone()[0]
    active_cases=c.execute("SELECT COUNT(*) FROM cases WHERE status!='CLOSED'").fetchone()[0]
    closed_cases=c.execute("SELECT COUNT(*) FROM cases WHERE status='CLOSED'").fetchone()[0]
    confirmed_outbreaks=c.execute("SELECT COUNT(DISTINCT village_id) FROM outbreak_assessments WHERE confirmed=1").fetchone()[0]
    # Village risk: driven by stored model predictions / case density, not a fabricated probability.
    village_rows=[]
    for v in c.execute("SELECT * FROM villages ORDER BY name"):
        active=c.execute("""
          SELECT cs.id,hr.ai_prediction_json FROM cases cs
          JOIN animals a ON a.id=cs.animal_id
          LEFT JOIN health_reports hr ON hr.id=(SELECT id FROM health_reports WHERE case_id=cs.id ORDER BY reported_at DESC,id DESC LIMIT 1)
          WHERE a.village_id=? AND cs.status!='CLOSED'
        """,(v["id"],)).fetchall()
        risk_probs=[]
        outbreak_probs=[]
        diseases={}
        for x in active:
            p=json.loads(x["ai_prediction_json"] or "{}") if x["ai_prediction_json"] else {}
            op=p.get("outbreak_probability")
            if op is None: op=p.get("escalation_probability")
            if isinstance(op,(int,float)): outbreak_probs.append(float(op))
            for pred in (p.get("predictions") or []):
                if isinstance(pred,dict):
                    if isinstance(pred.get("probability"),(int,float)): risk_probs.append(float(pred["probability"]))
                    if pred.get("disease"): diseases[pred["disease"]]=diseases.get(pred["disease"],0)+1
        maxp=max(risk_probs) if risk_probs else None
        max_outbreak=max(outbreak_probs) if outbreak_probs else None
        confirmed=bool(c.execute("SELECT 1 FROM outbreak_assessments WHERE village_id=? AND confirmed=1 LIMIT 1",(v["id"],)).fetchone())
        village_rows.append({
            "id":v["id"],"name":v["name"],"pincode":v["pincode"],
            "latitude":v["latitude"],"longitude":v["longitude"],
            "lat":v["latitude"],"lon":v["longitude"],
            "animals":c.execute("SELECT COUNT(*) FROM animals WHERE village_id=?",(v["id"],)).fetchone()[0],
            "farmers":c.execute("SELECT COUNT(DISTINCT farmer_id) FROM animals WHERE village_id=?",(v["id"],)).fetchone()[0],
            "active_cases":len(active),"max_model_probability":maxp,
            "max_outbreak_probability":max_outbreak,"confirmed_outbreak":confirmed,
            "top_disease":max(diseases,key=diseases.get) if diseases else None
        })
    # Simple time-series for the dashboard.
    trend=[]
    for day in range(13,-1,-1):
        # ISO date in UTC; use SQLite date comparison.
        d=(datetime.now(timezone.utc).date()).toordinal()-day
        date=str(datetime.fromordinal(d).date())
        cnt=c.execute("SELECT COUNT(*) FROM cases WHERE date(opened_at)=?",(date,)).fetchone()[0]
        trend.append({"date":date,"cases":cnt})
    vets=[]
    for v in c.execute("SELECT id,name,phone,address FROM users WHERE role='vet' ORDER BY name"):
        handled=c.execute("SELECT COUNT(*) FROM cases WHERE vet_id=?",(v["id"],)).fetchone()[0]
        closed=c.execute("SELECT COUNT(*) FROM cases WHERE vet_id=? AND status='CLOSED'",(v["id"],)).fetchone()[0]
        vets.append({"id":v["id"],"name":v["name"],"phone":v["phone"],"address":v["address"],"handled":handled,"closed":closed})
    requests=[]
    for rr in c.execute("""
      SELECT rr.*,v.name village_name,u.name vet_name FROM resource_requests rr
      JOIN villages v ON v.id=rr.village_id JOIN users u ON u.id=rr.vet_id
      ORDER BY CASE rr.status WHEN 'PENDING' THEN 0 ELSE 1 END,rr.created_at DESC
    """):
        d=dict(rr);d["resources"]=json.loads(d.pop("resource_json") or "[]");requests.append(d)
    inventory=[dict(x) for x in c.execute("SELECT * FROM resource_inventory ORDER BY resource_name")]
    vaccination_count=c.execute("SELECT COUNT(*) FROM vaccinations").fetchone()[0]
    vaccines_by_type=[dict(x) for x in c.execute("SELECT vaccine_name,COUNT(*) doses,COALESCE(SUM(dose),0) quantity FROM vaccinations GROUP BY vaccine_name ORDER BY quantity DESC,vaccine_name")]
    vaccines_by_vet=[dict(x) for x in c.execute("SELECT v.vet_id,u.name vet_name,COUNT(*) doses,COALESCE(SUM(v.dose),0) quantity FROM vaccinations v JOIN users u ON u.id=v.vet_id GROUP BY v.vet_id,u.name ORDER BY quantity DESC,u.name")]
    vaccine_usage=[dict(x) for x in c.execute("SELECT it.resource_name,COALESCE(-SUM(CASE WHEN it.quantity_change<0 THEN it.quantity_change ELSE 0 END),0) used_quantity,ri.available_qty remaining_quantity,ri.unit FROM inventory_transactions it LEFT JOIN resource_inventory ri ON lower(ri.resource_name)=lower(it.resource_name) WHERE it.reason='Vaccine administered' GROUP BY it.resource_name,ri.available_qty,ri.unit ORDER BY used_quantity DESC,it.resource_name")]
    return jsonify(
        totals={"farmers":total_farmers,"vets":total_vets,"animals":total_animals,"vaccinated":vaccinated,
                "vaccination_pct":round((vaccinated/total_animals*100),1) if total_animals else 0,
                "vaccination_events":vaccination_count,"active_cases":active_cases,"closed_cases":closed_cases,"confirmed_outbreaks":confirmed_outbreaks},
        villages=village_rows,trend=trend,vets=vets,requests=requests,inventory=inventory,
        vaccinations={"by_type":vaccines_by_type,"by_vet":vaccines_by_vet,"inventory_usage":vaccine_usage},
        radius_km=RADIUS_KM
    )

@app.post("/api/govt/inventory")
@require_role("government")
def govt_inventory(u,c):
    d=request.json or {}
    name=(d.get("resource_name") or "").strip()
    qty=d.get("available_qty")
    unit=(d.get("unit") or "units").strip()
    if not name or not isinstance(qty,(int,float)) or qty<0:return jsonify(error="Provide resource name and non-negative quantity."),400
    c.execute("""
      INSERT INTO resource_inventory(resource_name,available_qty,unit,updated_at)
      VALUES(?,?,?,?)
      ON CONFLICT(resource_name) DO UPDATE SET available_qty=excluded.available_qty,unit=excluded.unit,updated_at=excluded.updated_at
    """,(name,float(qty),unit,now()))
    c.commit();return jsonify(ok=True)

@app.post("/api/govt/resource-request/<int:request_id>")
@require_role("government")
def govt_review_resource(u,c,request_id):
    rr=c.execute("SELECT * FROM resource_requests WHERE id=?",(request_id,)).fetchone()
    if not rr:return jsonify(error="Request not found"),404
    d=request.json or {};status=d.get("status")
    if status not in ("APPROVED","REJECTED"):return jsonify(error="Status must be APPROVED or REJECTED."),400
    c.execute("UPDATE resource_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?",(status,now(),u["id"],request_id))
    c.commit()
    create_notification(c,rr["vet_id"],f"Resource request {status.lower()}",
        f"Government marked resource request #{request_id} as {status.lower()}.","INFO","RESOURCE",None,rr["village_id"])
    c.commit();return jsonify(ok=True)

if __name__=="__main__":
    init()
    app.run(host=os.environ.get("HOST","127.0.0.1"),port=int(os.environ.get("PORT","5000")),debug=True)
