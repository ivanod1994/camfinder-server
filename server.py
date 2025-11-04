#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS

# -----------------------------
# Configuration
# -----------------------------
ADMIN_KEY = os.getenv("ADMIN_KEY", "MASTER112")     # Admin secret
APP_NAME  = os.getenv("APP_NAME",  "camfinder-server")
PORT      = int(os.getenv("PORT", "8000"))
DB_PATH   = os.getenv("DATABASE_URL", "database.db")  # sqlite file path on Railway

# -----------------------------
# App init
# -----------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app, resources={r"/api/*": {"origins": "*"}})

def now_utc():
    return datetime.now(timezone.utc)

# -----------------------------
# DB helpers
# -----------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS devices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT UNIQUE,
        created_ts TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT,
        is_active INTEGER DEFAULT 0,
        is_dev INTEGER DEFAULT 0,
        start_ts TEXT,
        end_ts TEXT,
        created_ts TEXT,
        updated_ts TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT,
        tx TEXT,
        comment TEXT,
        status TEXT,              -- pending/approved/rejected
        amount REAL,
        currency TEXT,
        created_ts TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        action TEXT,
        who_ip TEXT,
        meta TEXT
    );
    """)
    conn.commit()
    conn.close()

def log_audit(action, meta=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log(ts, action, who_ip, meta) VALUES (?, ?, ?, ?)",
        (now_utc().isoformat(), action, request.remote_addr or "-", meta),
    )
    conn.commit()
    conn.close()

def ensure_device(device_id:str):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO devices(device_id, created_ts) VALUES(?, ?)",
        (device_id, now_utc().isoformat())
    )
    conn.commit()
    conn.close()

def get_current_sub(device_id:str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM subscriptions
        WHERE device_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (device_id,))
    row = cur.fetchone()
    conn.close()
    return row

# -----------------------------
# Routes - public API
# -----------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True, "app": APP_NAME, "time": now_utc().isoformat()})

@app.get("/api/v1/subscriptions/status")
def sub_status():
    device_id = request.args.get("device_id", "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    ensure_device(device_id)
    row = get_current_sub(device_id)
    active = False
    is_dev = False
    expires_at = None

    if row:
        is_dev  = bool(row["is_dev"])
        if row["end_ts"]:
            expires_at = row["end_ts"]
            # active if now < end
            try:
                active = now_utc() < datetime.fromisoformat(expires_at)
            except Exception:
                active = False
        # dev mode is always considered active, expires_at may be null
        if is_dev:
            active = True

    payload = {
        "ok": True,
        "sub_active": bool(active),
        "dev": bool(is_dev),
        "expires_at": expires_at,
        "now": now_utc().isoformat()
    }
    return jsonify(payload)

@app.post("/api/v1/subscriptions/submit")
def submit_payment():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    tx       = str(data.get("tx", "")).strip()
    comment  = str(data.get("comment", "")).strip()
    amount   = data.get("amount")
    currency = data.get("currency")

    if not device_id or not tx:
        return jsonify({"ok": False, "error": "device_id and tx are required"}), 400

    ensure_device(device_id)
    conn = get_db()
    conn.execute("""
        INSERT INTO payments(device_id, tx, comment, status, amount, currency, created_ts)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
    """, (device_id, tx, comment, amount, currency, now_utc().isoformat()))
    conn.commit()
    conn.close()
    log_audit("payment_submit", f"device={device_id} tx={tx[:16]}... comment={comment}")
    return jsonify({"ok": True, "queued": True})

# -----------------------------
# Admin API (protected by ADMIN_KEY)
# -----------------------------
def require_admin():
    key = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    return key and key == ADMIN_KEY

@app.get("/api/v1/admin/pending")
def admin_pending():
    if not require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "items": rows})

@app.post("/api/v1/admin/activate")
def admin_activate():
    if not require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    months    = int(data.get("months", 1))
    dev       = bool(data.get("dev", False))
    payment_id = data.get("payment_id")

    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    ensure_device(device_id)

    start = now_utc()
    end   = None if dev else (start + timedelta(days=30*max(1, months)))

    conn = get_db()
    conn.execute("""
        INSERT INTO subscriptions(device_id, is_active, is_dev, start_ts, end_ts, created_ts, updated_ts)
        VALUES(?, ?, ?, ?, ?, ?, ?)
    """, (device_id, 1, 1 if dev else 0,
          start.isoformat(), None if dev else end.isoformat(),
          start.isoformat(), start.isoformat()))
    if payment_id:
        conn.execute("UPDATE payments SET status = 'approved' WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
    log_audit("admin_activate", f"device={device_id} dev={dev} months={months}")
    return jsonify({"ok": True, "activated": True, "dev": dev, "expires_at": None if dev else end.isoformat()})

@app.post("/api/v1/admin/reject")
def admin_reject():
    if not require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    payment_id = data.get("payment_id")
    if not payment_id:
        return jsonify({"ok": False, "error": "payment_id required"}), 400
    conn = get_db()
    conn.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
    log_audit("admin_reject", f"payment_id={payment_id}")
    return jsonify({"ok": True})

@app.post("/api/v1/admin/revoke")
def admin_revoke():
    if not require_admin():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    conn = get_db()
    conn.execute("""
        INSERT INTO subscriptions(device_id, is_active, is_dev, start_ts, end_ts, created_ts, updated_ts)
        VALUES(?, 0, 0, ?, ?, ?, ?)
    """, (device_id, now_utc().isoformat(), now_utc().isoformat(), now_utc().isoformat(), now_utc().isoformat()))
    conn.commit()
    conn.close()
    log_audit("admin_revoke", f"device={device_id}")
    return jsonify({"ok": True, "revoked": True})

# -----------------------------
# Admin UI
# -----------------------------
@app.get("/admin")
def admin_page():
    return render_template("admin.html", app_name=APP_NAME)

@app.get("/")
def index():
    return render_template("admin.html", app_name=APP_NAME)

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT)
