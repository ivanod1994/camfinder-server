# -*- coding: utf-8 -*-
import os
import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any

from flask import Flask, request, jsonify, render_template, redirect, url_for, session, abort
from flask_cors import CORS

# ------------------------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------------------------
APP_NAME = "CamFinder API Server"
DB_FILE = os.environ.get("DEVICES_DB", "devices.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Ledevi3656610208")
SECRET_KEY = os.environ.get("SECRET_KEY", "camfinder-secret-" + uuid.uuid4().hex)

# Cколько бесплатных поисков давать изначально
INITIAL_FREE = 3

app = Flask(__name__, template_folder="templates", static_folder=None)
app.config["SECRET_KEY"] = SECRET_KEY
CORS(app, resources={r"/api/*": {"origins": "*"}})

_db_lock = threading.Lock()

# ------------------------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.utcnow()

def to_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.replace(microsecond=0).isoformat()

def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_FILE):
        return {"devices": {}}
    with _db_lock:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "devices" not in data or not isinstance(data["devices"], dict):
                data = {"devices": {}}
            return data
        except Exception:
            return {"devices": {}}

def save_db(data: Dict[str, Any]) -> None:
    with _db_lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)

def ensure_device(d: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    devices = d.setdefault("devices", {})
    if device_id not in devices:
        devices[device_id] = {
            "device_id": device_id,
            "created_at": to_iso(now_utc()),
            "last_seen": to_iso(now_utc()),
            "free_left": INITIAL_FREE,
            "sub_active": False,
            "sub_expires_at": None,
            "dev_mode": False,           # если True — всегда активная подписка
            "tx_history": [],            # [{tx, comment, at}]
            "last_tx": None,
            "last_comment": None,
        }
    else:
        devices[device_id]["last_seen"] = to_iso(now_utc())
    return devices[device_id]

def recalc_subscription_state(dev: Dict[str, Any]) -> None:
    """Деактивирует подписку, если срок истёк."""
    if dev.get("dev_mode"):
        dev["sub_active"] = True
        dev["sub_expires_at"] = None
        return
    if dev.get("sub_active"):
        exp = from_iso(dev.get("sub_expires_at"))
        if exp and now_utc() > exp:
            dev["sub_active"] = False
            dev["sub_expires_at"] = None

def snapshot(dev: Dict[str, Any]) -> Dict[str, Any]:
    recalc_subscription_state(dev)
    active = bool(dev.get("sub_active")) or bool(dev.get("dev_mode"))
    free_left = int(dev.get("free_left", 0))
    locked = (not active) and (free_left <= 0)
    return {
        "device_id": dev["device_id"],
        "active": active,
        "expires_at": dev["sub_expires_at"],
        "free_left": free_left,
        "locked": locked,
        "dev_mode": bool(dev.get("dev_mode", False)),
        "last_tx": dev.get("last_tx"),
        "last_comment": dev.get("last_comment"),
    }

# ------------------------------------------------------------------------------
# HTML: главная + админ
# ------------------------------------------------------------------------------
def require_admin():
    if not session.get("is_admin"):
        abort(401)

@app.route("/")
def index_page():
    return render_template("index.html", app_name=APP_NAME)

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin.html", app_name=APP_NAME, error="Неверный пароль", devices=[])
    if not session.get("is_admin"):
        return render_template("admin.html", app_name=APP_NAME, devices=[])
    # уже залогинен — на дашборд
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/dashboard")
def admin_dashboard():
    require_admin()
    data = load_db()
    devices = list(data.get("devices", {}).values())
    # сортировка: сначала активные/в dev, потом по дате последнего появления
    def sort_key(x):
        return (
            0 if (x.get("dev_mode") or x.get("sub_active")) else 1,
            x.get("last_seen", ""),
        )
    devices.sort(key=sort_key)
    return render_template("admin.html", app_name=APP_NAME, devices=devices)

@app.route("/admin/action", methods=["POST"])
def admin_action():
    require_admin()
    act = request.form.get("action")
    device_id = request.form.get("device_id")

    data = load_db()
    dev = ensure_device(data, device_id)

    if act == "grant30":
        # Выдать подписку на 30 дней (перекрывает бесплатные попытки!)
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=30))
    elif act == "remove_sub":
        dev["sub_active"] = False
        dev["sub_expires_at"] = None
    elif act == "toggle_dev":
        dev["dev_mode"] = not bool(dev.get("dev_mode"))
        if dev["dev_mode"]:
            dev["sub_active"] = True
            dev["sub_expires_at"] = None
    elif act == "reset_free":
        dev["free_left"] = INITIAL_FREE
    elif act == "zero_free":
        dev["free_left"] = 0
    elif act == "delete":
        data["devices"].pop(device_id, None)
        save_db(data)
        return redirect(url_for("admin_dashboard"))
    elif act == "clear_tx":
        dev["last_tx"] = None
        dev["last_comment"] = None
        dev["tx_history"] = []

    save_db(data)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_page"))

