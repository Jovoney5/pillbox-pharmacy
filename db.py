"""
Storage layer for Leeds Pharmacy.

Backends
--------
* **Postgres** — used automatically when the DATABASE_URL env var is set
  (e.g. on Render, pointing at the shared paid Postgres instance). Tables are
  prefixed with ``leeds_`` so they never collide with another app sharing the
  same database.
* **SQLite** — fallback for local development when DATABASE_URL isn't set.

Both backends persist data across restarts/redeploys. The rest of the app keeps
calling the same function names (load_orders/save_orders, etc.) — only the
storage underneath changes.

Data is stored per-table as a JSON blob per row to stay faithful to the existing
dict shapes (no rigid columns while the schema is still evolving).
"""

import os
import json
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

# Prefix keeps this app's tables separate from any other app sharing the DB.
TABLE_PREFIX = "leeds_"

# Logical table names used throughout the app.
_LIST_TABLES = {"products", "orders", "staff", "appointments", "ads", "events", "searches", "invoices"}
_ALL_TABLES = _LIST_TABLES | {"users"}


def _phys(table):
    """Physical table name (prefixed)."""
    return TABLE_PREFIX + table


# ── SQLite fallback paths ─────────────────────────────────────────────────────
_DISK = "/opt/render/project/src"
_BASE = _DISK if os.path.isdir(_DISK) else os.path.dirname(__file__)
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(_BASE, "pillbox.db"))


if USE_PG:
    import psycopg2
    import psycopg2.extras

    def _connect():
        conn = psycopg2.connect(DATABASE_URL)
        return conn

    def init_db():
        """Create prefixed tables if they don't exist. Safe to call on startup."""
        conn = _connect()
        cur = conn.cursor()
        for table in _ALL_TABLES:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_phys(table)} ("
                f"  id   TEXT PRIMARY KEY,"
                f"  seq  BIGSERIAL,"
                f"  data JSONB NOT NULL"
                f")"
            )
        conn.commit()
        cur.close()
        conn.close()

    def _read_rows(table):
        conn = _connect()
        cur = conn.cursor()
        cur.execute(f"SELECT id, data FROM {_phys(table)} ORDER BY seq")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows

    def _replace_rows(table, items):
        """items: list of (id, dict). Replace entire table contents."""
        conn = _connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_phys(table)}")
        for rec_id, rec in items:
            cur.execute(
                f"INSERT INTO {_phys(table)} (id, data) VALUES (%s, %s)",
                (str(rec_id), json.dumps(rec)),
            )
        conn.commit()
        cur.close()
        conn.close()

    def _count(table):
        conn = _connect()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {_phys(table)}")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return n

    def _row_data(row):
        # psycopg2 returns JSONB already decoded to dict
        d = row[1]
        return d if isinstance(d, dict) else json.loads(d)

else:
    def _connect():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        conn = _connect()
        cur = conn.cursor()
        for table in _ALL_TABLES:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_phys(table)} ("
                f"  id   TEXT PRIMARY KEY,"
                f"  data TEXT NOT NULL"
                f")"
            )
        conn.commit()
        conn.close()

    def _read_rows(table):
        conn = _connect()
        rows = conn.execute(f"SELECT id, data FROM {_phys(table)} ORDER BY rowid").fetchall()
        conn.close()
        return [(r["id"], r["data"]) for r in rows]

    def _replace_rows(table, items):
        conn = _connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {_phys(table)}")
        for rec_id, rec in items:
            cur.execute(
                f"INSERT INTO {_phys(table)} (id, data) VALUES (?, ?)",
                (str(rec_id), json.dumps(rec)),
            )
        conn.commit()
        conn.close()

    def _count(table):
        conn = _connect()
        n = conn.execute(f"SELECT COUNT(*) AS c FROM {_phys(table)}").fetchone()["c"]
        conn.close()
        return n

    def _row_data(row):
        return json.loads(row[1])


# ── Generic list-table read/write (same API as before) ────────────────────────

def read_list(table):
    """Return all rows in a list-table as a list of dicts (insertion order)."""
    return [_row_data(r) for r in _read_rows(table)]


def write_list(table, records):
    """Replace the entire contents of a list-table with `records` (list of dicts)."""
    _replace_rows(table, [(rec.get("id"), rec) for rec in records])


def append_row(table, record):
    """Insert ONE row efficiently (no full-table rewrite). For high-volume
    append-only tables like events/searches."""
    init_db()
    rec_id = str(record.get("id"))
    conn = _connect()
    cur = conn.cursor()
    if USE_PG:
        cur.execute(
            f"INSERT INTO {_phys(table)} (id, data) VALUES (%s, %s) "
            f"ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
            (rec_id, json.dumps(record)),
        )
    else:
        cur.execute(
            f"INSERT OR REPLACE INTO {_phys(table)} (id, data) VALUES (?, ?)",
            (rec_id, json.dumps(record)),
        )
    conn.commit()
    try:
        cur.close()
    except Exception:
        pass
    conn.close()


# ── Users (dict keyed by id) ──────────────────────────────────────────────────

def read_users():
    return {r_id: _row_data((r_id, data)) for (r_id, data) in _read_rows("users")}


def write_users(users):
    _replace_rows("users", [(uid, rec) for uid, rec in users.items()])


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
        if _count(table) > 0:
            continue  # already has data — don't clobber

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
    if _count("users") == 0:
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
