from dotenv import load_dotenv
from pymongo import MongoClient
import os

import os, json, sqlite3, hashlib, math, base64, uuid
from dotenv import load_dotenv
from pymongo import MongoClient

import sys
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, send_from_directory
import requests
import math

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

mongo_client = None
mongo_db = None
print("MongoDB configured" if MONGO_URI else "MongoDB URI not configured")

def _coerce_latlon(lat, lon):
    """Accept lat/lon only when both are real numbers inside valid ranges."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(la) and math.isfinite(lo)):
        return None, None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None, None
    return la, lo

def validate_and_format_phone(phone_raw):
    p = str(phone_raw or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+91"):
        p = p[3:]
    elif p.startswith("91") and len(p) == 12:
        p = p[2:]
    elif p.startswith("0") and len(p) == 11:
        p = p[1:]
    if len(p) == 10 and p.isdigit() and p[0] in ("6", "7", "8", "9"):
        return "+91" + p
    return None

def get_maharashtra_coords(village_name, pincode=""):
    # Returns (None, None) when the place cannot be resolved. Returning a
    # state centroid instead would stack every unresolved account on one
    # point, which breaks nearest-vet routing and fakes outbreak clusters.
    v_clean = str(village_name or "").strip()
    p_clean = str(pincode or "").strip()
    if not v_clean:
        return None, None
    candidates = []
    if p_clean:
        candidates.append(f"{v_clean}, {p_clean}, Maharashtra, India")
        candidates.append(f"{v_clean}, {p_clean}, India")
    candidates.append(f"{v_clean}, Maharashtra, India")
    candidates.append(f"{v_clean}, India")
    
    for q in candidates:
        try:
            url = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(q)}&format=json&limit=1"
            res = requests.get(url, headers={'User-Agent': 'GramVetApp/2.0'}, timeout=5).json()
            if res and len(res) > 0 and 'lat' in res[0] and 'lon' in res[0]:
                return float(res[0]['lat']), float(res[0]['lon'])
        except Exception:
            continue
    return None, None

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
    return "Winter"

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

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

mongo_client = None
mongo_db = None
print("MongoDB Atlas configured" if MONGO_URI else "MongoDB Atlas URI not configured", flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gramvet-local-dev-secret")

@app.route('/media/<path:filename>')
def serve_media(filename):
    media_dir = os.path.join(BASE, 'media')
    return send_from_directory(media_dir, filename)

SYMPTOMS = [
    "Fever / High Body Temperature",
    "Cough",
    "Nasal Discharge",
    "Difficulty Breathing",
    "Reduced Appetite",
    "Weakness / Lethargy",
    "Diarrhea",
    "Dehydration",
    "Excessive Salivation",
    "Mouth Lesions / Sores",
    "Lameness / Difficulty Walking",
    "Swelling",
    "Skin Lesions / Rash",
    "Eye Discharge / Redness",
    "Abortion / Reproductive Problem",
    "Abnormal Milk Production",
    "Weight Loss",
    "Reduced Rumination",
    "Ticks / External Parasites"
]

ALLOWED_SPECIES = {"Cattle", "Buffalo"}
RADIUS_KM = 5.0
VACCINE_CODES = ("FMD", "HS", "LSD", "BQ")      # the 4 disease vaccines
VET_START_STOCK = 25                                # each vet starts with 25 of EACH vaccine
LOW_STOCK_THRESHOLD = 10                            # alert when stock is BELOW this (9 alerts, 10 does not)
GOVT_START_STOCK = int(os.environ.get("GOVT_VACCINE_START_STOCK", "500"))  # central stock per vaccine, seeded once
ML_DIR = os.path.join(BASE, "ml_package")
if ML_DIR not in sys.path:
    sys.path.insert(0, ML_DIR)
ML_IMPORT_ERROR = None
try:
    from predict_disease import predict_disease, FEATURES as MODEL_FEATURES, CLASSES as MODEL_CLASSES, _bundle as _load_model_bundle
    # The artifact was trained with scikit-learn 1.6.1 and does not unpickle on
    # other versions, so load it once at startup to fail loudly and clearly.
    _load_model_bundle()
except Exception as e:
    predict_disease = None
    MODEL_FEATURES, MODEL_CLASSES = [], []
    import sklearn as _sk
    ML_IMPORT_ERROR = f"{e} (installed scikit-learn {_sk.__version__}; the model requires scikit-learn==1.6.1 - run: pip install -r requirements.txt)"
    print(f"[GramVet] Disease model unavailable: {ML_IMPORT_ERROR}", flush=True)

def now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()

def mongo_upsert(collection, document):
    """Push document to MongoDB; on failure, queue for later sync."""
    global mongo_client, mongo_db
    if not MONGO_URI:
        return
    try:
        if mongo_db is None:
            mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
            mongo_db = mongo_client["gramvet"]
        mongo_db[collection].update_one({"_id": document["_id"]}, {"$set": document}, upsert=True)
        # Mark as synced in queue if it exists
        c = conn()
        c.execute("UPDATE sync_queue SET status='SYNCED', synced_at=? WHERE collection=? AND document_id=? AND status='PENDING'",
                  (now(), collection, document["_id"]))
        c.commit()
        c.close()
    except Exception as e:
        # Queue for later sync instead of failing silently
        c = conn()
        try:
            doc_id = document.get("_id", str(document))
            c.execute("""
              INSERT INTO sync_queue(collection, document_id, document_json, created_at)
              VALUES(?, ?, ?, ?)
              ON CONFLICT(collection, document_id) DO UPDATE SET
                document_json=?, retry_count=retry_count+1, last_error=?
            """, (collection, doc_id, json.dumps(document), now(), json.dumps(document), str(e)))
            c.commit()
        except Exception as qe:
            pass
        finally:
            c.close()
        print(f"[GramVet] MongoDB sync queued for {collection}: {e}", flush=True)

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
    # Reset existing data if reset flag/file is detected or on clean start
    reset_marker = os.path.join(BASE, ".db_reset_done")
    if not os.path.exists(reset_marker):
        c.executescript("""
            PRAGMA writable_schema = 1;
            DELETE FROM sqlite_master WHERE type IN ('table', 'index', 'trigger');
            PRAGMA writable_schema = 0;
            VACUUM;
            PRAGMA integrity_check;
        """)
        c.commit()
        try:
            with open(reset_marker, "w") as f:
                f.write("done")
        except Exception:
            pass

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

    -- Exotel IVR sessions are isolated from the existing application data model.
    CREATE TABLE IF NOT EXISTS ivr_sessions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      call_sid TEXT UNIQUE NOT NULL,
      phone TEXT,
      farmer_id INTEGER,
      animal_id INTEGER,
      pincode TEXT,
      species TEXT,
      gender TEXT,
      vaccinated INTEGER,
      vaccine_code TEXT,
      symptoms_json TEXT NOT NULL DEFAULT '[]',
      state TEXT NOT NULL DEFAULT 'START',
      last_error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(farmer_id) REFERENCES users(id),
      FOREIGN KEY(animal_id) REFERENCES animals(id)
    );
    CREATE INDEX IF NOT EXISTS idx_ivr_sessions_phone ON ivr_sessions(phone);
    CREATE INDEX IF NOT EXISTS idx_ivr_sessions_animal ON ivr_sessions(animal_id);
    """)
    # Migration from the earlier prototype
    ensure_column(c, "users", "address", "TEXT")
    ensure_column(c, "users", "govt_auth_id", "TEXT")
    ensure_column(c, "users", "latitude", "REAL")
    ensure_column(c, "users", "longitude", "REAL")
    ensure_column(c, "users", "district", "TEXT")
    ensure_column(c, "users", "state", "TEXT")
    # Per-farmer vet ownership. vet_villages alone cannot express "farmer A moved to
    # vet 2 while farmer B in the same village stays with vet 1", so each farmer
    # carries the id of the vet who owns their whole record (animals, open and
    # closed cases, past reports, map presence).
    ensure_column(c, "users", "assigned_vet_id", "INTEGER")
    # Advisory notifications store WHICH advisory applies (disease, level, village);
    # the text is looked up in static/advisories.js in the reader's language.
    ensure_column(c, "notifications", "data_json", "TEXT")
    ensure_column(c, "notifications", "dedup_key", "TEXT")
    ensure_column(c, "villages", "created_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(c, "animals", "created_at", "TEXT NOT NULL DEFAULT ''")
    ensure_column(c, 'animals', 'sex', "TEXT NOT NULL DEFAULT 'Female'")
    ensure_column(c, "health_reports", "case_id", "INTEGER")
    ensure_column(c, "health_reports", "weather_json", "TEXT")
    ensure_column(c, "health_reports", "report_inputs_json", "TEXT")
    # report_case() writes audio_url, but the column was never created, so on a
    # fresh database every symptom report failed with an OperationalError.
    ensure_column(c, "health_reports", "audio_url", "TEXT")
    ensure_column(c, "outbreak_assessments", "model_correct", "INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_animals_farmer ON animals(farmer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_animals_village ON animals(village_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cases_animal ON cases(animal_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cases_vet ON cases(vet_id,status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,read_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vaccinations_animal ON vaccinations(animal_id,administered_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vaccinations_vet ON vaccinations(vet_id,administered_at)")
    # Sync queue for offline-first MongoDB replication
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            document_id TEXT NOT NULL,
            document_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'SYNCED', 'FAILED')),
            created_at TEXT NOT NULL,
            synced_at TEXT,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            UNIQUE(collection, document_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_tx_resource ON inventory_transactions(resource_name,created_at)")
    # Deduplicate villages — keep the row with the highest id per (name, pincode) group.
    # Remap all foreign keys (animals, vet_villages, cases, etc.) to the surviving row first.
    dup_groups = c.execute("""
        SELECT name, pincode, COUNT(*) cnt FROM villages GROUP BY name, pincode HAVING cnt > 1
    """).fetchall()
    for g in dup_groups:
        rows = c.execute(
            "SELECT id FROM villages WHERE name=? AND pincode=? ORDER BY (latitude IS NOT NULL) DESC, id DESC",
            (g["name"], g["pincode"])
        ).fetchall()
        keep_id = rows[0]["id"]
        drop_ids = [r["id"] for r in rows[1:]]
        for drop_id in drop_ids:
            # Remap references
            c.execute("UPDATE animals SET village_id=? WHERE village_id=?", (keep_id, drop_id))
            c.execute("UPDATE vet_villages SET village_id=? WHERE village_id=? AND NOT EXISTS (SELECT 1 FROM vet_villages WHERE vet_id=vet_villages.vet_id AND village_id=?)", (keep_id, drop_id, keep_id))
            c.execute("DELETE FROM vet_villages WHERE village_id=?", (drop_id,))
            c.execute("UPDATE resource_requests SET village_id=? WHERE village_id=?", (keep_id, drop_id))
            c.execute("UPDATE notifications SET village_id=? WHERE village_id=?", (keep_id, drop_id))
            c.execute("UPDATE outbreak_assessments SET village_id=? WHERE village_id=?", (keep_id, drop_id))
            c.execute("DELETE FROM villages WHERE id=?", (drop_id,))
    # Try to add unique index to prevent future duplicates
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_villages_name_pincode ON villages(name, pincode)")
    except Exception as e:
        print(f"[GramVet] Could not create villages unique index (likely duplicates remain): {e}", flush=True)

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
    backfill_farmer_vet_assignment(c)
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

    # Seeding requested accounts only
    demo_users = [
      ("Government Official", "+919000000001", "12345", "government", "506001", None, None, "District Livestock Health Office", None, "GOV-26128"),
      ("Dr. Veterinarian", "+919000000002", "12345", "vet", "506001", "Kothapalli", None, "Government Veterinary Centre", "VET-001", None),
      ("Demo Farmer", "+919000000003", "12345", "farmer", "506001", "Kothapalli", "Ward 1", "Farmer House", None, None),
    ]

    for name,phone,password,role,pincode,village,ward,address,vet_auth,govt_auth in demo_users:
        c.execute("""
          INSERT OR IGNORE INTO users(name,phone,password_hash,role,pincode,village,ward,address,vet_auth_id,govt_auth_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,(name,phone,hp(password),role,pincode,village,ward,address,vet_auth,govt_auth,now()))

    # Link vet to village if applicable
    vet_row = c.execute("SELECT id FROM users WHERE phone='+919000000002'").fetchone()
    vil_row = c.execute("SELECT id FROM villages WHERE name='Kothapalli'").fetchone()
    if vet_row and vil_row:
        c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (vet_row["id"], vil_row["id"]))

    # Vaccine inventory. CREATE IF NOT EXISTS + INSERT OR IGNORE only: restarting never resets stock.
    c.executescript("""
    CREATE TABLE IF NOT EXISTS vet_vaccine_stock(
      vet_id INTEGER NOT NULL, vaccine TEXT NOT NULL CHECK(vaccine IN ('FMD','HS','LSD','BQ')),
      quantity INTEGER NOT NULL CHECK(quantity>=0), updated_at TEXT NOT NULL,
      PRIMARY KEY(vet_id,vaccine),
      FOREIGN KEY(vet_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS vaccine_shipments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, vet_id INTEGER NOT NULL,
      vaccine TEXT NOT NULL CHECK(vaccine IN ('FMD','HS','LSD','BQ')),
      quantity INTEGER NOT NULL CHECK(quantity>0),
      status TEXT NOT NULL DEFAULT 'IN_TRANSIT' CHECK(status IN ('IN_TRANSIT','RECEIVED')),
      released_by INTEGER, released_at TEXT NOT NULL, received_at TEXT, received_by INTEGER,
      idempotency_key TEXT,
      FOREIGN KEY(vet_id) REFERENCES users(id),
      FOREIGN KEY(released_by) REFERENCES users(id),
      FOREIGN KEY(received_by) REFERENCES users(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vaccine_shipments_idem
      ON vaccine_shipments(released_by,idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_vaccine_shipments_vet ON vaccine_shipments(vet_id,status);
    CREATE TABLE IF NOT EXISTS vaccine_alerts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, vet_id INTEGER NOT NULL,
      vaccine TEXT NOT NULL CHECK(vaccine IN ('FMD','HS','LSD','BQ')),
      stock_at_alert INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','RESOLVED')),
      created_at TEXT NOT NULL, resolved_at TEXT, resolved_by_shipment_id INTEGER,
      FOREIGN KEY(vet_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_vaccine_alert_one_open
      ON vaccine_alerts(vet_id,vaccine) WHERE status='OPEN';
    CREATE TABLE IF NOT EXISTS vaccine_stock_transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, party TEXT NOT NULL CHECK(party IN ('VET','GOVT')),
      vet_id INTEGER, vaccine TEXT NOT NULL, quantity_change INTEGER NOT NULL, balance_after INTEGER NOT NULL,
      txn_type TEXT NOT NULL, shipment_id INTEGER, vaccination_id INTEGER, case_id INTEGER, animal_id INTEGER,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vaccine_txn_vet ON vaccine_stock_transactions(vet_id,vaccine,id);
    """)
    ensure_govt_stock(c)
    for _v in c.execute("SELECT id FROM users WHERE role='vet'").fetchall():
        ensure_vet_stock(c, _v["id"])

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

def create_notification(c, user_id, title, message, severity="INFO", kind="GENERAL", case_id=None, village_id=None, data=None, dedup_key=None):
    c.execute("""
      INSERT INTO notifications(user_id,title,message,severity,kind,case_id,village_id,created_at,data_json,dedup_key)
      VALUES(?,?,?,?,?,?,?,?,?,?)
    """,(user_id,title,message,severity,kind,case_id,village_id,now(),json.dumps(data) if data else None,dedup_key))

def upsert_notification(c, user_id, dedup_key, title, message, severity, kind, case_id, village_id, data):
    """One live notification per user + dedup_key over 7 days. If its level
    changed it is updated and marked unread (so it pops up again); otherwise
    only its content is refreshed."""
    cutoff = (datetime.fromisoformat(now()) - timedelta(days=7)).isoformat()
    ex = c.execute("""SELECT id,data_json FROM notifications WHERE user_id=? AND dedup_key=? AND created_at>=?
                      ORDER BY id DESC LIMIT 1""", (user_id, dedup_key, cutoff)).fetchone()
    if not ex:
        create_notification(c, user_id, title, message, severity, kind, case_id, village_id, data, dedup_key)
        return
    old = json.loads(ex["data_json"] or "{}")
    if (old.get("level"), old.get("confirmed")) != (data.get("level"), data.get("confirmed")):
        c.execute("""UPDATE notifications SET title=?,message=?,severity=?,data_json=?,case_id=COALESCE(?,case_id),
                     created_at=?,read_at=NULL WHERE id=?""",
                  (title, message, severity, json.dumps(data), case_id, now(), ex["id"]))
    else:
        c.execute("UPDATE notifications SET message=?,data_json=? WHERE id=?", (message, json.dumps(data), ex["id"]))

def case_disease(c, case_id):
    """Top model prediction on the case's latest report (canonical name) or None."""
    row = c.execute("""SELECT ai_prediction_json FROM health_reports WHERE case_id=?
                       ORDER BY reported_at DESC, id DESC LIMIT 1""", (case_id,)).fetchone()
    try:
        p = json.loads((row["ai_prediction_json"] if row else None) or "{}")
        return canonical_disease(((p.get("predictions") or [{}])[0] or {}).get("disease")) if isinstance(p, dict) else None
    except Exception:
        return None

def vet_confirmed_outbreak(c, village_id, disease):
    """Has a vet confirmed an outbreak of this disease in this village in the last 30 days?"""
    cutoff = (datetime.fromisoformat(now()) - timedelta(days=30)).isoformat()
    for r in c.execute("SELECT case_id FROM outbreak_assessments WHERE village_id=? AND confirmed=1 AND created_at>=?",
                       (village_id, cutoff)):
        if case_disease(c, r["case_id"]) == disease:
            return True
    return False

def notify_farmer_outbreak_advisory(c, disease, village_id, village_name, level, score, confirmed, case_id=None):
    """Outbreak advisory to every farmer with animals in the village (never for LOW)."""
    if level not in ("MODERATE", "HIGH", "CRITICAL") or not disease or disease == "Healthy":
        return
    data = {"adv": "farmer_outbreak", "disease": disease, "level": level, "village": village_name,
            "score": score, "confirmed": bool(confirmed)}
    title = f"Outbreak advisory: {disease} in {village_name} ({level})"
    msg = (f"{'Confirmed by a veterinarian' if confirmed else 'Possible'} {disease} outbreak in {village_name}. "
           f"Level: {level}. Follow the advisory and contact your veterinarian.")
    sev = "CRITICAL" if level == "CRITICAL" else "HIGH"
    for f in c.execute("""SELECT DISTINCT a.farmer_id FROM animals a JOIN users u ON u.id=a.farmer_id
                          WHERE a.village_id=? AND u.role='farmer'""", (village_id,)).fetchall():
        upsert_notification(c, f["farmer_id"], f"farmer_ob:{disease}:{village_id}", title, msg, sev,
                            "ADVISORY", case_id, village_id, data)

def village_for(c, name, pincode, lat=None, lon=None):
    pincode = pincode or ""
    c.execute("INSERT OR IGNORE INTO villages(name,pincode,latitude,longitude,created_at) VALUES(?,?,?,?,?)",
              (name,pincode,lat,lon,now()))
    v = c.execute("SELECT * FROM villages WHERE name=? AND pincode=?",(name,pincode)).fetchone()
    # Store coordinates only when supplied. Never invent coordinates.
    if v and lat is not None and lon is not None:
        c.execute("UPDATE villages SET latitude=?,longitude=? WHERE id=?",(lat,lon,v["id"]))
        v = c.execute("SELECT * FROM villages WHERE id=?",(v["id"],)).fetchone()
    # Sync village to MongoDB
    mongo_upsert("villages", {
        "_id": v["id"],
        "id": v["id"],
        "name": v["name"],
        "pincode": v["pincode"],
        "latitude": v["latitude"],
        "longitude": v["longitude"],
        "created_at": v["created_at"]
    })
    return v

def accessible_animals(c, u):
    if u["role"] == "farmer":
        return c.execute("""
          SELECT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone,u.latitude farmer_lat,u.longitude farmer_lon
          FROM animals a JOIN villages v ON v.id=a.village_id
          JOIN users u ON u.id=a.farmer_id
          WHERE a.farmer_id=? AND a.species IN ('Cattle','Buffalo') ORDER BY a.id DESC
        """,(u["id"],)).fetchall()
    if u["role"] == "vet":
        # Scoped by farmer ownership, not village. When a farmer is transferred to a
        # closer vet their animals leave this list immediately, even if another of
        # this vet's farmers still lives in the same village.
        return c.execute("""
          SELECT DISTINCT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone,u.latitude farmer_lat,u.longitude farmer_lon
          FROM animals a JOIN villages v ON v.id=a.village_id
          JOIN users u ON u.id=a.farmer_id
          WHERE u.assigned_vet_id=? AND a.species IN ('Cattle','Buffalo') ORDER BY v.name,a.name
        """,(u["id"],)).fetchall()
    return c.execute("""
      SELECT a.*,v.name village_name,u.name farmer_name,u.phone farmer_phone,u.latitude farmer_lat,u.longitude farmer_lon
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
    d["report_inputs"] = json.loads(d.pop("report_inputs_json") or "null") if "report_inputs_json" in d else None
    return d

def active_case(c, animal_id):
    return c.execute("SELECT * FROM cases WHERE animal_id=? AND status!='CLOSED' ORDER BY opened_at DESC LIMIT 1",(animal_id,)).fetchone()

def assigned_vet_by_gps(c, farmer_lat, farmer_lon):
    """Find the closest vet to a farmer by haversine lat/lng distance.
    Always returns the geographically nearest vet (no radius cutoff).
    Returns the vet user row or None.
    """
    vets = c.execute("SELECT * FROM users WHERE role='vet'").fetchall()
    if not vets:
        return None
    closest = None
    closest_dist = float("inf")
    for v in vets:
        if v["latitude"] is not None and v["longitude"] is not None:
            d = haversine(farmer_lat, farmer_lon, v["latitude"], v["longitude"])
            if d < closest_dist:
                closest_dist = d
                closest = v
    return closest or vets[0]  # fallback to first vet if none have GPS

def assigned_vet(c, village_id):
    """Assign a vet to a village using GPS of village centre; fallback to old vet_villages logic."""
    vil = c.execute("SELECT * FROM villages WHERE id=?", (village_id,)).fetchone()
    if vil and vil["latitude"] is not None and vil["longitude"] is not None:
        vet = assigned_vet_by_gps(c, vil["latitude"], vil["longitude"])
        if vet:
            # Ensure vet_villages mapping exists
            c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (vet["id"], village_id))
            return vet
    # Old fallback: vet_villages table
    row = c.execute("""
      SELECT u.* FROM users u JOIN vet_villages vv ON vv.vet_id=u.id
      WHERE vv.village_id=? AND u.role='vet' ORDER BY u.id LIMIT 1
    """,(village_id,)).fetchone()
    if row:
        return row
    # Last resort: first vet
    vet = c.execute("SELECT * FROM users WHERE role='vet' ORDER BY id LIMIT 1").fetchone()
    if vet and village_id:
        c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (vet["id"], village_id))
        return vet
    return None

def backfill_farmer_vet_assignment(c):
    """Give every farmer an owning vet, derived from their newest case, else from
    whichever vet covers their village. Runs once for data created before
    users.assigned_vet_id existed."""
    for f in c.execute("SELECT id,village FROM users WHERE role='farmer' AND assigned_vet_id IS NULL"):
        vet = c.execute(
            "SELECT vet_id FROM cases WHERE farmer_id=? AND vet_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (f["id"],)).fetchone()
        vet_id = vet["vet_id"] if vet else None
        if vet_id is None and f["village"]:
            row = c.execute("""
                SELECT vv.vet_id FROM vet_villages vv
                JOIN villages v ON v.id=vv.village_id
                WHERE v.name=? LIMIT 1
            """,(f["village"],)).fetchone()
            vet_id = row["vet_id"] if row else None
        if vet_id is not None:
            c.execute("UPDATE users SET assigned_vet_id=? WHERE id=?", (vet_id, f["id"]))


def transfer_farmer_to_vet(c, farmer_id, new_vet_id, reason="a closer veterinarian is now available"):
    """Move a farmer's ENTIRE record to a new vet: open cases, closed cases and their
    past health reports all follow the farmer. The old vet loses access unless they
    still serve another farmer in the same village.

    Returns the previous vet id, or None if nothing changed.
    """
    farmer = c.execute("SELECT * FROM users WHERE id=? AND role='farmer'", (farmer_id,)).fetchone()
    if not farmer:
        return None
    old_vet_id = farmer["assigned_vet_id"]
    if old_vet_id == new_vet_id:
        return None

    new_vet = c.execute("SELECT * FROM users WHERE id=? AND role='vet'", (new_vet_id,)).fetchone()
    if not new_vet:
        return None

    # 1. The farmer themselves.
    c.execute("UPDATE users SET assigned_vet_id=? WHERE id=?", (new_vet_id, farmer_id))

    # 2. Every case ever opened by this farmer - open AND closed - so history,
    #    past reports and closed cases move across too, not just live work.
    c.execute("UPDATE cases SET vet_id=? WHERE farmer_id=?", (new_vet_id, farmer_id))

    # 3. Villages this farmer has animals in become visible to the new vet.
    farmer_villages = [r["village_id"] for r in c.execute(
        "SELECT DISTINCT village_id FROM animals WHERE farmer_id=?", (farmer_id,))]
    for vid in farmer_villages:
        if vid is not None:
            c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (new_vet_id, vid))

    # 4. Drop the old vet's village link only where they no longer serve ANY farmer.
    #    A village can hold farmers belonging to different vets, so this must be
    #    checked per village rather than removed outright.
    if old_vet_id:
        for vid in farmer_villages:
            if vid is None:
                continue
            still_serving = c.execute("""
                SELECT 1 FROM users f
                JOIN animals a ON a.farmer_id=f.id
                WHERE f.assigned_vet_id=? AND a.village_id=? LIMIT 1
            """,(old_vet_id, vid)).fetchone()
            if not still_serving:
                c.execute("DELETE FROM vet_villages WHERE vet_id=? AND village_id=?", (old_vet_id, vid))

        create_notification(c, old_vet_id,
            "Farmer transferred to a closer vet",
            f"{farmer['name']} ({farmer['village'] or 'village'}) and all of their records - "
            f"open cases, closed cases and past reports - have moved to {new_vet['name']}, "
            f"because {reason}. They no longer appear on your dashboard.",
            "INFO", "GENERAL", None, None)

    create_notification(c, new_vet_id,
        "New farmer assigned to you",
        f"{farmer['name']} ({farmer['village'] or 'village'}) is now under your care, "
        f"including their full case history. Please review their open cases.",
        "HIGH", "GENERAL", None, None)

    create_notification(c, farmer_id,
        "Your veterinarian has changed",
        f"{new_vet['name']} is now your assigned veterinarian and can see your herd's "
        f"full history. They are your new primary contact.",
        "INFO", "GENERAL", None, None)

    return old_vet_id


def _reassign_farmers_to_vet(c, new_vet_id, vet_lat, vet_lon):
    """When a vet registers, pull over every farmer for whom this vet is strictly
    closer than their current vet. The whole farmer record moves - open cases,
    closed cases and past reports - via transfer_farmer_to_vet()."""
    new_vet = c.execute("SELECT * FROM users WHERE id=?", (new_vet_id,)).fetchone()
    if not new_vet or vet_lat is None or vet_lon is None:
        return

    moved = []
    farmers = c.execute("""
        SELECT id,name,village,latitude,longitude,assigned_vet_id
        FROM users WHERE role='farmer'
    """).fetchall()

    for f in farmers:
        if f["assigned_vet_id"] == new_vet_id:
            continue

        # Locate the farmer: their own coords, else any animal, else their village.
        lat, lon = f["latitude"], f["longitude"]
        if lat is None or lon is None:
            row = c.execute("""
                SELECT COALESCE(a.latitude, v.latitude) lat,
                       COALESCE(a.longitude, v.longitude) lon
                FROM animals a LEFT JOIN villages v ON v.id=a.village_id
                WHERE a.farmer_id=? AND COALESCE(a.latitude, v.latitude) IS NOT NULL
                LIMIT 1
            """,(f["id"],)).fetchone()
            if row:
                lat, lon = row["lat"], row["lon"]
        if lat is None or lon is None:
            continue

        dist_new = haversine(vet_lat, vet_lon, lat, lon)

        old_vet = None
        if f["assigned_vet_id"]:
            old_vet = c.execute("SELECT * FROM users WHERE id=?", (f["assigned_vet_id"],)).fetchone()

        if old_vet and old_vet["latitude"] is not None and old_vet["longitude"] is not None:
            dist_old = haversine(old_vet["latitude"], old_vet["longitude"], lat, lon)
            if dist_new >= dist_old:
                continue   # incumbent vet is equal or closer - leave the farmer alone
            reason = f"they are {round(dist_new,1)} km away versus {round(dist_old,1)} km"
        elif old_vet:
            reason = "the previous vet has no mapped location"
        else:
            reason = "no vet was assigned yet"

        if transfer_farmer_to_vet(c, f["id"], new_vet_id, reason) is not None or True:
            moved.append(f["name"])

    if moved:
        shown = ", ".join(moved[:5])
        extra = f" and {len(moved)-5} more" if len(moved) > 5 else ""
        create_notification(c, new_vet_id,
            f"{len(moved)} farmer(s) assigned to you",
            f"You are now the closest vet for: {shown}{extra}. "
            f"Their full records have been transferred to your dashboard.",
            "HIGH", "GENERAL", None, None)


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

def _parse_report_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=timezone.utc)
        except Exception:
            return None

def _clean_report_inputs(d):
    def positive_number(key, default=None):
        value=d.get(key)
        if value in (None, ""):
            return default
        try:
            value=float(value)
            return value if value >= 0 else default
        except Exception:
            return default

    vaccine_dates={}
    vaccine_months={}
    vaccine_flags={}
    latest=None
    today=datetime.now(timezone.utc)
    for vaccine in ("FMD","HS","LSD","BQ"):
        key=f"vaccinated_{vaccine.lower()}_date"
        raw=(d.get(key) or "").strip() if isinstance(d.get(key),str) else d.get(key)
        parsed=_parse_report_date(raw)
        vaccine_dates[vaccine]=raw or None
        if parsed:
            days=max(0,(today-parsed).days)
            months=max(0,int(days/30))
            vaccine_months[vaccine]=months
            vaccine_flags[f"Vaccinated_{vaccine}"]=1 if parsed <= today and days <= 365 else 0
            latest=parsed if latest is None or parsed > latest else latest
        else:
            vaccine_months[vaccine]=None
            vaccine_flags[f"Vaccinated_{vaccine}"]=0
    vaccine_flags["Days_Since_Last_Vaccination"]=max(0,(today-latest).days) if latest else 999
    return {
        "age_months": positive_number("age_months"),
        "herd_size": positive_number("herd_size", 1),
        "dist_waterbody_km": positive_number("dist_waterbody_km", 1.2),
        "vaccine_dates": vaccine_dates,
        "vaccine_months": vaccine_months,
        "vaccination_flags": vaccine_flags,
    }

def _symptom_flags(symptoms):
    """One 0/1 flag per model symptom field.

    The 19 UI symptoms map 1:1 onto the model's 19 symptom fields by name:
    "Fever / High Body Temperature" -> "Fever_High_Body_Temperature".
    """
    chosen = {_symptom_feature(s) for s in (symptoms or [])}
    return {f: int(f in chosen) for f in SYMPTOM_FEATURES}


def _symptom_feature(name):
    return str(name).replace(" / ", "_").replace(" ", "_")


SYMPTOM_FEATURES = [_symptom_feature(s) for s in SYMPTOMS]


def call_disease_model(a, symptoms, weather=None, c=None, report_inputs=None):
    """Run the disease classifier. It predicts the disease and its confidence
    ONLY - triage, outbreak and escalation are decided by rule-based logic
    elsewhere, using this output together with the case context.

    Returns:
      connected, predicted_disease, confidence (0-100),
      predictions [{disease, probability 0-1}] sorted high->low,
      model_inputs (the exact 32 values sent), model_version
    """
    failed = {"connected": False, "error": "Disease model failed to run", "predictions": [],
              "predicted_disease": None, "confidence": None,
              "model_version": "ml-unavailable"}
    if predict_disease is None:
        return {**failed, "detail": ML_IMPORT_ERROR}
    try:
        c = c or conn()
        weather = weather or {}
        report_inputs = report_inputs or {}
        flags = report_inputs.get("vaccination_flags") or _vaccination_flags(c, a["id"])
        herd = c.execute("SELECT COUNT(*) n FROM animals WHERE farmer_id=?", (a["farmer_id"],)).fetchone()["n"]

        model_inputs = {
            "Animal_Species": "Buffalo" if a["species"] == "Buffalo" else "Cow",
            "Age_Months": float(report_inputs.get("age_months") or _age_months(a) or 36),
            "Sex": a["sex"] if a["sex"] in ("Male", "Female") else "Female",
            "Herd_Size": max(1, int(report_inputs.get("herd_size") or herd or 1)),
            # Never-vaccinated animals were trained with 180-1100; clip to that range.
            "Days_Since_Last_Vaccination": min(1100, max(0, int(flags.get("Days_Since_Last_Vaccination", 999)))),
            "Vaccinated_FMD": int(flags.get("Vaccinated_FMD", 0)),
            "Vaccinated_HS": int(flags.get("Vaccinated_HS", 0)),
            "Vaccinated_LSD": int(flags.get("Vaccinated_LSD", 0)),
            "Vaccinated_BQ": int(flags.get("Vaccinated_BQ", 0)),
            **_symptom_flags(symptoms),
            "Temperature_C": float(weather["temperature_c"]) if weather.get("temperature_c") is not None else 28.0,
            "Humidity_Percent": float(weather["humidity_pct"]) if weather.get("humidity_pct") is not None else 65.0,
            "Rainfall_mm": max(0.0, float(weather["rainfall_mm"])) if weather.get("rainfall_mm") is not None else 0.0,
            "Season": weather.get("season") or get_season_india(),
        }
        out = predict_disease(model_inputs)

        # probabilities come back as percentages; the app stores 0-1 fractions.
        ranked = sorted(({"disease": k, "probability": round(v / 100.0, 4)} for k, v in out["probabilities"].items()),
                        key=lambda x: x["probability"], reverse=True)
        return {
            "connected": True,
            "predicted_disease": out["predicted_disease"],
            "confidence": out["confidence"],
            "predictions": ranked,
            "model_inputs": model_inputs,
            "weather_defaulted": weather.get("temperature_c") is None,
            "model_version": "GramVet disease classifier (32 inputs, 7 classes, scikit-learn 1.6.1)",
            "message": "AI-assisted screening only. A veterinarian must review and treat the animal before the case can be closed.",
        }
    except Exception as e:
        return {**failed, "detail": str(e), "model_version": "ml-inference-error"}


def _get_unique_animals(cases):
    """Return set of unique animal IDs from a list of case rows."""
    return {r["animal_id"] for r in cases if r.get("animal_id") is not None}

def _get_unique_farms(cases):
    """Return set of unique farm/owner IDs (farmer_id as farm proxy)."""
    return {r["farmer_id"] for r in cases if r.get("farmer_id") is not None}

def _get_affected_villages(cases):
    """Return set of unique non-empty village names from a list of cases."""
    names = set()
    for r in cases:
        v = (r.get("village_name") or "").strip()
        if v:
            names.add(v)
    return names

def _parse_case_date(value):
    """Parse a date string to UTC datetime; return None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=timezone.utc)
        except Exception:
            return None

def calculate_spatial_score(cases, ref_lat, ref_lon):
    """
    Score 0-100: how tightly are the cases clustered around (ref_lat, ref_lon)?
    One point per unique animal, so repeat reports add nothing.
    Tightness = 1 - (mean distance / 5 km): all animals at the reference point -> 1,
    all at the 5 km edge -> 0. Animals without coordinates (matched by village
    name) get no tightness credit rather than an invented position.
    """
    per_animal = {r["animal_id"]: r for r in cases if r.get("animal_id") is not None}
    n_animals = len(per_animal)
    if n_animals == 0:
        return 0
    n_farms = len(_get_unique_farms(per_animal.values()))

    dists = []
    for r in per_animal.values():
        if None not in (r.get("lat"), r.get("lon"), ref_lat, ref_lon):
            dists.append(min(RADIUS_KM, haversine(ref_lat, ref_lon, r["lat"], r["lon"])))
        else:
            dists.append(RADIUS_KM)
    tightness = 1.0 - (sum(dists) / len(dists)) / RADIUS_KM

    animal_score = min(1.0, n_animals / 8.0)
    farm_score = min(1.0, n_farms / 5.0)
    score = (0.45 * animal_score + 0.35 * farm_score + 0.20 * tightness) * 100
    return min(100, max(0, round(score)))

def calculate_temporal_score(cases, now_dt):
    """
    Score 0–100: how concentrated are the cases in the recent 7-day window?
    More unique animals appearing closer to 'today' → higher score.
    """
    if not cases:
        return 0
    unique_animals = _get_unique_animals(cases)
    n_animals = len(unique_animals)
    if n_animals == 0:
        return 0

    # Collect dates per unique animal (first occurrence in window)
    seen = {}
    for r in cases:
        aid = r.get("animal_id")
        if aid is None:
            continue
        dt = _parse_case_date(r.get("reported_at"))
        if dt is None:
            continue
        if aid not in seen or dt > seen[aid]:
            seen[aid] = dt

    if not seen:
        return 0

    # Spread: difference between earliest and latest case date in days
    dates = sorted(seen.values())
    span_days = max(0.0, (dates[-1] - dates[0]).total_seconds() / 86400)

    # More animals appearing within a short span = higher temporal score
    animal_score = min(1.0, n_animals / 5.0)
    # Concentration: 7 cases in 7 days is max; compressed into 2 days is even higher
    compression = 1.0 - min(1.0, span_days / 7.0)  # 0 span → 1.0, 7-day span → 0.0

    score = (0.6 * animal_score + 0.4 * compression) * 100
    return min(100, max(0, round(score)))

def calculate_growth_score(recent_unique_animals, baseline_unique_animals):
    """
    Score 0–100: is recent activity elevated versus the 28-day baseline?
    Avoids divide-by-zero. Does NOT automatically return 99 for zero baseline.

    baseline_weekly_rate = baseline_unique_animals / 4
    growth_ratio = recent_unique_animals / baseline_weekly_rate

    baseline=0 and recent>0 → treat as 'new emerging signal', moderate score.
    """
    if recent_unique_animals <= 0:
        return 0

    baseline_weekly_rate = baseline_unique_animals / 4.0

    if baseline_weekly_rate <= 0:
        # New/emerging disease — no history. Moderate signal (40 points).
        # Combined with other scores the engine decides the final risk level.
        return 40

    growth_ratio = recent_unique_animals / baseline_weekly_rate

    # Score: ratio=1 (no growth) → ~0; ratio=3 → moderate; ratio>=6 → near max
    score = min(1.0, (growth_ratio - 1.0) / 5.0) * 100 if growth_ratio > 1.0 else 0.0
    return min(100, max(0, round(score)))

def calculate_farm_spread_score(unique_animals, unique_farms):
    """
    Score 0–100: how widely is the disease distributed across independent farms?
    1 farm → very low; 4+ farms → high.
    """
    if unique_farms <= 1:
        return max(0, (unique_animals - 1) * 5)  # tiny signal if >1 animal on same farm
    # Score based on farm count, capped
    score = min(1.0, (unique_farms - 1) / 5.0) * 100
    return min(100, max(0, round(score)))

# ---------------------------------------------------------------------------
# Decision logic OUTSIDE the model.
# The classifier only says "which disease, how confident". Everything below is
# explicit, reviewable rules that combine that output with case context.
# ---------------------------------------------------------------------------

# severity  -> clinical urgency for the individual animal before other factors
# priority  -> disease that authorities treat as reportable; verify this list
#              against the state's notified-disease schedule before deployment
DISEASE_PROFILE = {
    "Anthrax":                  {"severity": "Critical", "priority": True},
    "Hemorrhagic Septicemia":   {"severity": "High",     "priority": True},
    "Black Quarter / Blackleg": {"severity": "High",     "priority": True},
    "FMD":                      {"severity": "High",     "priority": True},
    "Lumpy Skin Disease":       {"severity": "Moderate", "priority": True},
    "Mastitis":                 {"severity": "Moderate", "priority": False},
    "Healthy":                  {"severity": "Low",      "priority": False},
}
TRIAGE_LEVELS = ["Low", "Moderate", "High", "Critical"]
# Reports saved before the model swap use the old class names; map them so old
# and new reports of the same disease still cluster together.
LEGACY_DISEASE_NAMES = {
    "Lumpy_Skin_Disease": "Lumpy Skin Disease",
    "Hemorrhagic_Septicemia": "Hemorrhagic Septicemia",
    "Black_Quarter": "Black Quarter / Blackleg",
}


def canonical_disease(name):
    return LEGACY_DISEASE_NAMES.get(name, name)


RED_FLAG_SYMPTOMS = {"Difficulty Breathing", "Dehydration"}


def assess_triage(disease, confidence, symptoms, outbreak_status=None):
    """Clinical urgency for THIS animal. Returns (level, reasons)."""
    if not disease:
        return "Unknown", ["Disease model did not return a prediction"]
    reasons = []
    level = TRIAGE_LEVELS.index(DISEASE_PROFILE.get(disease, {}).get("severity", "Moderate"))
    reasons.append(f"{disease} baseline severity: {TRIAGE_LEVELS[level]}")

    conf = confidence or 0
    if disease == "Healthy":
        if len(symptoms) >= 3 and conf < 60:
            level = max(level, 1)
            reasons.append(f"'Healthy' at only {conf:.0f}% with {len(symptoms)} symptoms - needs checking")
    elif conf < 40 and level > 1:
        level -= 1
        reasons.append(f"Low model confidence ({conf:.0f}%) - lowered one level, vet to confirm")

    flags = sorted(RED_FLAG_SYMPTOMS.intersection(symptoms))
    if flags and level < 2:
        level = 2
        reasons.append("Red-flag symptom: " + ", ".join(flags))
    if len(symptoms) >= 5 and level < 1:
        level = 1
        reasons.append(f"{len(symptoms)} symptoms reported")

    if outbreak_status == "CRITICAL" and level < 3:
        level = 3
        reasons.append("Part of a CRITICAL outbreak cluster")
    elif outbreak_status == "HIGH" and level < 2:
        level = 2
        reasons.append("Part of a HIGH outbreak cluster")
    return TRIAGE_LEVELS[level], reasons


def assess_escalation(disease, confidence, outbreak_status):
    """Does this need action beyond the assigned vet? Returns (bool, reason)."""
    conf = confidence or 0
    if outbreak_status in ("HIGH", "CRITICAL"):
        return True, f"{outbreak_status} outbreak cluster - district/government response needed"
    if disease == "Anthrax" and conf >= 50:
        return True, "Suspected anthrax - zoonotic, report immediately"
    if DISEASE_PROFILE.get(disease, {}).get("priority") and outbreak_status == "MODERATE":
        return True, f"Priority disease ({disease}) with an emerging cluster"
    return False, "Handled by the assigned veterinarian"


def calculate_outbreak_risk(c, disease, ref_lat, ref_lon, ref_village_name=None):
    """
    Outbreak Risk Score for one disease around one reference point, per
    GramVet_Outbreak_Logic.md:
        score = 0.35 spatial + 0.30 temporal + 0.20 growth + 0.15 farm spread
    Recent window: last 7 days. Baseline: the 28 days before that (day -35 to -7).
    Disease comes from the ML model's prediction stored on each report.
    """
    # Stored timestamps are IST strings (see now()); window bounds must be in the
    # same zone, since SQLite compares them as text.
    now_dt = datetime.fromisoformat(now())
    recent_start = now_dt - timedelta(days=7)
    baseline_start = now_dt - timedelta(days=35)

    rows = c.execute("""
        SELECT hr.id report_id, hr.animal_id, hr.reported_at, hr.ai_prediction_json,
               cs.farmer_id,
               COALESCE(a.latitude, v.latitude) lat,
               COALESCE(a.longitude, v.longitude) lon,
               COALESCE(v.name, '') village_name,
               v.id village_id
        FROM health_reports hr
        JOIN cases cs ON cs.id = hr.case_id
        JOIN animals a ON a.id = hr.animal_id
        LEFT JOIN villages v ON v.id = a.village_id
        WHERE hr.reported_at >= ?
    """, (baseline_start.isoformat(),)).fetchall()

    def _disease_of(row):
        try:
            pred = json.loads(row["ai_prediction_json"] or "{}")
            top = (pred.get("predictions") or [{}])[0]
            return canonical_disease(top.get("disease"))
        except Exception:
            return None

    def _in_radius(row):
        if None not in (row["lat"], row["lon"], ref_lat, ref_lon):
            return haversine(ref_lat, ref_lon, row["lat"], row["lon"]) <= RADIUS_KM
        # No coordinates: fall back to same village name
        return bool(ref_village_name and row["village_name"]
                    and row["village_name"].strip().lower() == ref_village_name.strip().lower())

    matching = [dict(r) for r in rows if _disease_of(r) == disease and _in_radius(r)]
    recent_start_s = recent_start.isoformat()
    recent = [r for r in matching if r["reported_at"] >= recent_start_s]
    baseline = [r for r in matching if r["reported_at"] < recent_start_s]

    recent_animals = _get_unique_animals(recent)
    recent_farms = _get_unique_farms(recent)
    n_animals = len(recent_animals)
    n_farms = len(recent_farms)
    n_baseline = len(_get_unique_animals(baseline))
    baseline_weekly_rate = n_baseline / 4.0

    # Per-village breakdown (unique animals / farms in each affected village)
    villages = {}
    for r in recent:
        key = r["village_id"]
        v = villages.setdefault(key, {"village_id": key,
                                      "village": (r["village_name"] or "").strip() or "Unknown/Unmapped",
                                      "_animals": set(), "_farms": set()})
        v["_animals"].add(r["animal_id"])
        v["_farms"].add(r["farmer_id"])
    village_list = sorted(
        ({"village_id": v["village_id"], "village": v["village"],
          "unique_animals": len(v["_animals"]), "unique_farms": len(v["_farms"])} for v in villages.values()),
        key=lambda x: -x["unique_animals"])
    n_villages = len(village_list)

    spatial_score = calculate_spatial_score(recent, ref_lat, ref_lon)
    temporal_score = calculate_temporal_score(recent, now_dt)
    growth_score = calculate_growth_score(n_animals, n_baseline)
    farm_spread_score = calculate_farm_spread_score(n_animals, n_farms)

    score = (0.35 * spatial_score + 0.30 * temporal_score +
             0.20 * growth_score + 0.15 * farm_spread_score)
    outbreak_risk_score = int(round(max(0, min(100, score))))

    # Minimum evidence gate: below it the score must stay in the LOW band.
    minimum_met = n_animals >= 3 and n_farms >= 2
    if not minimum_met:
        outbreak_risk_score = min(outbreak_risk_score, 29)

    if outbreak_risk_score >= 80:
        outbreak_status = "CRITICAL"
    elif outbreak_risk_score >= 60:
        outbreak_status = "HIGH"
    elif outbreak_risk_score >= 30:
        outbreak_status = "MODERATE"
    else:
        outbreak_status = "LOW"

    evidence = []
    if n_animals:
        evidence.append(f"{n_animals} unique animal{'s' if n_animals != 1 else ''} affected")
    if n_farms:
        evidence.append(f"{n_farms} different farm{'s' if n_farms != 1 else ''} affected")
    if n_animals and ref_lat is not None:
        evidence.append(f"Cases detected within {RADIUS_KM:g} km")
    if n_animals >= 2:
        evidence.append("Multiple cases detected within 7 days")
    if n_villages > 1:
        evidence.append(f"Cases detected across {n_villages} villages")
    if n_animals and n_baseline == 0:
        evidence.append("No cases in previous 28 days - new/emerging signal")
    elif n_baseline and n_animals > baseline_weekly_rate:
        evidence.append(f"Recent activity exceeds historical baseline ({baseline_weekly_rate:.1f}/week)")
    if not minimum_met:
        evidence.append("Minimum evidence not met (needs 3+ animals on 2+ farms) - kept LOW")

    return {
        "disease": disease,
        "outbreak_risk_score": outbreak_risk_score,
        "outbreak_status": outbreak_status,
        "recent_cases_7d": n_animals,
        "baseline_cases_28d": n_baseline,
        "baseline_weekly_rate": round(baseline_weekly_rate, 2),
        "growth_ratio": round(n_animals / baseline_weekly_rate, 2) if n_baseline else None,
        "unique_animals": n_animals,
        "unique_farms": n_farms,
        "unique_villages": n_villages,
        "village": village_list[0]["village"] if village_list else (ref_village_name or "Unknown/Unmapped"),
        "villages": village_list,
        "radius_km": RADIUS_KM,
        "time_window_days": 7,
        "spatial_score": spatial_score,
        "temporal_score": temporal_score,
        "growth_score": growth_score,
        "farm_spread_score": farm_spread_score,
        "minimum_evidence_met": minimum_met,
        "evidence": evidence,
    }

def create_outbreak_vet_alert(c, disease, village, vet, result):
    """
    Village-specific alert for the responsible vet. If an alert for the same
    vet + disease + village exists from the last 7 days, update it instead of
    adding another; if the risk level changed, mark it unread so it resurfaces.
    """
    status = result["outbreak_status"]
    title = f"\u26a0\ufe0f POSSIBLE OUTBREAK \u2014 {disease} \u2014 {village['village']} ({status})"
    message = (
        f"Possible {disease} outbreak detected in {village['village']} village.\n"
        f"Risk Level: {status} | Risk Score: {result['outbreak_risk_score']}/100\n"
        f"In {village['village']}: {village['unique_animals']} animal(s), {village['unique_farms']} farm(s)\n"
        f"Whole cluster: {result['unique_animals']} animals, {result['unique_farms']} farms, "
        f"{result['unique_villages']} village(s)\n"
        f"Detected within: {RADIUS_KM:g} km | Recent window: 7 days\n"
        f"Evidence: {'; '.join(result['evidence'])}\n"
        f"Based on model-predicted (unconfirmed) cases. Please investigate."
    )
    sev = "CRITICAL" if status == "CRITICAL" else "HIGH"
    data = {"adv": "vet_outbreak", "disease": disease, "level": status, "village": village["village"],
            "score": result["outbreak_risk_score"], "animals": village["unique_animals"], "farms": village["unique_farms"],
            "cluster_animals": result["unique_animals"], "cluster_farms": result["unique_farms"],
            "confirmed": vet_confirmed_outbreak(c, village["village_id"], disease)}
    cutoff = (datetime.fromisoformat(now()) - timedelta(days=7)).isoformat()
    prefix = f"\u26a0\ufe0f POSSIBLE OUTBREAK \u2014 {disease} \u2014 {village['village']} ("
    existing = c.execute("""
        SELECT id, title FROM notifications
        WHERE user_id=? AND kind='OUTBREAK' AND village_id IS ? AND substr(title,1,?)=?
          AND created_at >= ?
        ORDER BY id DESC LIMIT 1
    """, (vet["id"], village["village_id"], len(prefix), prefix, cutoff)).fetchone()
    if existing:
        if existing["title"] != title:   # risk level changed -> resurface
            c.execute("UPDATE notifications SET title=?, message=?, severity=?, data_json=?, created_at=?, read_at=NULL WHERE id=?",
                      (title, message, sev, json.dumps(data), now(), existing["id"]))
        else:
            c.execute("UPDATE notifications SET message=?, data_json=? WHERE id=?", (message, json.dumps(data), existing["id"]))
        return
    create_notification(c, vet["id"], title, message, sev, "OUTBREAK", None, village["village_id"], data)

def run_outbreak_engine(c, disease, animal, ref_lat=None, ref_lon=None):
    """
    Run after a new report is saved. Computes the Outbreak Risk Score and, for
    MODERATE/HIGH/CRITICAL, alerts the vet responsible for each affected village.
    """
    if not disease:
        return None
    vil_row = c.execute("SELECT * FROM villages WHERE id=?", (animal["village_id"],)).fetchone()
    ref_village = vil_row["name"] if vil_row else None
    if ref_lat is None or ref_lon is None:
        if animal.get("latitude") is not None and animal.get("longitude") is not None:
            ref_lat, ref_lon = animal["latitude"], animal["longitude"]
        elif vil_row and vil_row["latitude"] is not None:
            ref_lat, ref_lon = vil_row["latitude"], vil_row["longitude"]

    result = calculate_outbreak_risk(c, disease, ref_lat, ref_lon, ref_village)
    if result["outbreak_status"] == "LOW":
        return result

    for village in result["villages"]:
        if village["village_id"] is None:
            continue
        vet = assigned_vet(c, village["village_id"])
        if vet:
            create_outbreak_vet_alert(c, disease, village, vet, result)
        confirmed = vet_confirmed_outbreak(c, village["village_id"], disease)
        notify_farmer_outbreak_advisory(c, disease, village["village_id"], village["village"],
                                        "CRITICAL" if confirmed else result["outbreak_status"],
                                        result["outbreak_risk_score"], confirmed)

    # Government users (existing behaviour, deduplicated per disease + village)
    score, status = result["outbreak_risk_score"], result["outbreak_status"]
    v_name = result["village"]
    gov_cutoff = (datetime.fromisoformat(now()) - timedelta(days=7)).isoformat()
    for g in c.execute("SELECT id FROM users WHERE role='government'").fetchall():
        existing_gov = c.execute("""
            SELECT 1 FROM notifications
            WHERE user_id=? AND kind='OUTBREAK' AND title=? AND created_at >= ? AND read_at IS NULL
            LIMIT 1
        """, (g["id"], f"\u26a0\ufe0f Outbreak Risk \u2014 {disease} \u2014 {v_name}", gov_cutoff)).fetchone()
        if not existing_gov:
            create_notification(
                c, g["id"], f"\u26a0\ufe0f Outbreak Risk \u2014 {disease} \u2014 {v_name}",
                f"Possible {disease} outbreak in {v_name}. Risk: {status} ({score}/100). "
                f"Animals: {result['unique_animals']}, Farms: {result['unique_farms']}.",
                "CRITICAL" if status == "CRITICAL" else "HIGH", "OUTBREAK", None, animal.get("village_id"))

    return result


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
        k:u[k] for k in ["id","name","phone","role","pincode","village","ward","address","latitude","longitude","district","state"] if k in u.keys()
    })

@app.get("/api/pincode_lookup")
def pincode_lookup():
    """Resolve a 6-digit Indian pincode to village/district/state and lat/lng via India Post API + Nominatim."""
    pin = (request.args.get("pincode") or "").strip()
    if not pin or len(pin) != 6 or not pin.isdigit():
        return jsonify(error="Invalid pincode. Must be a 6-digit number."), 400
    result = {"pincode": pin, "places": [], "latitude": None, "longitude": None}
    # 1. Try India Post API
    try:
        r = requests.get(f"https://api.postalpincode.in/pincode/{pin}", timeout=6)
        data = r.json()
        if data and data[0].get("Status") == "Success":
            post_offices = data[0].get("PostOffice") or []
            for po in post_offices:
                result["places"].append({
                    "name": po.get("Name", ""),
                    "district": po.get("District", ""),
                    "state": po.get("State", ""),
                    "division": po.get("Division", ""),
                    "region": po.get("Region", ""),
                    "block": po.get("Block", ""),
                    "branch_type": po.get("BranchType", ""),
                })
            # Use first post office for district/state info
            if post_offices:
                first = post_offices[0]
                result["district"] = first.get("District", "")
                result["state"] = first.get("State", "")
                result["division"] = first.get("Division", "")
                result["block"] = first.get("Block", "")
    except Exception:
        pass
    # 2. Get lat/lng via Nominatim for this pincode
    try:
        q = requests.utils.quote(f"{pin}, India")
        r = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&addressdetails=1",
            headers={"User-Agent": "GramVetApp/2.0"}, timeout=5
        )
        geo = r.json()
        if geo and len(geo) > 0:
            result["latitude"] = float(geo[0]["lat"])
            result["longitude"] = float(geo[0]["lon"])
            
            # Fallback to Nominatim address details if India Post failed
            addr = geo[0].get("address", {})
            if not result.get("state"):
                result["state"] = addr.get("state", "")
            if not result.get("district"):
                result["district"] = addr.get("county") or addr.get("state_district") or ""
            
            # Add a fallback place if India Post returned nothing
            if not result["places"]:
                place_name = addr.get("village") or addr.get("town") or addr.get("suburb") or addr.get("city") or ""
                if place_name:
                    result["places"].append({
                        "name": place_name,
                        "district": result["district"],
                        "state": result["state"]
                    })
    except Exception:
        pass
    return jsonify(result)


@app.post("/api/signup")
def signup():
    d=request.json or {}
    name=(d.get("name") or "").strip()
    phone_formatted = validate_and_format_phone(d.get("phone"))
    if not phone_formatted:
        return jsonify(error="Mobile number must be a 10-digit number starting with 9, 8, 7, or 6."),400
    phone = phone_formatted
    password=d.get("password") or ""
    role=d.get("role")
    if not name or len(password)<4 or role not in ("farmer","vet","government"):
        return jsonify(error="Complete all required fields. Password must be at least 4 digits/characters."),400
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
        lat, lon = _coerce_latlon(d.get("latitude"), d.get("longitude"))
        if lat is None or lon is None:
            lat, lon = get_maharashtra_coords(d.get("village") or d.get("address",""), d.get("pincode",""))
        # Farmers and vets must be placed on the map: their coordinates drive
        # nearest-vet assignment, the weather inputs to the disease model and
        # the outbreak engine's spatial window.
        if (lat is None or lon is None) and role in ("farmer","vet"):
            c.execute("DELETE FROM users WHERE id=?", (uid,)); c.commit()
            return jsonify(error="Location could not be determined. Use GPS, pick the spot on the map, or enter a valid pincode."),400
        if lat is not None and lon is not None:
            c.execute("UPDATE users SET latitude=?,longitude=? WHERE id=?", (lat, lon, uid))
        district = d.get("district", "")
        state = d.get("state", "")
        if district or state:
            c.execute("UPDATE users SET district=?,state=? WHERE id=?", (district, state, uid))
        if d.get("village"):
            village_for(c, d["village"].strip(), d.get("pincode"), lat, lon)
        # Give a new farmer their nearest vet straight away, so they show up on that
        # vet's local map before they have filed any case.
        if role == "farmer" and lat is not None and lon is not None:
            nearest = assigned_vet_by_gps(c, lat, lon)
            if nearest:
                c.execute("UPDATE users SET assigned_vet_id=? WHERE id=?", (nearest["id"], uid))
                if d.get("village"):
                    vrow = village_for(c, d["village"].strip(), d.get("pincode"), lat, lon)
                    c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)",
                              (nearest["id"], vrow["id"]))
        # If a new vet just registered: reassign open cases where this vet is closer
        if role == "vet" and lat is not None and lon is not None:
            _reassign_farmers_to_vet(c, uid, lat, lon)
        c.commit(); session["uid"]=uid
        u=c.execute("SELECT id,name,phone,role,pincode,village,ward,address,latitude,longitude,district,state FROM users WHERE id=?",(uid,)).fetchone()
        # Sync new user to MongoDB
        mongo_upsert("users", {
            "_id": uid,
            "id": uid,
            "name": u["name"],
            "phone": u["phone"],
            "role": u["role"],
            "pincode": u["pincode"],
            "village": u["village"],
            "latitude": u["latitude"],
            "longitude": u["longitude"],
            "created_at": now()
        })
        return jsonify(ok=True,user=dict(u))
    except sqlite3.IntegrityError:
        return jsonify(error="This phone number is already registered. Please login."),409
    finally:
        c.close()

@app.post("/api/login")
def login():
    d=request.json or {}
    phone_formatted = validate_and_format_phone(d.get("phone"))
    if not phone_formatted:
        return jsonify(error="Mobile number must be a 10-digit number starting with 9, 8, 7, or 6."),400
    phone = phone_formatted
    password=d.get("password") or ""
    if len(password) < 4:
        return jsonify(error="Password must be at least 4 digits/characters."),400
    c=conn()
    u=c.execute("SELECT * FROM users WHERE phone=? AND password_hash=?",(phone,hp(password))).fetchone()
    if not u:
        c.close(); return jsonify(error="Incorrect phone number or password."),401
    session["uid"]=u["id"]; c.close()
    return jsonify(ok=True,user={k:u[k] for k in ["id","name","phone","role","pincode","village","ward","address","latitude","longitude","district","state"] if k in u.keys()})

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
    
    # If animal location not provided, use farmer's registered location.
    # NOTE: u is a sqlite3.Row - it supports u["key"] but has no .get() method.
    animal_lat = d.get("latitude")
    animal_lon = d.get("longitude")
    if animal_lat is None or animal_lon is None:
        animal_lat = u["latitude"]
        animal_lon = u["longitude"]
    
    v=village_for(c,d["village"].strip(),d.get("pincode") or u["pincode"],animal_lat,animal_lon)
    try:
        c.execute("""
          INSERT INTO animals(tag,name,species,breed,age,sex,farmer_id,village_id,ward,latitude,longitude,last_vaccinated,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(d["tag"].strip(),d["name"].strip(),d["species"],d.get("breed"),d.get("age"),d.get("sex") if d.get("sex") in ("Male","Female") else "Female",u["id"],v["id"],
             d["ward"],animal_lat,animal_lon,d.get("last_vaccinated"),now()))
        c.commit()
        animal_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Sync new animal to MongoDB
        mongo_upsert("animals", {
            "_id": animal_id,
            "id": animal_id,
            "tag": d["tag"].strip(),
            "name": d["name"].strip(),
            "species": d["species"],
            "breed": d.get("breed"),
            "age": d.get("age"),
            "farmer_id": u["id"],
            "village_id": v["id"],
            "latitude": animal_lat,
            "longitude": animal_lon,
            "created_at": now()
        })
        return jsonify(ok=True,animal_id=animal_id)
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
    """Coordinates used for weather, model inputs and outbreak clustering.

    The farmer's REGISTERED location is the source of truth, so every report for
    a given farmer resolves to the same point. Live device GPS is not consulted.
    Order: farmer's registered point > the animal's own record > its village.
    """
    farmer = c.execute("SELECT latitude,longitude FROM users WHERE id=?",(a["farmer_id"],)).fetchone()
    if farmer and farmer["latitude"] is not None and farmer["longitude"] is not None:
        return farmer["latitude"], farmer["longitude"], "farmer_registered_location"
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
    source=None
    # For a farmer the registered location always wins, so the weather shown in
    # the report form is exactly what the model will be given on submit.
    if animal_id:
        a=c.execute("SELECT * FROM animals WHERE id=?",(animal_id,)).fetchone()
        if a:
            lat,lon,source=resolve_location(c,a)
    if (lat is None or lon is None) and u["role"]=="farmer" and u["latitude"] is not None:
        lat,lon,source=u["latitude"],u["longitude"],"farmer_registered_location"
    if lat is None or lon is None:
        return jsonify(error="Location unavailable. Register your location to enable weather."),400
    wx=fetch_weather(lat,lon)
    wx["location_source"]=source or "registered"
    return jsonify(weather=wx)


@app.post("/api/extract_symptoms")
def extract_symptoms():
    d = request.json or {}
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(symptoms=[])

    # First try LLM if Gemini key is available
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        try:
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
            prompt = f"Extract the exact symptoms mentioned in the following text. You MUST return ONLY a JSON list of strings exactly matching the official symptom list. If none match, return [].\nText: '{text}'\nOfficial List: {SYMPTOMS}"
            resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=10)
            if resp.ok:
                resp_json = resp.json()
                content = resp_json["candidates"][0]["content"]["parts"][0]["text"]
                
                # Simple extraction of the JSON array from response
                import re
                match = re.search(r"\[.*?\]", content, re.DOTALL)
                if match:
                    llm_syms = json.loads(match.group(0))
                    valid_syms = [s for s in llm_syms if s in SYMPTOMS]
                    return jsonify(symptoms=valid_syms, method="gemini")
        except Exception as e:
            print("LLM Extraction failed, falling back to dict:", e)

    # Fallback to comprehensive dictionary matching
    synonymMap = {
        "Fever / High Body Temperature": ["fever", "bukhar", "taap", "garam", "hot", "बुखार", "ताप", "गरम", "high temperature", "tez bukhar", "kadak taap", "तेज बुखार", "कडक ताप", "बुख़ार", "फीवर", "तापमान"],
        "Cough": ["cough", "khansi", "khokla", "खांसी", "खोकला", "कफ", "खाँसी", "कास"],
        "Nasal Discharge": ["nasal", "naak", "sardi", "नाक बहना", "नाक गळणे", "सर्दी", "नाक"],
        "Difficulty Breathing": ["breathing", "saans", "shwas", "dhaap", "सांस", "श्वास", "धाप"],
        "Reduced Appetite": ["appetite", "bhook", "kha nahi", "chara", "भूख", "भूक", "चारा", "खा नहीं"],
        "Weakness / Lethargy": ["weakness", "lethargy", "kamzor", "sust", "थकान", "कमजोर", "सुस्ती", "अशक्त", "थकवा"],
        "Diarrhea": ["diarrhoea", "diarrhea", "dast", "loose motion", "julab", "दस्त", "जुलाब", "संडास", "डायरिया", "डायरीया", "हगवण", "पातळ शेण", "पतला गोबर"],
        "Dehydration": ["dehydration", "pani", "tahan", "पानी", "तहान", "डिहाइड्रेशन", "डीहाइड्रेशन", "पाणी कमी"],
        "Excessive Salivation": ["saliva", "drool", "lar", "laar", "thook", "लार", "लाळ", "थूक"],
        "Mouth Lesions / Sores": ["mouth", "chhale", "tond", "muh", "छाले", "मुंह", "तोंड", "जख्म"],
        "Lameness / Difficulty Walking": ["lame", "limp", "langda", "chalne", "लंगड़ा", "लंगड", "चाल"],
        "Swelling": ["swelling", "sujan", "suj", "सूजन", "सूज", "फुगीर", "सवेलिंग"],
        "Skin Lesions / Rash": ["rash", "chakatte", "pural", "khaj", "चकत्ते", "पुरळ", "दाने", "खाज"],
        "Eye Discharge / Redness": ["eye", "aankh", "dole", "lal", "आंख", "डोळे", "लाल"],
        "Abnormal Milk Production": ["milk", "doodh", "dudh", "दूध", "दूध कमी", "दूध नहीं"],
        "Abortion / Reproductive Problem": ["abortion", "garbhpat", "pillu", "गर्भपात", "प्रजनन", "पिल्लू"],
        "Weight Loss": ["weight", "vazan", "wazan", "barik", "वजन", "बारीक", "दुबला"],
        "Reduced Rumination": ["ruminate", "jugali", "ravanth", "जुगाली", "रवंथ", "चावना", "रवंथ कमी"],
        "Ticks / External Parasites": ["tick", "killi", "gochid", "parjivi", "किल्ली", "गोचिड", "परजीवी", "जूं", "कीड़े"]
    }
    
    text_lower = text.lower()
    matched = []
    for sym in SYMPTOMS:
        kws = synonymMap.get(sym, [sym.lower()])
        for kw in kws:
            if kw in text_lower:
                matched.append(sym)
                break
    
    return jsonify(symptoms=matched, method="dictionary")


# -----------------------------------------------------------------------------
# Exotel IVR integration (GramVet sick-animal reporting)
# -----------------------------------------------------------------------------
# The IVR only reports an EXISTING livestock record. It never creates a farmer
# or animal. Exotel supplies the caller number in CallFrom/From and the Gather
# applets supply pincode, animal ID and symptom digits.
IVR_SYMPTOMS = {
    "1": "Fever / High Body Temperature",
    "2": "Cough",
    "3": "Difficulty Breathing",
    "4": "Reduced Appetite",
    "5": "Diarrhea",
}

IVR_VACCINES = {"1": "FMD", "2": "HS", "3": "LSD", "4": "BQ"}


def _ivr_param(*names):
    """Read an Exotel parameter regardless of its capitalization/legacy name."""
    for name in names:
        value = request.args.get(name)
        if value not in (None, ""):
            return str(value).strip()
        value = request.form.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _ivr_phone(raw):
    phone = validate_and_format_phone(raw)
    return phone or str(raw or "").strip()


def _ivr_get_session(c, call_sid, phone):
    row = c.execute("SELECT * FROM ivr_sessions WHERE call_sid=?", (call_sid,)).fetchone()
    if row:
        return row
    ts = now()
    c.execute("""
        INSERT INTO ivr_sessions(call_sid,phone,state,created_at,updated_at)
        VALUES(?,?,?,?,?)
    """, (call_sid, phone, "START", ts, ts))
    c.commit()
    return c.execute("SELECT * FROM ivr_sessions WHERE call_sid=?", (call_sid,)).fetchone()


def _ivr_update(c, call_sid, **fields):
    allowed = {"phone","farmer_id","animal_id","pincode","species","gender",
               "vaccinated","vaccine_code","symptoms_json","state","last_error","updated_at"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE ivr_sessions SET {sets} WHERE call_sid=?", (*fields.values(), call_sid))
    c.commit()


def _ivr_xml(message, status=200):
    # Exotel Passthru only needs a successful HTTP response to follow its 200 branch.
    return jsonify(ok=True, message=message), status


def _ivr_create_case(c, farmer, animal, symptoms, pincode=None):
    """Create an IVR case using the same ML/outbreak/notification pipeline as
    the existing farmer reporting route, without changing that route itself."""
    symptoms = [s for s in symptoms if s in SYMPTOMS]
    if not symptoms:
        raise ValueError("No valid symptoms supplied by IVR")

    # Keep the application's registered location as the authoritative outbreak
    # location. The caller's pincode is stored as IVR report metadata below.
    lat, lon, loc_source = resolve_location(c, animal)
    weather = fetch_weather(lat, lon)
    weather["location_source"] = loc_source

    report_inputs = _clean_report_inputs({})
    report_inputs["ivr_pincode"] = pincode
    report_inputs["ivr_report"] = True
    report_inputs["animal_id_source"] = "existing_animal_record"

    prediction = call_disease_model(animal, symptoms, weather, c, report_inputs)
    current_disease = prediction.get("predicted_disease")
    confidence = prediction.get("confidence")

    existing = active_case(c, animal["id"])
    case_id = existing["id"] if existing else None
    if not case_id:
        try:
            c.execute("INSERT INTO cases(animal_id,farmer_id,vet_id,status,opened_at) VALUES(?,?,?,?,?)",
                      (animal["id"], farmer["id"], None, "OPEN", now()))
            case_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        except sqlite3.IntegrityError:
            existing = active_case(c, animal["id"])
            if not existing:
                raise
            case_id = existing["id"]

    vet = None
    if farmer["assigned_vet_id"]:
        vet = c.execute("SELECT * FROM users WHERE id=? AND role='vet'",
                        (farmer["assigned_vet_id"],)).fetchone()
    if not vet and lat is not None and lon is not None:
        vet = assigned_vet_by_gps(c, lat, lon)
    if not vet:
        vet = assigned_vet(c, animal["village_id"])
    if vet and farmer["assigned_vet_id"] != vet["id"]:
        c.execute("UPDATE users SET assigned_vet_id=? WHERE id=?", (vet["id"], farmer["id"]))
    if vet:
        c.execute("UPDATE cases SET vet_id=?,status='OPEN' WHERE id=?", (vet["id"], case_id))
        c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)",
                  (vet["id"], animal["village_id"]))

    reported_at = now()
    c.execute("""
      INSERT INTO health_reports(case_id,animal_id,symptoms_json,notes,reported_at,
                                 ai_prediction_json,ai_model_version,weather_json,report_inputs_json,audio_url)
      VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (case_id, animal["id"], json.dumps(symptoms),
          "Reported through Exotel IVR [keypad phone]" +
          (f" | Pincode: {pincode}" if pincode else ""),
          reported_at, json.dumps(prediction), prediction.get("model_version", "unknown"),
          json.dumps(weather), json.dumps(report_inputs), None))
    report_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    outbreak_result = None
    if current_disease and current_disease != "Healthy":
        try:
            outbreak_result = run_outbreak_engine(c, current_disease, dict(animal), ref_lat=lat, ref_lon=lon)
        except Exception as e:
            print(f"[GramVet IVR] Outbreak engine error (non-fatal): {e}", flush=True)

    ob_status = outbreak_result["outbreak_status"] if outbreak_result else ("LOW" if current_disease else "UNKNOWN")
    ob_score = outbreak_result["outbreak_risk_score"] if outbreak_result else 0
    triage, triage_reasons = assess_triage(current_disease, confidence, symptoms, ob_status)
    escalate, escalation_reason = assess_escalation(current_disease, confidence, ob_status)
    prediction.update({
        "triage_risk": triage,
        "triage_reasons": triage_reasons,
        "outbreak_status": ob_status,
        "outbreak_risk_score": ob_score if current_disease else None,
        "outbreak_probability": ob_score / 100.0 if current_disease else None,
        "outbreak": outbreak_result,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
    })
    c.execute("UPDATE health_reports SET ai_prediction_json=? WHERE id=?", (json.dumps(prediction), report_id))

    if vet:
        disease = current_disease or "possible livestock illness"
        conftxt = f" ({confidence:.0f}%)" if isinstance(confidence, (int, float)) else ""
        create_notification(c, vet["id"], "New livestock health case",
                            f"{farmer['name']} reported {animal['name']} ({animal['species']}) through IVR. "
                            f"Model: {disease}{conftxt}. Triage: {triage}. Review the case.",
                            "HIGH" if triage in ("High", "Critical") else "INFO",
                            "CASE", case_id, animal["village_id"])

    if current_disease and triage in TRIAGE_LEVELS:
        lvl = None if current_disease == "Healthy" else triage
        create_notification(
            c, farmer["id"], f"Health advisory: {current_disease}" + (f" ({lvl})" if lvl else ""),
            f"Advisory for {animal['name']}: the reported symptoms are "
            + ("not a strong disease pattern." if current_disease == "Healthy"
               else f"consistent with {current_disease}. Contact a veterinarian for confirmation."),
            {"Critical": "CRITICAL", "High": "HIGH"}.get(triage, "INFO"),
            "ADVISORY", case_id, animal["village_id"],
            {"adv": "farmer_disease", "disease": current_disease, "level": lvl,
             "animal": animal["name"], "case_id": case_id})

    c.commit()

    mongo_upsert("cases", {
        "_id": case_id, "id": case_id, "animal_id": animal["id"],
        "farmer_id": farmer["id"], "vet_id": vet["id"] if vet else None,
        "status": "OPEN", "synced_at": now()
    })
    mongo_upsert("health_reports", {
        "_id": report_id, "id": report_id, "case_id": case_id, "animal_id": animal["id"],
        "symptoms": symptoms, "symptoms_json": json.dumps(symptoms),
        "notes": "Reported through Exotel IVR [keypad phone]",
        "reported_at": reported_at, "prediction": prediction,
        "ai_prediction_json": json.dumps(prediction),
        "ai_model_version": prediction.get("model_version", "unknown"),
        "weather": weather, "weather_json": json.dumps(weather),
        "report_inputs": report_inputs, "report_inputs_json": json.dumps(report_inputs),
        "synced_at": now()
    })

    return case_id, prediction


@app.get("/ivr/exotel/passthru")
def ivr_exotel_passthru():
    """Synchronous Exotel Passthru endpoint.

    Supported stages used by the final GramVet IVR:
      pincode -> store pincode in this call session
      animal_id -> find the existing animal owned by the caller
      symptom -> collect one or more symptom digits and create the sick report

    This endpoint is intentionally isolated from the existing authenticated web
    API. Exotel does not have a browser session, so farmer identity comes from
    CallFrom/From and is checked against users.phone.
    """
    call_sid = _ivr_param("CallSid", "call_sid", "CallSID")
    phone_raw = _ivr_param("CallFrom", "From", "from", "Callfrom")
    digits = _ivr_param("digits", "Digits", "DigitsEntered")
    stage = (_ivr_param("stage") or "").lower()
    phone = _ivr_phone(phone_raw)

    if not call_sid:
        return _ivr_xml("Missing CallSid", 400)
    if not stage:
        return _ivr_xml("Missing stage", 400)

    c = conn()
    try:
        sess = _ivr_get_session(c, call_sid, phone)
        _ivr_update(c, call_sid, phone=phone)

        if stage == "pincode":
            pin = "".join(ch for ch in digits if ch.isdigit())
            if len(pin) != 6:
                _ivr_update(c, call_sid, state="PINCODE_INVALID", last_error="Pincode must be 6 digits")
                return _ivr_xml("Invalid pincode", 400)
            _ivr_update(c, call_sid, pincode=pin, state="PINCODE_OK", last_error=None)
            return _ivr_xml("Pincode accepted")

        if stage == "animal_id":
            raw_id = "".join(ch for ch in digits if ch.isdigit())
            if not raw_id or len(raw_id) > 2:
                _ivr_update(c, call_sid, state="ANIMAL_ID_INVALID", last_error="Animal ID must be numeric and up to 2 digits")
                return _ivr_xml("Invalid animal ID", 400)

            # The caller must already be a registered farmer.
            # Exotel may send the same Indian number as +91XXXXXXXXXX,
            # 91XXXXXXXXXX, 0XXXXXXXXXX, or XXXXXXXXXX. Compare the
            # normalized last 10 digits so all valid formats match.
            caller_digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
            if caller_digits.startswith("91") and len(caller_digits) >= 12:
                caller_digits = caller_digits[-10:]
            elif caller_digits.startswith("0") and len(caller_digits) == 11:
                caller_digits = caller_digits[-10:]
            else:
                caller_digits = caller_digits[-10:]

            farmer = c.execute(
                """
                SELECT * FROM users
                WHERE role='farmer'
                  AND REPLACE(REPLACE(REPLACE(phone, '+91', ''), ' ', ''), '-', '') LIKE ?
                """,
                (f"%{caller_digits}",)
            ).fetchone()
            if not farmer:
                _ivr_update(c, call_sid, state="FARMER_NOT_FOUND", last_error="Caller is not a registered farmer")
                return _ivr_xml("Farmer not found", 403)

            # The farmer enters the visible Assigned ID/tag (for example, "4"),
            # not SQLite's internal animals.id.
            animal = c.execute(
                "SELECT * FROM animals WHERE tag=? AND farmer_id=?",
                (raw_id, farmer["id"])
            ).fetchone()
            if not animal:
                _ivr_update(c, call_sid, farmer_id=farmer["id"], state="ANIMAL_ID_NOT_FOUND",
                            last_error="Animal ID not found for this farmer")
                return _ivr_xml("Animal ID not found", 404)

            _ivr_update(c, call_sid, farmer_id=farmer["id"], animal_id=animal["id"],
                        species=animal["species"], gender=animal["sex"] if "sex" in animal.keys() else None,
                        state="ANIMAL_OK", last_error=None)
            return _ivr_xml("Animal verified")

        if stage == "symptom":
            # Gather sends multiple digits such as 134#; map each digit to a
            # symptom. Repeated digits are de-duplicated while preserving order.
            selected = []
            for d in digits:
                if d in IVR_SYMPTOMS and IVR_SYMPTOMS[d] not in selected:
                    selected.append(IVR_SYMPTOMS[d])
            if not selected:
                _ivr_update(c, call_sid, state="SYMPTOM_INVALID", last_error="No valid symptom digit")
                return _ivr_xml("No valid symptoms", 400)

            sess = c.execute("SELECT * FROM ivr_sessions WHERE call_sid=?", (call_sid,)).fetchone()
            if not sess or not sess["farmer_id"] or not sess["animal_id"]:
                _ivr_update(c, call_sid, state="SESSION_INCOMPLETE", last_error="Farmer/animal not verified")
                return _ivr_xml("IVR session incomplete", 409)

            farmer = c.execute("SELECT * FROM users WHERE id=? AND role='farmer'", (sess["farmer_id"],)).fetchone()
            animal = c.execute("SELECT * FROM animals WHERE id=? AND farmer_id=?", (sess["animal_id"], sess["farmer_id"])).fetchone()
            if not farmer or not animal:
                _ivr_update(c, call_sid, state="ANIMAL_NOT_FOUND", last_error="Existing animal could not be verified")
                return _ivr_xml("Animal could not be verified", 404)

            _ivr_update(c, call_sid, symptoms_json=json.dumps(selected), state="SYMPTOMS_OK", last_error=None)
            try:
                case_id, prediction = _ivr_create_case(c, farmer, animal, selected, sess["pincode"])
                _ivr_update(c, call_sid, state="REPORT_CREATED", last_error=None)
                return _ivr_xml(f"Report created: {case_id}")
            except Exception as e:
                c.rollback()
                print(f"[GramVet IVR] report creation failed: {e}", flush=True)
                _ivr_update(c, call_sid, state="REPORT_FAILED", last_error=str(e))
                return _ivr_xml("Report creation failed", 500)

        return _ivr_xml("Unknown stage", 400)
    finally:
        c.close()


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
    report_inputs=_clean_report_inputs(d)
    # Location is ALWAYS the registered one (farmer/animal record), never live
    # device GPS. Any latitude/longitude posted by the client is deliberately
    # ignored so that weather, the model inputs and outbreak clustering all use
    # one stable point per farmer.
    lat,lon,loc_source=resolve_location(c,a)

    # Pull weather for that registered point.
    weather=fetch_weather(lat,lon)
    weather["location_source"]=loc_source

    # 1) MODEL: disease + confidence only.
    prediction=call_disease_model(a,symptoms,weather,c,report_inputs)
    current_disease = prediction.get("predicted_disease")
    confidence = prediction.get("confidence")

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
    # The farmer's owning vet wins, so a case never drifts to a different vet than
    # the one holding the rest of that farmer's history.
    vet = None
    if u["assigned_vet_id"]:
        vet = c.execute("SELECT * FROM users WHERE id=? AND role='vet'", (u["assigned_vet_id"],)).fetchone()
    if not vet and lat is not None and lon is not None:
        vet = assigned_vet_by_gps(c, lat, lon)
    if not vet:
        vet = assigned_vet(c, a["village_id"])
    if vet and u["assigned_vet_id"] != vet["id"]:
        c.execute("UPDATE users SET assigned_vet_id=? WHERE id=?", (vet["id"], u["id"]))
    if vet:
        c.execute("UPDATE cases SET vet_id=?,status='OPEN' WHERE id=?",(vet["id"],case_id))
        c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (vet["id"], a["village_id"]))

    audio_url = None
    if d.get("audio_base64"):
        try:
            audio_data = base64.b64decode(d["audio_base64"].split(",")[1] if "," in d["audio_base64"] else d["audio_base64"])
            filename = f"audio_{uuid.uuid4().hex[:8]}.webm"
            audio_dir = os.path.join(BASE, "media", "audio")
            os.makedirs(audio_dir, exist_ok=True)
            filepath = os.path.join(audio_dir, filename)
            with open(filepath, "wb") as f:
                f.write(audio_data)
            audio_url = f"/media/audio/{filename}"
        except Exception as e:
            print("Error saving audio:", e)

    # 2) Save the report BEFORE the outbreak engine runs, so this case is counted
    #    in its own cluster. (Previously the engine ran first and always missed it.)
    reported_at=now()
    c.execute("""
      INSERT INTO health_reports(case_id,animal_id,symptoms_json,notes,reported_at,ai_prediction_json,ai_model_version,weather_json,report_inputs_json,audio_url)
      VALUES(?,?,?,?,?,?,?,?,?,?)
    """,(case_id,a["id"],json.dumps(symptoms),d.get("notes"),reported_at,json.dumps(prediction),prediction.get("model_version","unknown"),json.dumps(weather),json.dumps(report_inputs),audio_url))
    report_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]

    # 3) OUTBREAK LOGIC: spatial + temporal + growth + farm spread of this disease.
    outbreak_result = None
    if current_disease and current_disease != "Healthy":
        try:
            outbreak_result = run_outbreak_engine(c, current_disease, dict(a), ref_lat=lat, ref_lon=lon)
        except Exception as _obe:
            print(f"[GramVet] Outbreak engine error (non-fatal): {_obe}", flush=True)
    ob_status = outbreak_result["outbreak_status"] if outbreak_result else ("LOW" if current_disease else "UNKNOWN")
    ob_score = outbreak_result["outbreak_risk_score"] if outbreak_result else 0

    # 4) TRIAGE + ESCALATION: rules over the model output and the outbreak result.
    triage, triage_reasons = assess_triage(current_disease, confidence, symptoms, ob_status)
    escalate, escalation_reason = assess_escalation(current_disease, confidence, ob_status)

    prediction.update({
        "triage_risk": triage,
        "triage_reasons": triage_reasons,
        "outbreak_status": ob_status,
        "outbreak_risk_score": ob_score if current_disease else None,
        "outbreak_probability": ob_score / 100.0 if current_disease else None,
        "outbreak": outbreak_result,
        "escalate": escalate,
        "escalation_reason": escalation_reason,
    })
    c.execute("UPDATE health_reports SET ai_prediction_json=? WHERE id=?", (json.dumps(prediction), report_id))

    # 5) Notifications, now that the full assessment is known.
    if vet:
        disease=current_disease or "possible livestock illness"
        conftxt=f" ({confidence:.0f}%)" if isinstance(confidence,(int,float)) else ""
        create_notification(c,vet["id"],"New livestock health case",
                            f"{u['name']} reported {a['name']} ({a['species']}) in {u['village'] or 'your assigned village'}. "
                            f"Model: {disease}{conftxt}. Triage: {triage}. Review the case.",
                            "HIGH" if triage in ("High","Critical") else "INFO",
                            "CASE",case_id,a["village_id"])
    # Farmer disease advisory for this report (suspected disease x triage level).
    if current_disease and triage in TRIAGE_LEVELS:
        lvl = None if current_disease == "Healthy" else triage
        create_notification(
            c, u["id"], f"Health advisory: {current_disease}" + (f" ({lvl})" if lvl else ""),
            f"Advisory for {a['name']}: the reported symptoms are "
            + ("not a strong disease pattern." if current_disease == "Healthy" else f"consistent with {current_disease}. Contact a veterinarian for confirmation."),
            {"Critical": "CRITICAL", "High": "HIGH"}.get(triage, "INFO"), "ADVISORY", case_id, a["village_id"],
            {"adv": "farmer_disease", "disease": current_disease, "level": lvl, "animal": a["name"], "case_id": case_id})
    c.commit()

    mongo_upsert("cases", {"_id": case_id, "id": case_id, "animal_id": a["id"], "farmer_id": u["id"], "vet_id": vet["id"] if vet else None, "status": "OPEN", "synced_at": now()})
    mongo_upsert("health_reports", {
        "_id": report_id, "id": report_id, "case_id": case_id, "animal_id": a["id"],
        "symptoms": symptoms, "symptoms_json": json.dumps(symptoms), "notes": d.get("notes"),
        "audio_url": audio_url,
        "audio_data": d.get("audio_base64"),
        "reported_at": reported_at, "prediction": prediction, "ai_prediction_json": json.dumps(prediction),
        "ai_model_version": prediction.get("model_version","unknown"), "weather": weather,
        "weather_json": json.dumps(weather), "report_inputs": report_inputs,
        "report_inputs_json": json.dumps(report_inputs), "synced_at": now()
    })
    vet_info = None
    if vet:
        vet_info = {"id":vet["id"],"name":vet["name"],"phone":vet["phone"],"address":vet["address"]}
        if lat is not None and lon is not None and vet["latitude"] is not None and vet["longitude"] is not None:
            vet_info["distance_km"] = round(haversine(lat, lon, vet["latitude"], vet["longitude"]), 1)
    return jsonify(ok=True,case_id=case_id,prediction=prediction,weather=weather,
                   symptoms=symptoms,report_inputs=report_inputs, audio_url=audio_url,
                   assigned_vet=vet_info)

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
            z=dict(r);z["symptoms"]=json.loads(z.pop("symptoms_json") or "[]");z["prediction"]=json.loads(z.pop("ai_prediction_json") or "null");z["weather"]=json.loads(z.pop("weather_json") or "null") if "weather_json" in z else None;z["report_inputs"]=json.loads(z.pop("report_inputs_json") or "null") if "report_inputs_json" in z else None;d["reports"].append(z)
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
      SELECT cs.*,a.name animal_name,a.tag animal_tag,a.species,a.farmer_id,a.village_id,a.latitude animal_lat,a.longitude animal_lon,
             f.name farmer_name,f.phone farmer_phone,f.address farmer_address,f.latitude farmer_lat,f.longitude farmer_lon,
             v.name village_name,v.pincode,v.latitude village_lat,v.longitude village_lon,
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
            rd["report_inputs"]=json.loads(rd.pop("report_inputs_json") or "null") if "report_inputs_json" in rd else None
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
             v.name village_name,vet.name vet_name,vet.phone vet_phone,vet.address vet_address, a.village_id
      FROM cases cs JOIN animals a ON a.id=cs.animal_id JOIN users f ON f.id=cs.farmer_id
      JOIN villages v ON v.id=a.village_id LEFT JOIN users vet ON vet.id=cs.vet_id WHERE cs.id=?
    """,(case_id,)).fetchone()
    d=dict(rr)
    # Distance to the vet currently on the case (follows a farmer transfer).
    d["vet_distance_km"]=None
    vet_row=c.execute("SELECT latitude,longitude FROM users WHERE id=?",(cs["vet_id"],)).fetchone() if cs["vet_id"] else None
    if vet_row and vet_row["latitude"] is not None and vet_row["longitude"] is not None:
        lat,lon,_=resolve_location(c,c.execute("SELECT * FROM animals WHERE id=?",(cs["animal_id"],)).fetchone())
        if lat is not None and lon is not None:
            d["vet_distance_km"]=round(haversine(lat,lon,vet_row["latitude"],vet_row["longitude"]),1)
    d["reports"]=[]
    for x in c.execute("SELECT * FROM health_reports WHERE case_id=? ORDER BY reported_at",(case_id,)):
        z=dict(x);z["symptoms"]=json.loads(z.pop("symptoms_json"));z["prediction"]=json.loads(z.pop("ai_prediction_json") or "null")
        z["weather"]=json.loads(z.pop("weather_json") or "null") if "weather_json" in z else None
        z["report_inputs"]=json.loads(z.pop("report_inputs_json") or "null") if "report_inputs_json" in z else None
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
    if typ=="VACCINE":
        # A vaccine is only "given" through Record vaccination, which deducts vet stock atomically.
        # Logging it here would put a vaccine on the treatment timeline without touching inventory.
        return jsonify(error="Use Record vaccination to record a vaccine; it updates your vaccine stock."),400
    if typ not in ("TEST","VACCINE","MEDICATION","NOTE","CURE"):
        return jsonify(error="Action type must be TEST, VACCINE, MEDICATION, NOTE or CURE."),400
    details=(d.get("details") or "").strip()
    if not details:return jsonify(error="Please enter action details."),400
    c.execute("INSERT INTO case_actions(case_id,vet_id,action_type,details,action_at) VALUES(?,?,?,?,?)",
              (case_id,u["id"],typ,details,d.get("action_at") or now()))
    action_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Sync case action to MongoDB
    mongo_upsert("case_actions", {
        "_id": action_id,
        "id": action_id,
        "case_id": case_id,
        "vet_id": u["id"],
        "action_type": typ,
        "details": details,
        "action_at": d.get("action_at") or now()
    })
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
    d=request.json or {}; name=(d.get("vaccine_name") or "").strip().upper()
    if name not in VACCINE_CODES:
        return jsonify(error="Only FMD, HS, LSD, or BQ vaccines are permitted."), 400
    # One vaccine is applied per administration. Any dose/unit sent by a client is ignored, so the record, the
    # stock deduction and the dose totals can never disagree (and NaN/Infinity can never reach the totals).
    dose,unit=1.0,"dose"
    ts=d.get("administered_at") or now()
    if not isinstance(ts,str):return jsonify(error="Administered date/time must be text."),400

    # Administering is the ONLY thing that consumes vet stock (1 vaccine per administration).
    # Stock check + deduction + vaccination record + transaction commit together or not at all.
    begin_write(c)
    ensure_vet_stock(c,u["id"])
    new_stock=consume_vet_stock(c,u["id"],name)
    if new_stock is None:
        c.rollback()
        return jsonify(error=f"Insufficient stock for {name}. You have 0 left. Please contact government to release more."),400

    cur=c.execute("INSERT INTO vaccinations(animal_id,case_id,farmer_id,vet_id,vaccine_name,dose,unit,administered_at,batch_no,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (cs["animal_id"],case_id,cs["farmer_id"],u["id"],name,dose,unit,ts,d.get("batch_no"),d.get("notes"),now()))
    vacc_id=cur.lastrowid
    # Sync vaccination to MongoDB
    mongo_upsert("vaccinations", {
        "_id": vacc_id,
        "id": vacc_id,
        "animal_id": cs["animal_id"],
        "case_id": case_id,
        "farmer_id": cs["farmer_id"],
        "vet_id": u["id"],
        "vaccine_name": name,
        "dose": dose,
        "unit": unit,
        "administered_at": ts,
        "batch_no": d.get("batch_no"),
        "notes": d.get("notes"),
        "created_at": now()
    })
    c.execute("UPDATE animals SET last_vaccinated=? WHERE id=?",(ts[:10] if len(ts)>=10 else ts,cs["animal_id"]))
    details=f"{name} — {dose:g} {unit}" + (f" · batch {d.get('batch_no')}" if d.get('batch_no') else "") + (f" · {d.get('notes')}" if d.get('notes') else "")
    c.execute("INSERT INTO case_actions(case_id,vet_id,action_type,details,action_at) VALUES(?,?,?,?,?)",(case_id,u["id"],"VACCINE",details,ts))
    _stock_txn(c,"VET",u["id"],name,-1,new_stock,"ADMINISTERED",vaccination_id=vacc_id,case_id=case_id,animal_id=cs["animal_id"])
    check_low_stock(c,u["id"],u["name"],name,new_stock)

    create_notification(c,cs["farmer_id"],"Vaccination recorded",f"{name} ({dose:g} {unit}) was administered to the animal in case #{case_id}.","INFO","CASE",case_id)
    c.commit()
    return jsonify(ok=True,remaining_stock=new_stock)

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
    import traceback
    try:
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
            disease=case_disease(c,case_id)
            if disease and disease!="Healthy":
                # Farmers: outbreak advisory for the confirmed disease, in their own language.
                for vid in villages:
                    vn=c.execute("SELECT name FROM villages WHERE id=?",(vid,)).fetchone()
                    notify_farmer_outbreak_advisory(c,disease,vid,vn["name"] if vn else v["name"],"CRITICAL",None,True,case_id)
                # Earlier "suspected" alerts for this disease + village now show as confirmed.
                for n in c.execute("SELECT id,data_json FROM notifications WHERE kind='OUTBREAK' AND village_id=? AND data_json IS NOT NULL",(v["id"],)).fetchall():
                    dd=json.loads(n["data_json"])
                    if dd.get("adv")=="vet_outbreak" and dd.get("disease")==disease and not dd.get("confirmed"):
                        dd["confirmed"]=True; dd["confirmed_by"]=u["name"]
                        c.execute("UPDATE notifications SET data_json=? WHERE id=?",(json.dumps(dd),n["id"]))
                # Vets: the confirmed disease-control advisory is now active (confirmation gate).
                for r in c.execute("SELECT id FROM users WHERE role='vet' AND id IN (%s)"%",".join("?"*len(recipients)),list(recipients)).fetchall():
                    upsert_notification(c,r["id"],f"vet_confirmed:{disease}:{v['id']}",
                        f"CONFIRMED {disease} OUTBREAK — {v['name']}",
                        f"Veterinarian {u['name']} confirmed a {disease} outbreak in {v['name']}. Disease-specific control measures are now active.",
                        "CRITICAL","OUTBREAK",case_id,v["id"],
                        {"adv":"vet_confirmed","disease":disease,"level":"CRITICAL","village":v["name"],"confirmed":True,"confirmed_by":u["name"],"case_id":case_id})
            else:
                for uid in recipients:
                    create_notification(c,uid,
                        "CONFIRMED OUTBREAK ALERT",
                        f"Veterinarian {u['name']} confirmed an outbreak in {v['name']}. Maintain heightened vigilance. This alert covers villages within {RADIUS_KM:g} km when village coordinates are available.",
                        "CRITICAL","OUTBREAK",case_id,v["id"])
            # Government is explicitly notified only when a vet confirms.
            govs=c.execute("SELECT id FROM users WHERE role='government'").fetchall()
            for g in govs:
                create_notification(c,g["id"],"Confirmed outbreak reported",
                    f"Vet {u['name']} confirmed an outbreak in {v['name']}. Immediate government review is recommended.",
                    "CRITICAL","OUTBREAK",case_id,v["id"])
        c.commit()
        return jsonify(ok=True,confirmed=bool(confirmed),alerted_villages=villages if confirmed else [])
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)), 500

@app.get("/api/outbreak/summary")
@require_role("vet","government")
def outbreak_summary(u,c):
    """
    One entry per (village, disease), built from the outbreak engine and vet
    confirmations - never from notification text (that listed each alert
    message once per recipient, with the title shown as the disease).
      - Engine: every disease reported in a village in the last 7 days, kept if
        the current Outbreak Risk Score is MODERATE or above.
      - Vet confirmations from the last 30 days, kept whatever the engine says,
        with the engine's numbers alongside.
    A vet sees only villages in their own area; government sees all.
    """
    nowdt = datetime.fromisoformat(now())
    since7 = (nowdt - timedelta(days=7)).isoformat()
    since30 = (nowdt - timedelta(days=30)).isoformat()

    def top_disease(pj):
        try:
            p = json.loads(pj or "{}")
            if not isinstance(p, dict):
                return None
            return canonical_disease(((p.get("predictions") or [{}])[0] or {}).get("disease"))
        except Exception:
            return None

    allowed = None
    if u["role"] == "vet":
        allowed = {r[0] for r in c.execute("SELECT village_id FROM vet_villages WHERE vet_id=?", (u["id"],))}
        allowed |= {r[0] for r in c.execute(
            "SELECT DISTINCT a.village_id FROM animals a JOIN users f ON f.id=a.farmer_id WHERE f.assigned_vet_id=?", (u["id"],))}

    pairs = {}
    for vid, pj in c.execute("""SELECT a.village_id, hr.ai_prediction_json FROM health_reports hr
                                JOIN animals a ON a.id=hr.animal_id WHERE hr.reported_at>=?""", (since7,)):
        d = top_disease(pj)
        if d and d != "Healthy":
            pairs.setdefault((vid, d), {})
    for oa in c.execute("""SELECT oa.*, v.name vet_name,
                                  (SELECT hr.ai_prediction_json FROM health_reports hr WHERE hr.case_id=oa.case_id
                                   ORDER BY hr.reported_at DESC, hr.id DESC LIMIT 1) pj
                           FROM outbreak_assessments oa JOIN users v ON v.id=oa.vet_id
                           WHERE oa.confirmed=1 AND oa.created_at>=? ORDER BY oa.created_at DESC""", (since30,)):
        e = pairs.setdefault((oa["village_id"], top_disease(oa["pj"]) or "Unknown"), {})
        if "confirmed_by" not in e:   # newest confirmation wins
            e.update(confirmed_by=oa["vet_name"], confirmed_at=oa["created_at"], confirm_notes=oa["notes"],
                     case_id=oa["case_id"])

    results = []
    for (vid, disease), e in pairs.items():
        if vid is None or (allowed is not None and vid not in allowed):
            continue
        v = c.execute("SELECT * FROM villages WHERE id=?", (vid,)).fetchone()
        if not v:
            continue
        r = calculate_outbreak_risk(c, disease, v["latitude"], v["longitude"], v["name"]) if disease != "Unknown" else None
        confirmed = "confirmed_by" in e
        if not confirmed and (r is None or r["outbreak_status"] == "LOW"):
            continue
        results.append({
            "village_id": vid, "village": v["name"], "disease": disease,
            "confirmed": confirmed, "confirmed_by": e.get("confirmed_by"),
            "confirmed_at": e.get("confirmed_at"), "confirm_notes": e.get("confirm_notes"), "case_id": e.get("case_id"),
            "outbreak_status": r["outbreak_status"] if r else None,
            "outbreak_risk_score": r["outbreak_risk_score"] if r else None,
            "unique_animals": r["unique_animals"] if r else None,
            "unique_farms": r["unique_farms"] if r else None,
            "unique_villages": r["unique_villages"] if r else None,
            "villages": r["villages"] if r else [],
            "evidence": r["evidence"] if r else [],
        })
    results.sort(key=lambda x: (not x["confirmed"], -(x["outbreak_risk_score"] or 0)))
    return jsonify(outbreaks=results)

@app.get("/api/notifications")
@require_role("farmer","vet","government")
def notifications(u,c):

    rows=c.execute("""
      SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 100
    """,(u["id"],)).fetchall()
    unread=sum(1 for r in rows if not r["read_at"])
    out=[]
    for r in rows:
        n=dict(r)
        try: n["data"]=json.loads(n.pop("data_json",None) or "null")
        except Exception: n["data"]=None
        out.append(n)
    return jsonify(unread=unread,notifications=out)

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
    vet_lat = u["latitude"] if "latitude" in u.keys() else None
    vet_lon = u["longitude"] if "longitude" in u.keys() else None
    
    # Get all villages; filter to 5km of vet if GPS available
    all_villages = c.execute("SELECT * FROM villages ORDER BY name").fetchall()
    villages_out = []
    for v in all_villages:
        include = False
        if vet_lat is not None and vet_lon is not None and v["latitude"] is not None and v["longitude"] is not None:
            if haversine(vet_lat, vet_lon, v["latitude"], v["longitude"]) <= RADIUS_KM:
                include = True
        else:
            # Fallback: use vet_villages mapping
            in_vv = c.execute("SELECT 1 FROM vet_villages WHERE vet_id=? AND village_id=?", (u["id"], v["id"])).fetchone()
            if in_vv:
                include = True
        if not include:
            continue
        # Ensure vet_villages mapping is up to date
        c.execute("INSERT OR IGNORE INTO vet_villages(vet_id,village_id) VALUES(?,?)", (u["id"], v["id"]))
        animals_count = c.execute("SELECT COUNT(DISTINCT a.id) FROM animals a WHERE a.village_id=?", (v["id"],)).fetchone()[0]
        farmers_count = c.execute("SELECT COUNT(DISTINCT a.farmer_id) FROM animals a WHERE a.village_id=?", (v["id"],)).fetchone()[0]
        active_cases_count = c.execute("""
            SELECT COUNT(DISTINCT cs.id) FROM cases cs 
            JOIN animals a ON a.id=cs.animal_id 
            WHERE a.village_id=? AND cs.status!='CLOSED'
        """, (v["id"],)).fetchone()[0]
        # Only show villages that actually have registered livestock
        if animals_count == 0:
            continue
        villages_out.append({"id": v["id"], "name": v["name"], "pincode": v["pincode"],
                              "animals": animals_count, "farmers": farmers_count,
                              "active_cases": active_cases_count})

    # Owner-searchable animal list — DISTINCT animals in vet's range villages
    village_ids = [v["id"] for v in villages_out]
    animals = []
    if village_ids:
        placeholders = ",".join("?" * len(village_ids))
        for a in c.execute(f"""
            SELECT DISTINCT a.id, a.name, a.tag, a.species, a.village_id,
                   u.name farmer_name, u.phone farmer_phone, v.name village_name
            FROM animals a
            JOIN users u ON u.id=a.farmer_id
            JOIN villages v ON v.id=a.village_id
            WHERE a.village_id IN ({placeholders}) AND a.species IN ('Cattle','Buffalo')
            ORDER BY v.name, a.name
        """, village_ids).fetchall():
            animals.append({"id": a["id"], "name": a["name"], "tag": a["tag"],
                            "species": a["species"], "farmer_name": a["farmer_name"],
                            "farmer_phone": a["farmer_phone"], "village": a["village_name"]})

    one_year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    total_animals_under = len(animals)
    vacc_animals = 0
    if village_ids:
        vacc_animals = c.execute(f"""
            SELECT COUNT(DISTINCT vac.animal_id) FROM vaccinations vac
            JOIN animals a ON a.id=vac.animal_id
            WHERE a.village_id IN ({placeholders}) AND vac.administered_at >= ?
        """, village_ids + [one_year_ago]).fetchone()[0]
    vaccination_pct = round(vacc_animals / total_animals_under * 100, 1) if total_animals_under else 0

    resources_left, resources_sent = vet_vaccine_totals(c, u["id"])

    reqs = []
    for r in c.execute("SELECT rr.*, v.name village_name FROM resource_requests rr JOIN villages v ON v.id=rr.village_id WHERE rr.vet_id=? ORDER BY rr.created_at DESC", (u["id"],)).fetchall():
        rd = dict(r)
        rd["resources"] = json.loads(rd.pop("resource_json") or "[]")
        reqs.append(rd)

    c.commit()
    return jsonify(villages=villages_out, animals=animals, requests=reqs,
                   vaccination_pct=vaccination_pct, resources_sent=resources_sent, resources_left=resources_left)

@app.get("/api/govt/farmers")
@require_role("government")
def govt_farmers(u,c):
    villages_out = []
    for v in c.execute("SELECT * FROM villages ORDER BY name").fetchall():
        animals_count = c.execute("SELECT COUNT(DISTINCT a.id) FROM animals a WHERE a.village_id=?", (v["id"],)).fetchone()[0]
        farmers_count = c.execute("SELECT COUNT(DISTINCT a.farmer_id) FROM animals a WHERE a.village_id=?", (v["id"],)).fetchone()[0]
        active_cases_count = c.execute("""
            SELECT COUNT(DISTINCT cs.id) FROM cases cs 
            JOIN animals a ON a.id=cs.animal_id 
            WHERE a.village_id=? AND cs.status!='CLOSED'
        """, (v["id"],)).fetchone()[0]
        if animals_count > 0:
            villages_out.append({"id": v["id"], "name": v["name"], "pincode": v["pincode"],
                                 "animals": animals_count, "farmers": farmers_count,
                                 "active_cases": active_cases_count})
    animals = []
    for a in c.execute("""
        SELECT DISTINCT a.id, a.name, a.tag, a.species, a.village_id,
               u.name farmer_name, u.phone farmer_phone, v.name village_name
        FROM animals a
        JOIN users u ON u.id=a.farmer_id
        JOIN villages v ON v.id=a.village_id
        WHERE a.species IN ('Cattle','Buffalo')
        ORDER BY v.name, a.name
    """).fetchall():
        animals.append({"id": a["id"], "name": a["name"], "tag": a["tag"],
                        "species": a["species"], "farmer_name": a["farmer_name"],
                        "farmer_phone": a["farmer_phone"], "village": a["village_name"]})
    return jsonify(villages=villages_out, animals=animals)

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
    one_year_ago_global = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=365)).isoformat()
    vaccinated=c.execute("SELECT COUNT(DISTINCT animal_id) FROM vaccinations WHERE administered_at >= ?", (one_year_ago_global,)).fetchone()[0]
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
            if not isinstance(p,dict): p={}   # a report stored as JSON "null"
            op=p.get("outbreak_probability")
            if isinstance(op,(int,float)): outbreak_probs.append(float(op))
            for pred in (p.get("predictions") or []):
                if isinstance(pred,dict):
                    if isinstance(pred.get("probability"),(int,float)): risk_probs.append(float(pred["probability"]))
                    if pred.get("disease"): diseases[pred["disease"]]=diseases.get(pred["disease"],0)+1
        maxp=max(risk_probs) if risk_probs else None
        max_outbreak=max(outbreak_probs) if outbreak_probs else None
        confirmed=bool(c.execute("SELECT 1 FROM outbreak_assessments WHERE village_id=? AND confirmed=1 LIMIT 1",(v["id"],)).fetchone())
        
        top_disease = max(diseases,key=diseases.get) if diseases else None
        
        treated_count = 0
        if top_disease:
            closed_cases_village = c.execute("""
              SELECT cs.id, hr.ai_prediction_json FROM cases cs
              JOIN animals a ON a.id=cs.animal_id
              LEFT JOIN health_reports hr ON hr.id=(SELECT id FROM health_reports WHERE case_id=cs.id ORDER BY reported_at DESC,id DESC LIMIT 1)
              WHERE a.village_id=? AND cs.status='CLOSED'
            """,(v["id"],)).fetchall()
            for x in closed_cases_village:
                p = json.loads(x["ai_prediction_json"] or "{}") if x["ai_prediction_json"] else {}
                for pred in (p.get("predictions") or []):
                    if isinstance(pred,dict) and pred.get("disease") == top_disease:
                        treated_count += 1
                        break
        else:
            treated_count = c.execute("SELECT COUNT(*) FROM cases cs JOIN animals a ON a.id=cs.animal_id WHERE a.village_id=? AND cs.status='CLOSED'", (v["id"],)).fetchone()[0]

        outbreak_level = "CRITICAL" if (confirmed or (max_outbreak is not None and max_outbreak >= 0.85)) else \
                         "HIGH" if (max_outbreak is not None and max_outbreak >= 0.6) or (len(active) >= 6) else \
                         "MODERATE" if (max_outbreak is not None and max_outbreak >= 0.4) or (3 <= len(active) <= 5) else \
                         None
        if outbreak_level:
            village_rows.append({
                "id":v["id"],"name":v["name"],"pincode":v["pincode"],
                "latitude":v["latitude"],"longitude":v["longitude"],
                "lat":v["latitude"],"lon":v["longitude"],
                "animals":c.execute("SELECT COUNT(*) FROM animals WHERE village_id=?",(v["id"],)).fetchone()[0],
                "farmers":c.execute("SELECT COUNT(DISTINCT farmer_id) FROM animals WHERE village_id=?",(v["id"],)).fetchone()[0],
                "active_cases":len(active),
                "affected_count":len(active),
                "treated_count":treated_count,
                "max_model_probability":maxp,
                "max_outbreak_probability":max_outbreak,"confirmed_outbreak":confirmed,
                "top_disease":top_disease,
                "outbreak_level":outbreak_level
            })
    # Simple time-series for the dashboard.
    trend=[]
    for day in range(13,-1,-1):
        # ISO date in IST; use SQLite date comparison.
        d=(datetime.now(timezone(timedelta(hours=5, minutes=30))).date()).toordinal()-day
        date=str(datetime.fromordinal(d).date())
        cnt=c.execute("SELECT COUNT(*) FROM cases WHERE date(opened_at)=?",(date,)).fetchone()[0]
        trend.append({"date":date,"cases":cnt})
    vets=[]
    
    # Calculate overall resources dispatched to all vets all time & monthly
    total_dispatched_all_time = 0
    total_dispatched_monthly = 0
    first_day_of_month = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(day=1, hour=0, minute=0, second=0).isoformat()
    
    for v in c.execute("SELECT id,name,phone,address FROM users WHERE role='vet' ORDER BY name"):
        handled=c.execute("SELECT COUNT(*) FROM cases WHERE vet_id=?",(v["id"],)).fetchone()[0]
        closed=c.execute("SELECT COUNT(*) FROM cases WHERE vet_id=? AND status='CLOSED'",(v["id"],)).fetchone()[0]
        
        # New stats
        animals_under = c.execute("SELECT COUNT(a.id) FROM animals a JOIN vet_villages vv ON a.village_id=vv.village_id WHERE vv.vet_id=?", (v["id"],)).fetchone()[0]
        farmers_under = c.execute("SELECT COUNT(DISTINCT a.farmer_id) FROM animals a JOIN vet_villages vv ON a.village_id=vv.village_id WHERE vv.vet_id=?", (v["id"],)).fetchone()[0]
        
        one_year_ago = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=365)).isoformat()
        vacc_animals = c.execute("SELECT COUNT(DISTINCT a.id) FROM animals a JOIN vet_villages vv ON a.village_id=vv.village_id JOIN vaccinations vac ON vac.animal_id=a.id WHERE vv.vet_id=? AND vac.administered_at >= ?", (v["id"], one_year_ago)).fetchone()[0]
        vaccination_pct = round(vacc_animals / animals_under * 100, 1) if animals_under else 0
        
        resources_left, resources_sent = vet_vaccine_totals(c, v["id"])
        
        total_dispatched_all_time += resources_sent
        if resources_left <= 0:
            # check if we already notified them recently to avoid spam, but since we are computing this on read, 
            # we should notify them. But maybe only when vaccination happens! Yes, handled below.
            pass

        # For global stats
        monthly_dispatched = c.execute("SELECT COALESCE(SUM(quantity_change),0) FROM inventory_transactions WHERE vet_id=? AND reason='Govt Dispatch' AND created_at >= ?", (v["id"], first_day_of_month)).fetchone()[0]
        total_dispatched_monthly += monthly_dispatched
        
        vets.append({"id":v["id"],"name":v["name"],"phone":v["phone"],"address":v["address"],
                     "handled":handled,"closed":closed, "animals_under":animals_under, 
                     "farmers_under":farmers_under, "vaccination_pct":vaccination_pct, 
                     "resources_left":resources_left, "resources_sent":resources_sent})
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
                "vaccination_events":vaccination_count,"active_cases":active_cases,"closed_cases":closed_cases,"confirmed_outbreaks":confirmed_outbreaks,
                "total_dispatched_all_time": total_dispatched_all_time, "total_dispatched_monthly": total_dispatched_monthly},
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
    if not name or not _is_stock_qty(qty):return jsonify(error="Provide resource name and non-negative quantity."),400
    is_vaccine = name in VACCINE_CODES
    if is_vaccine and (qty != int(qty) or qty > 10**9):
        return jsonify(error="Vaccine stock must be a whole number of doses."),400
    if is_vaccine:
        begin_write(c)   # read the old quantity and write the new one as one step so the ledger stays exact
        old=c.execute("SELECT available_qty FROM resource_inventory WHERE resource_name=?",(name,)).fetchone()
        old_qty=int(old["available_qty"]) if old else 0
    c.execute("""
      INSERT INTO resource_inventory(resource_name,available_qty,unit,updated_at)
      VALUES(?,?,?,?)
      ON CONFLICT(resource_name) DO UPDATE SET available_qty=excluded.available_qty,unit=excluded.unit,updated_at=excluded.updated_at
    """,(name,float(qty),unit,now()))
    if is_vaccine and int(qty)!=old_qty:   # a manual correction is a stock movement too: record it
        _stock_txn(c,"GOVT",None,name,int(qty)-old_qty,int(qty),"MANUAL_ADJUST")
    c.commit();return jsonify(ok=True)

@app.post("/api/govt/resource-request/<int:request_id>")
@require_role("government")
def govt_review_resource(u,c,request_id):
    rr=c.execute("SELECT * FROM resource_requests WHERE id=?",(request_id,)).fetchone()
    if not rr:return jsonify(error="Request not found"),404
    d=request.json or {};status=d.get("status")
    if status not in ("DISPATCHED","REJECTED","DELIVERED"):return jsonify(error="Invalid status."),400
    
    if status == "DELIVERED" and rr["status"] != "DISPATCHED":
        return jsonify(error="Can only deliver a dispatched request."),400
        
    c.execute("UPDATE resource_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?",(status,now(),u["id"],request_id))
    
    if status == "DELIVERED":
        # Only add to inventory when actually delivered
        resources = json.loads(rr["resource_json"] or "[]")
        for res in resources:
            qty = float(res.get("quantity",0))
            if qty > 0:
                c.execute("""
                  INSERT INTO inventory_transactions(resource_name, quantity_change, unit, reason, vet_id, created_at)
                  VALUES(?, ?, ?, ?, ?, ?)
                """, (res.get("name","Vaccine"), qty, res.get("unit","units"), "Govt Dispatch", rr["vet_id"], now()))

    create_notification(c,rr["vet_id"],f"Resource request {status.lower()}",
        f"Government marked resource request #{request_id} as {status.lower()}.","INFO","RESOURCE",None,rr["village_id"])
    c.commit();return jsonify(ok=True)

@app.post("/api/vet/resource-request/<int:request_id>/deliver")
@require_role("vet")
def vet_deliver_resource(u,c,request_id):
    rr=c.execute("SELECT * FROM resource_requests WHERE id=? AND vet_id=?",(request_id, u["id"])).fetchone()
    if not rr:return jsonify(error="Request not found"),404
    if rr["status"] != "DISPATCHED": return jsonify(error="Can only deliver a dispatched request."),400
    
    c.execute("UPDATE resource_requests SET status=?,reviewed_at=? WHERE id=?",("DELIVERED",now(),request_id))
    
    resources = json.loads(rr["resource_json"] or "[]")
    for res in resources:
        qty = float(res.get("quantity",0))
        if qty > 0:
            c.execute("""
              INSERT INTO inventory_transactions(resource_name, quantity_change, unit, reason, vet_id, created_at)
              VALUES(?, ?, ?, ?, ?, ?)
            """, (res.get("name","Vaccine"), qty, res.get("unit","units"), "Govt Dispatch", rr["vet_id"], now()))

    create_notification(c,u["id"],"Resource Delivered",
        f"You marked resource request #{request_id} as delivered. Stock has been updated.","INFO","RESOURCE",None,rr["village_id"])
    c.commit();return jsonify(ok=True)

# ---------------------------------------------------------------------------
# Vaccine inventory (vet stock, government stock, shipments, low-stock alerts)
# ---------------------------------------------------------------------------
# Every mutation below runs inside ONE write transaction (BEGIN IMMEDIATE) and
# is guarded by a conditional UPDATE (..WHERE quantity>=? / status='IN_TRANSIT'),
# so concurrent or repeated requests can never drive stock negative, release
# stock the government does not have, or credit a shipment twice.

def begin_write(c):
    """Take SQLite's write lock up front so read-then-write sequences cannot interleave."""
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")

