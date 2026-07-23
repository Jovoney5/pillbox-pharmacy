"""
SQLite storage layer for The PillBox Pharmacy.

Why this exists: the app originally stored everything in JSON files. On a hosted
server those files can be wiped on redeploy/restart, losing orders, staff and
appointments. SQLite keeps the data in a single database file on the persistent
disk so it survives restarts.

Design: this module intentionally mirrors the old JSON helper functions
(load_orders / save_orders, load_staff / save_staff, etc.) so the rest of the app
keeps calling the same function names — only the storage underneath changed.

Data is stored per-table as a JSON blob per row to keep the migration faithful to
the existing dict shapes (no rigid columns to fight with while the schema is still
evolving toward a pilot).
"""

import os
import json
import sqlite3

# Put the DB on the Render persistent disk when available, else beside the code.
_DISK = "/opt/render/project/src"
_BASE = _DISK if os.path.isdir(_DISK) else os.path.dirname(__file__)
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(_BASE, "pillbox.db"))

# Tables that hold a JSON list of records, keyed by a string "id" field.
_LIST_TABLES = {"products", "orders", "staff", "appointments", "ads"}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = _connect()
    cur = conn.cursor()
    for table in _LIST_TABLES:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"  id   TEXT PRIMARY KEY,"
            f"  data TEXT NOT NULL"
            f")"
        )
    # users is a dict keyed by user id — same row shape works fine
    cur.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "  id   TEXT PRIMARY KEY,"
        "  data TEXT NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()


# ── Generic list-table read/write ─────────────────────────────────────────────

def read_list(table):
    """Return all rows in a list-table as a list of dicts (insertion order)."""
    conn = _connect()
    rows = conn.execute(f"SELECT data FROM {table} ORDER BY rowid").fetchall()
    conn.close()
    return [json.loads(r["data"]) for r in rows]


def write_list(table, records):
    """Replace the entire contents of a list-table with `records` (list of dicts)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table}")
    for rec in records:
        rec_id = str(rec.get("id"))
        cur.execute(
            f"INSERT INTO {table} (id, data) VALUES (?, ?)",
            (rec_id, json.dumps(rec)),
        )
    conn.commit()
    conn.close()


# ── Users (dict keyed by id) ──────────────────────────────────────────────────

def read_users():
    conn = _connect()
    rows = conn.execute("SELECT id, data FROM users").fetchall()
    conn.close()
    return {r["id"]: json.loads(r["data"]) for r in rows}


def write_users(users):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM users")
    for uid, rec in users.items():
        cur.execute(
            "INSERT INTO users (id, data) VALUES (?, ?)",
            (str(uid), json.dumps(rec)),
        )
    conn.commit()
    conn.close()


# ── One-time migration from the old JSON files ────────────────────────────────

def migrate_from_json(base_dir, default_staff=None):
    """
    Import existing JSON files into the DB the first time. Only imports a table
    if it's currently empty, so this is safe to run on every startup and won't
    clobber live data.
    """
    init_db()

    file_map = {
        "products":     "products.json",
        "orders":       "orders.json",
        "staff":        "staff.json",
        "appointments": "appointments.json",
        "ads":          "ads.json",
    }

    for table, filename in file_map.items():
        # Skip if this table already has data
        conn = _connect()
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        conn.close()
        if count > 0:
            continue

        path = os.path.join(base_dir, filename)
        records = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    records = json.load(f)
            except Exception as e:
                print(f"[db] could not read {filename}: {e}")
                records = []

        # Seed default staff if staff file was missing/empty
        if table == "staff" and not records and default_staff:
            records = default_staff

        if records:
            write_list(table, records)
            print(f"[db] migrated {len(records)} rows into '{table}'")

    # Users
    conn = _connect()
    ucount = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    conn.close()
    if ucount == 0:
        upath = os.path.join(base_dir, "users.json")
        if os.path.exists(upath):
            try:
                with open(upath) as f:
                    users = json.load(f)
                if users:
                    write_users(users)
                    print(f"[db] migrated {len(users)} users")
            except Exception as e:
                print(f"[db] could not read users.json: {e}")