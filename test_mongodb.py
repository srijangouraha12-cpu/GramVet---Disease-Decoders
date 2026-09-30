import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

try:
    client = MongoClient(MONGO_URI)

    # Test the connection
    client.admin.command("ping")

    print("✅ MongoDB connected successfully!")

except Exception as e:
    print("❌ MongoDB connection failed!")
    print(e)