def _abort(c, message, code):
    c.rollback()
    return jsonify(error=message), code

def _as_positive_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, float) and v.is_integer():
        n = int(v)
    elif isinstance(v, str) and v.strip().isdigit():
        n = int(v.strip())
    else:
        return None
    return n if 0 < n <= 10**9 else None

def _is_stock_qty(v):
    """A real, finite, non-negative number. bool, NaN, Infinity, huge ints and text are not quantities."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v)) and v >= 0
    except OverflowError:
        return False

def _stock_txn(c, party, vet_id, vaccine, change, balance_after, txn_type,
               shipment_id=None, vaccination_id=None, case_id=None, animal_id=None):
    c.execute("""
      INSERT INTO vaccine_stock_transactions(party,vet_id,vaccine,quantity_change,balance_after,txn_type,
                                             shipment_id,vaccination_id,case_id,animal_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (party, vet_id, vaccine, change, balance_after, txn_type, shipment_id, vaccination_id, case_id, animal_id, now()))

def _initial_vet_stock(c, vet_id, vaccine):
    # A vet starts with VET_START_STOCK. A vet that already recorded vaccinations before
    # this feature existed starts with what would be left of it.
    used = c.execute("SELECT COUNT(*) FROM vaccinations WHERE vet_id=? AND upper(vaccine_name)=?",
                     (vet_id, vaccine)).fetchone()[0]
    return max(0, VET_START_STOCK - used)

