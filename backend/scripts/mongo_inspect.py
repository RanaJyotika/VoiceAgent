"""What is in the Vedic Vaibhav MongoDB, for setting up the product catalogue. Free: reads only.

    .venv\\Scripts\\python.exe backend\scripts\\mongo_inspect.py

Lists the database's collections with their sizes, then shows the layout (field names and types, values
cut short) of one document from each collection whose name mentions products or collections. Other
collections (customers, orders...) are only counted, never printed. Then reads the catalogue the way
the server will and prints the first products as the agent would see them.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app import catalog  # noqa: E402
from app.config import MONGODB_DB, MONGODB_URI  # noqa: E402


def layout(value, indent: str = "  ", depth: int = 0) -> None:
    if isinstance(value, dict):
        for k, v in list(value.items())[:40]:
            kind = type(v).__name__
            if isinstance(v, (dict, list)) and depth < 3:
                print(f"{indent}{k}: {kind}{f' [{len(v)}]' if isinstance(v, list) else ''}")
                layout(v[0] if isinstance(v, list) and v else v if isinstance(v, dict) else {}, indent + "  ", depth + 1)
            else:
                text = str(v).replace("\n", " ")
                print(f"{indent}{k}: {kind} = {text[:60]}{'…' if len(text) > 60 else ''}")


async def main() -> None:
    if not MONGODB_URI:
        print("Set MONGODB_URI in .env first (a read-only user).")
        return
    from pymongo import AsyncMongoClient
    client = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
    try:
        db = client[MONGODB_DB]
        names = sorted(await db.list_collection_names())
        print(f"Database {MONGODB_DB}: {len(names)} collections")
        for n in names:
            print(f"  {n}: {await db[n].estimated_document_count()} documents")
        for n in names:
            if "product" in n.lower() or "collection" in n.lower():
                doc = await db[n].find_one({"isDeleted": {"$ne": True}, "productsCount": {"$ne": 0}}) or await db[n].find_one()
                print(f"\n=== one document of {n} ===")
                layout(doc or {})
    finally:
        await client.close()
    print("\n=== the catalogue as the server reads it ===")
    s = await catalog.refresh()
    print(s)
    for p in catalog.STATE["products"][:5]:
        print(catalog.facts(p))


asyncio.run(main())
