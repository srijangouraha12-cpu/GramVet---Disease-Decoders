import os
import sqlite3
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables
load_dotenv()

# -----------------------------
# MongoDB connection
# -----------------------------
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise Exception("MONGO_URI not found in .env")

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["gramvet"]

# -----------------------------
# SQLite connection
# -----------------------------
sqlite_conn = sqlite3.connect("gramvet.db")
sqlite_conn.row_factory = sqlite3.Row

# -----------------------------
# Tables to migrate
# -----------------------------
tables = [
    "users",
    "villages",
    "vet_villages",
    "animals",
    "cases",
    "health_reports",
    "case_actions",
    "vaccinations",
    "inventory_transactions",
    "outbreak_assessments",
    "resource_inventory",
    "resource_requests",
    "notifications"
]

print("\nStarting migration...\n")

for table in tables:

    print(f"Migrating: {table}")

    # Get all rows from SQLite
    rows = sqlite_conn.execute(
        f"SELECT * FROM {table}"
    ).fetchall()

    documents = []

    for row in rows:
        document = dict(row)

        # Preserve the original SQLite ID as MongoDB _id
        if "id" in document and document["id"] is not None:
            document["_id"] = document["id"]

        documents.append(document)

    collection = mongo_db[table]

    # Clear existing data in this collection
    collection.delete_many({})

    # Insert data
    if documents:
        collection.insert_many(documents)

    print(f"  ✓ {len(documents)} records migrated")

sqlite_conn.close()
mongo_client.close()

print("\n================================")
print("✅ MIGRATION COMPLETED")
print("================================")