def ensure_vet_stock(c, vet_id):
    """Create the vet's four stock rows if (and only if) they do not exist yet.
    Existing rows are never touched, so a restart/refresh can never reset stock to 25."""
    begin_write(c)
    for vaccine in VACCINE_CODES:
        if c.execute("SELECT 1 FROM vet_vaccine_stock WHERE vet_id=? AND vaccine=?", (vet_id, vaccine)).fetchone():
            continue
        start = _initial_vet_stock(c, vet_id, vaccine)
        cur = c.execute("INSERT OR IGNORE INTO vet_vaccine_stock(vet_id,vaccine,quantity,updated_at) VALUES(?,?,?,?)",
                        (vet_id, vaccine, start, now()))
        if cur.rowcount == 1:
            _stock_txn(c, "VET", vet_id, vaccine, start, start, "INITIAL")
            if start < LOW_STOCK_THRESHOLD:   # already low from the start (e.g. long vaccination history): don't let it go unnoticed
                who = c.execute("SELECT name FROM users WHERE id=?", (vet_id,)).fetchone()
                check_low_stock(c, vet_id, who["name"] if who else "", vaccine, start)

def ensure_govt_stock(c):
    """Seed the central inventory rows once; existing quantities are never overwritten."""
    begin_write(c)
    for vaccine in VACCINE_CODES:
        cur = c.execute("INSERT OR IGNORE INTO resource_inventory(resource_name,available_qty,unit,updated_at) VALUES(?,?,?,?)",
                        (vaccine, GOVT_START_STOCK, "dose", now()))
        if cur.rowcount == 1:
            _stock_txn(c, "GOVT", None, vaccine, GOVT_START_STOCK, GOVT_START_STOCK, "INITIAL")

