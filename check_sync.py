import sqlite3
import os

# This is for your machine - run it in your gramvet folder
db_path = "./gramvet.db"

if not os.path.exists(db_path):
    print("❌ gramvet.db not found! Run the Flask app first.")
    exit(1)

db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=" * 60)
print("GRAMVET SYNC DIAGNOSTICS")
print("=" * 60)

# 1. Check if sync_queue table exists
try:
    count = db.execute("SELECT COUNT(*) cnt FROM sync_queue").fetchone()['cnt']
    print(f"\n✅ sync_queue table EXISTS")
    print(f"   Total items queued: {count}")
except Exception as e:
    print(f"\n❌ sync_queue table NOT found: {e}")
    exit(1)

# 2. Show items by collection and status
print("\n📋 Items by Collection & Status:")
rows = db.execute("""
    SELECT collection, status, COUNT(*) cnt 
    FROM sync_queue 
    GROUP BY collection, status
    ORDER BY collection, status
""").fetchall()

if not rows:
    print("   (empty)")
else:
    for r in rows:
        print(f"   {r['collection']:20s} | {r['status']:10s} | {r['cnt']} items")

# 3. Show last 5 sync attempts
print("\n📊 Last 5 sync queue entries:")
entries = db.execute("""
    SELECT id, collection, status, retry_count, last_error, created_at 
    FROM sync_queue 
    ORDER BY created_at DESC 
    LIMIT 5
""").fetchall()

if not entries:
    print("   (empty)")
else:
    for e in entries:
        print(f"\n   [{e['id']}] {e['collection']:15s} | {e['status']:8s}")
        print(f"        Retries: {e['retry_count']} | Created: {e['created_at']}")
        if e['last_error']:
            print(f"        Error: {e['last_error'][:80]}")

# 4. Show total data in each table
print("\n📈 Data in SQLite tables:")
tables = ['users', 'animals', 'cases', 'vaccinations', 'case_actions', 'villages']
for table in tables:
    try:
        count = db.execute(f"SELECT COUNT(*) cnt FROM {table}").fetchone()['cnt']
        print(f"   {table:20s}: {count} records")
    except:
        print(f"   {table:20s}: (table not found)")

# 5. Show latest users and vaccines
print("\n👤 Latest 3 users created:")
users = db.execute("""
    SELECT id, name, phone, role, created_at 
    FROM users 
    ORDER BY id DESC 
    LIMIT 3
""").fetchall()
for u in users:
    print(f"   ID {u['id']:3d}: {u['name']:20s} ({u['role']:8s}) - {u['created_at']}")

print("\n💉 Latest 3 vaccinations:")
vaccines = db.execute("""
    SELECT id, vaccine_name, case_id, administered_at 
    FROM vaccinations 
    ORDER BY id DESC 
    LIMIT 3
""").fetchall()
if vaccines:
    for v in vaccines:
        print(f"   ID {v['id']:3d}: {v['vaccine_name']:15s} - Case {v['case_id']:3d} - {v['administered_at']}")
else:
    print("   (none yet)")

db.close()
print("\n" + "=" * 60)
