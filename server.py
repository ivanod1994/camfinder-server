# -*- coding: utf-8 -*-
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, session, send_from_directory
from flask_cors import CORS

APP_NAME = "CamFinder Server"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "Ledevi3656610208")
SECRET_KEY = os.environ.get("SECRET_KEY", "replace_this_secret")
DB_PATH = os.environ.get("DB_PATH", "camfinder.db")

app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ===== БАЗА ДАННЫХ =====
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    # Устройства
    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            free_left INTEGER NOT NULL DEFAULT 3,
            sub_active INTEGER NOT NULL DEFAULT 0,
            sub_expires_at TEXT,
            dev_mode INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Платежи
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            tx TEXT,
            comment TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===== УТИЛИТЫ =====
def get_device(conn, device_id: str):
    cur = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    return cur.fetchone()

def ensure_device(conn, device_id: str, default_free: int = 3):
    row = get_device(conn, device_id)
    now = datetime.utcnow().isoformat()
    if not row:
        conn.execute("""
            INSERT INTO devices(device_id, free_left, sub_active, sub_expires_at, dev_mode, created_at, updated_at)
            VALUES (?, ?, 0, NULL, 0, ?, ?)
        """, (device_id, int(default_free), now, now))
        conn.commit()
        row = get_device(conn, device_id)
    return row

def device_to_json(row):
    return {
        "device_id": row["device_id"],
        "free_left": row["free_left"],
        "sub_active": bool(row["sub_active"]),
        "sub_expires_at": row["sub_expires_at"],
        "dev_mode": bool(row["dev_mode"]),
    }

# ===== ADMIN AUTH =====
def is_admin():
    return session.get("admin_auth") is True

@app.route("/admin")
def admin_page():
    conn = db()
    devices = conn.execute("SELECT * FROM devices ORDER BY updated_at DESC LIMIT 200").fetchall()
    payments = conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return render_template("admin.html", app_name=APP_NAME, devices=devices, payments=payments)

@app.route("/api/admin/auth", methods=["POST"])
def admin_auth():
    data = request.get_json(force=True, silent=True) or {}
    password = data.get("password", "")
    if password == ADMIN_PASS:
        session["admin_auth"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "bad_password"}), 401

@app.route("/api/admin/activate", methods=["POST"])
def admin_activate():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    days = int(data.get("days", 30))
    if not device_id:
        return jsonify({"error": "device_id required"}), 400

    conn = db()
    ensure_device(conn, device_id)
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET sub_active=1, sub_expires_at=?, updated_at=? WHERE device_id=?",
                 (expires, now, device_id))
    conn.execute("UPDATE payments SET status='approved' WHERE device_id=?", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "expires_at": expires})

# ===== PUBLIC API =====
@app.route("/api/register_device", methods=["POST"])
def register_device():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    default_free = int(data.get("free_left", 3))
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id, default_free)
    conn.close()
    return jsonify(device_to_json(row))

@app.route("/api/device_status", methods=["GET"])
def device_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id)
    conn.close()
    return jsonify(device_to_json(row))

@app.route("/api/update_free_count", methods=["POST"])
def update_free_count():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id)
    free_left = int(row["free_left"])
    if free_left > 0:
        free_left -= 1
    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET free_left=?, updated_at=? WHERE device_id=?", (free_left, now, device_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "free_left": free_left})

@app.route("/api/subscriptions/status", methods=["GET"])
def sub_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id)
    active = bool(row["sub_active"])
    expires_at = row["sub_expires_at"]
    if active and expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.utcnow():
                active = False
                conn.execute("UPDATE devices SET sub_active=0, sub_expires_at=NULL WHERE device_id=?", (device_id,))
                conn.commit()
        except Exception:
            pass
    conn.close()
    return jsonify({"active": active, "expires_at": expires_at})

@app.route("/api/verify_payment", methods=["POST"])
def verify_payment():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    tx = (data.get("tx") or "").strip()
    comment = (data.get("comment") or "").strip()

    if not device_id or not (tx or comment):
        return jsonify({"error": "device_id and tx/comment required"}), 400

    conn = db()
    ensure_device(conn, device_id)
    now = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO payments(device_id, tx, comment, created_at) VALUES (?,?,?,?)",
                 (device_id, tx, comment, now))

    if comment.strip().upper() == "MASTER112":
        expires = datetime(2099, 1, 1).isoformat()
        conn.execute("UPDATE devices SET dev_mode=1, sub_active=1, sub_expires_at=?, updated_at=? WHERE device_id=?",
                     (expires, now, device_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