def vet_vaccine_stock_view(c, vet_id):
    """Read-only {vaccine: quantity}; never writes (safe for GET handlers)."""
    have = {r["vaccine"]: int(r["quantity"]) for r in
            c.execute("SELECT vaccine,quantity FROM vet_vaccine_stock WHERE vet_id=?", (vet_id,))}
    return {v: have[v] if v in have else _initial_vet_stock(c, vet_id, v) for v in VACCINE_CODES}

def vet_vaccine_totals(c, vet_id):
    """(doses left, doses supplied) across all four vaccines, for the existing dashboard cards."""
    left = sum(vet_vaccine_stock_view(c, vet_id).values())
    received = c.execute("SELECT COALESCE(SUM(quantity),0) FROM vaccine_shipments WHERE vet_id=? AND status='RECEIVED'",
                         (vet_id,)).fetchone()[0]
    return float(left), float(VET_START_STOCK * len(VACCINE_CODES) + received)

def consume_vet_stock(c, vet_id, vaccine):
    """Atomically take 1 vaccine from the vet's stock. Returns the new balance, or None if empty."""
    cur = c.execute("UPDATE vet_vaccine_stock SET quantity=quantity-1, updated_at=? "
                    "WHERE vet_id=? AND vaccine=? AND quantity>=1", (now(), vet_id, vaccine))
    if cur.rowcount != 1:
        return None
    return c.execute("SELECT quantity FROM vet_vaccine_stock WHERE vet_id=? AND vaccine=?",
                     (vet_id, vaccine)).fetchone()[0]

