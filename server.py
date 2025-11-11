# -*- coding: utf-8 -*-
import os
import sqlite3
import threading
import time
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

# ===== DB helpers =====
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            free_left INTEGER NOT NULL DEFAULT 3,
            sub_active INTEGER NOT NULL DEFAULT 0,
            sub_expires_at TEXT,
            dev_mode INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen TEXT
        )
    """)
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
    # на случай старой схемы — добавить недостающие поля
    try:
        c.execute("ALTER TABLE devices ADD COLUMN last_seen TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

def get_device(conn, device_id: str):
    return conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()

def ensure_device(conn, device_id: str, wished_free: int = 3):
    """
    Гарантирует, что устройство есть в БД.
    Если существует — НЕ увеличиваем free_left (защита от переустановки).
    """
    row = get_device(conn, device_id)
    now = datetime.utcnow().isoformat()
    if not row:
        conn.execute("""
            INSERT INTO devices(device_id, free_left, sub_active, sub_expires_at, dev_mode, created_at, updated_at, last_seen)
            VALUES (?, ?, 0, NULL, 0, ?, ?, ?)
        """, (device_id, int(wished_free), now, now, now))
        conn.commit()
        row = get_device(conn, device_id)
    return row

def device_json(row):
    return {
        "device_id": row["device_id"],
        "free_left": int(row["free_left"]),
        "sub_active": bool(row["sub_active"]),
        "sub_expires_at": row["sub_expires_at"],
        "dev_mode": bool(row["dev_mode"]),
    }

# ===== Background maintenance (every 10s) =====
def maintenance_loop():
    while True:
        try:
            conn = db()
            now = datetime.utcnow()
            # снять просроченные подписки
            rows = conn.execute("SELECT device_id, sub_expires_at FROM devices WHERE sub_active=1 AND sub_expires_at IS NOT NULL").fetchall()
            to_deactivate = []
            for r in rows:
                try:
                    if datetime.fromisoformat(r["sub_expires_at"]) < now:
                        to_deactivate.append(r["device_id"])
                except Exception:
                    pass
            if to_deactivate:
                ts = now.isoformat()
                for did in to_deactivate:
                    conn.execute("UPDATE devices SET sub_active=0, sub_expires_at=NULL, updated_at=? WHERE device_id=?", (ts, did))
                conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        time.sleep(10)

threading.Thread(target=maintenance_loop, daemon=True).start()

# ===== Admin auth =====
def is_admin():
    return session.get("admin_auth") is True

@app.route("/admin")
def admin_page():
    conn = db()
    devices = conn.execute("SELECT * FROM devices ORDER BY updated_at DESC LIMIT 500").fetchall()
    payments = conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 500").fetchall()
    conn.close()
    return render_template("admin.html", app_name=APP_NAME, devices=devices, payments=payments)

@app.route("/api/admin/auth", methods=["POST"])
def admin_auth():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("password") == ADMIN_PASS:
        session["admin_auth"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "bad_password"}), 401

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_auth", None)
    return jsonify({"ok": True})

# Activate 30 days
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
    ts = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET sub_active=1, sub_expires_at=?, updated_at=? WHERE device_id=?", (expires, ts, device_id))
    conn.execute("UPDATE payments SET status='approved' WHERE device_id=?", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "expires_at": expires})

# Toggle dev mode
@app.route("/api/admin/dev_mode", methods=["POST"])
def admin_dev_mode():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    enable = bool(data.get("enable", True))
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    ensure_device(conn, device_id)
    ts = datetime.utcnow().isoformat()
    if enable:
        expires = datetime(2099, 1, 1).isoformat()
        conn.execute("UPDATE devices SET dev_mode=1, sub_active=1, sub_expires_at=?, updated_at=? WHERE device_id=?", (expires, ts, device_id))
    else:
        conn.execute("UPDATE devices SET dev_mode=0, updated_at=? WHERE device_id=?", (ts, device_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Set free_left (0 or 3)
@app.route("/api/admin/set_free_left", methods=["POST"])
def admin_set_free_left():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    value = int(data.get("value", 0))
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    ensure_device(conn, device_id)
    ts = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET free_left=?, updated_at=? WHERE device_id=?", (value, ts, device_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ===== Public API =====

# 1) автозаведение устройства при первом запуске
@app.route("/api/register_device", methods=["POST"])
def register_device():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    wished_free = int(data.get("free_left", 3))
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id, wished_free)
    conn.close()
    return jsonify(device_json(row))

# 2) пульс раз в 10 сек из приложения (обновляет last_seen и возвращает статус)
@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id)
    ts = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET last_seen=?, updated_at=? WHERE device_id=?", (ts, ts, device_id))
    conn.commit()

    # после апдейта — возвращаем актуальный статус (учитывая возможное авто-снятие подписки в бэке)
    row = get_device(conn, device_id)
    # если подписка просрочилась прямо сейчас — снимем флаг синхронно
    active = bool(row["sub_active"])
    expires_at = row["sub_expires_at"]
    if active and expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.utcnow():
                active = False
                conn.execute("UPDATE devices SET sub_active=0, sub_expires_at=NULL, updated_at=? WHERE device_id=?", (ts, device_id))
                conn.commit()
                row = get_device(conn, device_id)
        except Exception:
            pass
    conn.close()
    return jsonify(device_json(row))

# 3) точечный опрос статуса
@app.route("/api/device_status", methods=["GET"])
def device_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    conn = db()
    row = ensure_device(conn, device_id)
    conn.close()
    return jsonify(device_json(row))

# 4) списать бесплатную попытку (вызывай после успешного поиска)
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
    ts = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET free_left=?, updated_at=? WHERE device_id=?", (free_left, ts, device_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "free_left": free_left})

# 5) статус подписки
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

# 6) заявка на оплату (TX/коммент)
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
    ts = datetime.utcnow().isoformat()
    conn.execute("INSERT INTO payments(device_id, tx, comment, created_at) VALUES (?,?,?,?)",
                 (device_id, tx, comment, ts))
    # dev-код через сервер
    if comment.upper() == "MASTER112":
        expires = datetime(2099, 1, 1).isoformat()
        conn.execute("UPDATE devices SET dev_mode=1, sub_active=1, sub_expires_at=?, updated_at=? WHERE device_id=?",
                     (expires, ts, device_id))
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