# ------------------------------------------------------------------------------
# API: регистрация, статус, попытки, платежи, подписки
# Совместимость с клиентом: оставлены те же URL и поля
# ------------------------------------------------------------------------------
@app.route("/api/register_device", methods=["POST"])
def api_register_device():
    """
    Вызов при первом запуске приложения и затем периодически.
    При первом визите создаёт устройство с free_left=3.
    Возвращает снапшот.
    """
    payload = request.get_json(force=True, silent=True) or {}
    device_id = (payload.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    data = load_db()
    dev = ensure_device(data, device_id)
    save_db(data)
    return jsonify({"ok": True, "device": snapshot(dev)})

@app.route("/api/device_status", methods=["GET"])
def api_device_status():
    """
    Клиент регулярно опрашивает. Возвращаем active, expires_at, free_left, locked.
    """
    device_id = (request.args.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    data = load_db()
    dev = ensure_device(data, device_id)
    save_db(data)
    return jsonify(snapshot(dev))

# Совместимость с клиентом: /api/subscriptions/status
@app.route("/api/subscriptions/status", methods=["GET"])
def api_subscription_status():
    device_id = (request.args.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    data = load_db()
    dev = ensure_device(data, device_id)
    snap = snapshot(dev)
    save_db(data)
    return jsonify({"active": snap["active"], "expires_at": snap["expires_at"]})

@app.route("/api/update_free_count", methods=["POST"])
def api_update_free_count():
    """
    Клиент уменьшает счётчик бесплатных попыток после УСПЕШНОГО поиска.
    Если есть активная подписка (или dev_mode), free_left НЕ трогаем.
    """
    payload = request.get_json(force=True, silent=True) or {}
    device_id = (payload.get("device_id") or "").strip()
    consumed = int(payload.get("consumed", 1))

    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400
    if consumed <= 0:
        consumed = 1

    data = load_db()
    dev = ensure_device(data, device_id)
    recalc_subscription_state(dev)

    active = bool(dev.get("sub_active")) or bool(dev.get("dev_mode"))
    if not active:
        # уменьшаем free_left только если нет подписки
        dev["free_left"] = max(0, int(dev.get("free_left", 0)) - consumed)

    save_db(data)
    return jsonify({"ok": True, "free_left": int(dev.get("free_left", 0)), "active": active})

@app.route("/api/verify_payment", methods=["POST"])
def api_verify_payment():
    """
    Клиент отправляет TX/комментарий после оплаты. Подписку не активируем автоматически —
    ты подтверждаешь в админке. Мы фиксируем в истории и сохраняем last_tx / last_comment.
    """
    payload = request.get_json(force=True, silent=True) or {}
    device_id = (payload.get("device_id") or "").strip()
    tx = (payload.get("tx") or "").strip()
    comment = (payload.get("comment") or "").strip()

    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400
    if not tx and not comment:
        return jsonify({"ok": False, "error": "tx or comment required"}), 400

    data = load_db()
    dev = ensure_device(data, device_id)

    rec = {
        "tx": tx or None,
        "comment": comment or None,
        "at": to_iso(now_utc()),
    }
    dev["tx_history"] = list(dev.get("tx_history", []))
    dev["tx_history"].append(rec)
    dev["last_tx"] = rec["tx"]
    dev["last_comment"] = rec["comment"]

    save_db(data)
    return jsonify({"ok": True, "message": "TX received", "device": snapshot(dev)})

# ------------------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------------------
# Для Railway / Gunicorn точка входа: app
if __name__ == "__main__":
    # локально: python server.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