def check_low_stock(c, vet_id, vet_name, vaccine, quantity):
    """Raise ONE government alert when a vet's stock for a vaccine is below the threshold.
    The partial unique index on (vet_id, vaccine) WHERE status='OPEN' makes duplicates impossible."""
    if quantity >= LOW_STOCK_THRESHOLD:
        return False
    cur = c.execute("INSERT OR IGNORE INTO vaccine_alerts(vet_id,vaccine,stock_at_alert,status,created_at) VALUES(?,?,?,?,?)",
                    (vet_id, vaccine, quantity, "OPEN", now()))
    if cur.rowcount != 1:
        return False  # an unresolved alert for this vet + vaccine already exists
    data = {"vet_id": vet_id, "vaccine": vaccine, "stock": quantity}
    for g in c.execute("SELECT id FROM users WHERE role='government'").fetchall():
        create_notification(c, g["id"], f"Low vaccine stock: {vaccine}",
            f"Dr. {vet_name} has only {quantity} {vaccine} vaccine(s) left (critical threshold: below {LOW_STOCK_THRESHOLD}).",
            "CRITICAL", "LOW_STOCK", None, None, data)
    create_notification(c, vet_id, f"Low Stock Alert: {vaccine}",
        f"Your {vaccine} stock has fallen to {quantity}. Government has been notified.",
        "CRITICAL", "LOW_STOCK", None, None, data)
    return True

def _shipment_dict(r):
    return {k: r[k] for k in r.keys()}

_SHIPMENT_SELECT = """
  SELECT s.id,s.vet_id,vu.name vet_name,s.vaccine,s.quantity,s.status,
         s.released_by,gu.name released_by_name,s.released_at,s.received_at
  FROM vaccine_shipments s
  JOIN users vu ON vu.id=s.vet_id
  LEFT JOIN users gu ON gu.id=s.released_by
"""

@app.get("/api/vet/vaccine-inventory")
@require_role("vet")
def vet_vaccine_inventory(u,c):
    stock = vet_vaccine_stock_view(c, u["id"])
    shipments = [_shipment_dict(r) for r in c.execute(
        _SHIPMENT_SELECT + " WHERE s.vet_id=? ORDER BY s.id DESC", (u["id"],))]
    return jsonify(threshold=LOW_STOCK_THRESHOLD,
                   stock=[{"vaccine": v, "quantity": stock[v], "low": stock[v] < LOW_STOCK_THRESHOLD} for v in VACCINE_CODES],
                   shipments=shipments)

@app.get("/api/govt/vaccine-inventory")
@require_role("government")
def govt_vaccine_inventory(u,c):
    have = {r["resource_name"]: r["available_qty"] for r in c.execute("SELECT resource_name,available_qty FROM resource_inventory")}
    central = [{"vaccine": v, "quantity": int(have[v]) if v in have else GOVT_START_STOCK} for v in VACCINE_CODES]
    vets = []
    for v in c.execute("SELECT id,name FROM users WHERE role='vet' ORDER BY name").fetchall():
        st = vet_vaccine_stock_view(c, v["id"])
        vets.append({"vet_id": v["id"], "name": v["name"], "stock": st,
                     "low": [k for k in VACCINE_CODES if st[k] < LOW_STOCK_THRESHOLD]})
    alerts = [dict(r) for r in c.execute("""
        SELECT a.id,a.vet_id,u.name vet_name,a.vaccine,a.stock_at_alert,a.created_at,
               COALESCE(s.quantity,0) current_stock
        FROM vaccine_alerts a JOIN users u ON u.id=a.vet_id
        LEFT JOIN vet_vaccine_stock s ON s.vet_id=a.vet_id AND s.vaccine=a.vaccine
        WHERE a.status='OPEN' ORDER BY a.created_at DESC,a.id DESC""")]
    return jsonify(threshold=LOW_STOCK_THRESHOLD, central=central, vets=vets, open_alerts=alerts)

@app.get("/api/govt/vaccine-shipments")
@require_role("government")
def govt_vaccine_shipments(u,c):
    where, params = [], []
    status = (request.args.get("status") or "").upper()
    if status:
        if status not in ("IN_TRANSIT", "RECEIVED"):
            return jsonify(error="Status must be IN_TRANSIT or RECEIVED."), 400
        where.append("s.status=?"); params.append(status)
    if request.args.get("vet_id"):
        try: where.append("s.vet_id=?"); params.append(int(request.args["vet_id"]))
        except ValueError: return jsonify(error="vet_id must be a number."), 400
    sql = _SHIPMENT_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.id DESC"
    return jsonify(shipments=[_shipment_dict(r) for r in c.execute(sql, params)])

@app.post("/api/govt/vaccine-shipments")
@require_role("government")
def govt_release_vaccine(u,c):
    d = request.json if isinstance(request.json, dict) else {}
    vaccine = str(d.get("vaccine") or "").strip().upper()
    if vaccine not in VACCINE_CODES:
        return jsonify(error="Vaccine must be one of FMD, HS, LSD or BQ."), 400
    qty = _as_positive_int(d.get("quantity"))
    if qty is None:
        return jsonify(error="Quantity must be a positive whole number."), 400
    vet_id = _as_positive_int(d.get("vet_id"))
    if vet_id is None: return jsonify(error="Choose a veterinarian."), 400
    key = str(d.get("idempotency_key") or "").strip()[:100] or None

    begin_write(c)
    if key:  # a repeated submit of the same release returns the original shipment instead of releasing twice
        ex = c.execute(_SHIPMENT_SELECT + " WHERE s.released_by=? AND s.idempotency_key=?", (u["id"], key)).fetchone()
        if ex:
            c.rollback()
            if (ex["vet_id"], ex["vaccine"], ex["quantity"]) != (vet_id, vaccine, qty):
                return jsonify(error="This idempotency_key was already used for a different release."), 409
            return jsonify(ok=True, duplicate=True, shipment=_shipment_dict(ex))
    vet = c.execute("SELECT id,name FROM users WHERE id=? AND role='vet'", (vet_id,)).fetchone()
    if not vet:
        return _abort(c, "Veterinarian not found.", 404)
    ensure_govt_stock(c)
    cur = c.execute("UPDATE resource_inventory SET available_qty=available_qty-?, updated_at=? "
                    "WHERE resource_name=? AND available_qty>=?", (qty, now(), vaccine, qty))
    if cur.rowcount != 1:
        have = c.execute("SELECT available_qty FROM resource_inventory WHERE resource_name=?", (vaccine,)).fetchone()[0]
        return _abort(c, f"Insufficient government stock for {vaccine}: {int(have)} available, {qty} requested.", 409)
    balance = int(c.execute("SELECT available_qty FROM resource_inventory WHERE resource_name=?", (vaccine,)).fetchone()[0])
    c.execute("""INSERT INTO vaccine_shipments(vet_id,vaccine,quantity,status,released_by,released_at,idempotency_key)
                 VALUES(?,?,?,'IN_TRANSIT',?,?,?)""", (vet_id, vaccine, qty, u["id"], now(), key))
    sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    _stock_txn(c, "GOVT", vet_id, vaccine, -qty, balance, "SHIPMENT_RELEASED", shipment_id=sid)
    create_notification(c, vet_id, "Vaccine shipment in transit",
        f"Government released {qty} {vaccine} vaccine(s) to you (shipment #{sid}). "
        "Confirm 'Vaccine Received' when they arrive; your stock updates only then.",
        "INFO", "SHIPMENT", None, None, {"shipment_id": sid, "vaccine": vaccine, "quantity": qty})
    c.commit()
    ship = c.execute(_SHIPMENT_SELECT + " WHERE s.id=?", (sid,)).fetchone()
    return jsonify(ok=True, shipment=_shipment_dict(ship), government_stock_remaining=balance), 201

@app.post("/api/vet/vaccine-shipments/<int:shipment_id>/receive")
@require_role("vet")
def vet_receive_vaccine(u,c,shipment_id):
    begin_write(c)
    ship = c.execute("SELECT * FROM vaccine_shipments WHERE id=? AND vet_id=?", (shipment_id, u["id"])).fetchone()
    if not ship:  # unknown id and someone else's shipment look identical on purpose
        return _abort(c, "Shipment not found.", 404)
    ensure_vet_stock(c, u["id"])
    # The status flip is the gate: only the request that changes IN_TRANSIT -> RECEIVED may credit stock.
    cur = c.execute("UPDATE vaccine_shipments SET status='RECEIVED', received_at=?, received_by=? "
                    "WHERE id=? AND vet_id=? AND status='IN_TRANSIT'", (now(), u["id"], shipment_id, u["id"]))
    if cur.rowcount != 1:
        return _abort(c, "This shipment has already been received.", 409)
    c.execute("UPDATE vet_vaccine_stock SET quantity=quantity+?, updated_at=? WHERE vet_id=? AND vaccine=?",
              (ship["quantity"], now(), u["id"], ship["vaccine"]))
    balance = c.execute("SELECT quantity FROM vet_vaccine_stock WHERE vet_id=? AND vaccine=?",
                        (u["id"], ship["vaccine"])).fetchone()[0]
    _stock_txn(c, "VET", u["id"], ship["vaccine"], ship["quantity"], balance, "SHIPMENT_RECEIVED", shipment_id=shipment_id)
    if balance >= LOW_STOCK_THRESHOLD:
        c.execute("UPDATE vaccine_alerts SET status='RESOLVED', resolved_at=?, resolved_by_shipment_id=? "
                  "WHERE vet_id=? AND vaccine=? AND status='OPEN'", (now(), shipment_id, u["id"], ship["vaccine"]))
    for g in c.execute("SELECT id FROM users WHERE role='government'").fetchall():
        create_notification(c, g["id"], "Vaccine shipment delivered",
            f"Dr. {u['name']} confirmed receipt of {ship['quantity']} {ship['vaccine']} vaccine(s) (shipment #{shipment_id}).",
            "INFO", "SHIPMENT", None, None, {"shipment_id": shipment_id, "vaccine": ship["vaccine"], "quantity": ship["quantity"]})
    c.commit()
    return jsonify(ok=True, shipment_id=shipment_id, status="RECEIVED", vaccine=ship["vaccine"],
                   quantity=ship["quantity"], new_stock=balance)

@app.get("/api/vet/outbreak-map")
@require_role("vet")
def vet_outbreak_map(u,c):
    """Everything in this vet's area, outbreak or not: the vet's own point, every
    village they cover, every farmer assigned to them, and each village's current
    Outbreak Risk Score from the same engine that sends the alerts.
    Positions are only ever real stored coordinates (farmer -> their animal ->
    their village); anything without one is returned with lat/lon = None."""
    since = (datetime.fromisoformat(now()) - timedelta(days=7)).isoformat()

    farmer_rows=[]
    for f in c.execute("""
        SELECT f.id,f.name,f.phone,f.village,
               COALESCE(f.latitude,(SELECT a.latitude FROM animals a WHERE a.farmer_id=f.id AND a.latitude IS NOT NULL LIMIT 1),
                        (SELECT v.latitude FROM animals a JOIN villages v ON v.id=a.village_id WHERE a.farmer_id=f.id AND v.latitude IS NOT NULL LIMIT 1)) lat,
               COALESCE(f.longitude,(SELECT a.longitude FROM animals a WHERE a.farmer_id=f.id AND a.longitude IS NOT NULL LIMIT 1),
                        (SELECT v.longitude FROM animals a JOIN villages v ON v.id=a.village_id WHERE a.farmer_id=f.id AND v.longitude IS NOT NULL LIMIT 1)) lon
        FROM users f WHERE f.role='farmer' AND f.assigned_vet_id=? ORDER BY f.name
    """,(u["id"],)).fetchall():
        farmer_rows.append({
            "id":f["id"],"name":f["name"],"phone":f["phone"],"village":f["village"],
            "lat":f["lat"],"lon":f["lon"],
            "animals":c.execute("SELECT COUNT(*) FROM animals WHERE farmer_id=?",(f["id"],)).fetchone()[0],
            "open_cases":c.execute("SELECT COUNT(*) FROM cases WHERE farmer_id=? AND status!='CLOSED'",(f["id"],)).fetchone()[0],
        })
    farmer_ids=[f["id"] for f in farmer_rows]
    q=",".join("?"*len(farmer_ids)) or "NULL"

    # Villages this vet covers, plus any village where one of their farmers keeps animals.
    village_ids={r[0] for r in c.execute("SELECT village_id FROM vet_villages WHERE vet_id=?",(u["id"],))}
    village_ids|={r[0] for r in c.execute(f"SELECT DISTINCT village_id FROM animals WHERE farmer_id IN ({q})",farmer_ids)}

    village_rows=[]
    for vid in sorted(v for v in village_ids if v is not None):
        v=c.execute("SELECT * FROM villages WHERE id=?",(vid,)).fetchone()
        if not v:
            continue
        lat,lon=v["latitude"],v["longitude"]
        if lat is None or lon is None:
            # Village never geocoded: use the centre of its farmers' real positions.
            pts=[(f["lat"],f["lon"]) for f in farmer_rows if f["lat"] is not None and
                 c.execute("SELECT 1 FROM animals WHERE farmer_id=? AND village_id=? LIMIT 1",(f["id"],vid)).fetchone()]
            if pts:
                lat=sum(p[0] for p in pts)/len(pts); lon=sum(p[1] for p in pts)/len(pts)

        # Current outbreak status: run the engine for each disease reported here this week.
        diseases=set()
        for (pj,) in c.execute("""
            SELECT hr.ai_prediction_json FROM health_reports hr JOIN animals a ON a.id=hr.animal_id
            WHERE a.village_id=? AND hr.reported_at>=?""",(vid,since)):
            try:
                pred=json.loads(pj or "{}")
                d=canonical_disease(((pred.get("predictions") or [{}])[0] or {}).get("disease")) if isinstance(pred,dict) else None
            except Exception:
                d=None
            if d and d!="Healthy":
                diseases.add(d)
        best=None
        for d in sorted(diseases):
            r=calculate_outbreak_risk(c,d,lat,lon,v["name"])
            if best is None or r["outbreak_risk_score"]>best["outbreak_risk_score"]:
                best=r
        confirmed=bool(c.execute("SELECT 1 FROM outbreak_assessments WHERE village_id=? AND confirmed=1 LIMIT 1",(vid,)).fetchone())
        village_rows.append({
            "id":vid,"name":v["name"],"pincode":v["pincode"],"lat":lat,"lon":lon,
            "farmers":c.execute(f"SELECT COUNT(DISTINCT farmer_id) FROM animals WHERE village_id=? AND farmer_id IN ({q})",[vid]+farmer_ids).fetchone()[0],
            "animals":c.execute(f"SELECT COUNT(*) FROM animals WHERE village_id=? AND farmer_id IN ({q})",[vid]+farmer_ids).fetchone()[0],
            "open_cases":c.execute("SELECT COUNT(*) FROM cases cs JOIN animals a ON a.id=cs.animal_id WHERE a.village_id=? AND cs.vet_id=? AND cs.status!='CLOSED'",(vid,u["id"])).fetchone()[0],
            "outbreak_level":best["outbreak_status"] if best else "LOW",
            "outbreak_risk_score":best["outbreak_risk_score"] if best else 0,
            "top_disease":best["disease"] if best else None,
            "confirmed_outbreak":confirmed,
        })

    return jsonify(villages=village_rows, farmers=farmer_rows,
                   vet={"latitude":u["latitude"],"longitude":u["longitude"],"name":u["name"],"address":u["address"]})
@app.get("/api/sync")
def api_sync():
    """Process pending sync_queue items and push to MongoDB. Called by frontend when net returns or periodically."""
    if not MONGO_URI:
        c = conn()
        pending = c.execute("SELECT COUNT(*) FROM sync_queue WHERE status='PENDING'").fetchone()[0]
        c.close()
        return jsonify(ok=True, synced=0, pending=pending, msg="MongoDB not configured")
    
    c = conn()
    pending = c.execute("SELECT COUNT(*) FROM sync_queue WHERE status='PENDING'").fetchone()[0]
    
    if pending == 0:
        c.close()
        return jsonify(ok=True, synced=0, pending=0, msg="Nothing to sync")
    
    synced = 0
    failed = 0
    global mongo_client, mongo_db
    
    try:
        if mongo_db is None:
            mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
            mongo_db = mongo_client["gramvet"]
        
        for row in c.execute("SELECT id, collection, document_id, document_json FROM sync_queue WHERE status='PENDING' AND retry_count < 5 ORDER BY created_at LIMIT 100"):
            try:
                doc = json.loads(row["document_json"])
                mongo_db[row["collection"]].update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
                c.execute("UPDATE sync_queue SET status='SYNCED', synced_at=? WHERE id=?", (now(), row["id"]))
                synced += 1
            except Exception as e:
                c.execute("UPDATE sync_queue SET retry_count=retry_count+1, last_error=? WHERE id=?", (str(e), row["id"]))
                failed += 1
        
        c.commit()
    except Exception as e:
        print(f"[GramVet] Sync worker error: {e}", flush=True)
    finally:
        c.close()
    
    return jsonify(ok=True, synced=synced, pending=pending - synced, failed=failed, msg=f"Synced {synced}, {pending - synced} still pending")


if __name__=="__main__":
    init()
    app.run(host=os.environ.get("HOST","127.0.0.1"),port=int(os.environ.get("PORT","5000")),debug=True)
