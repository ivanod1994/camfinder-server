# -*- coding: utf-8 -*-
import json
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

DATA_FILE = "devices.json"
ADMIN_PASS = "Ledevi3656610208"   # твой пароль администратора

# -----------------------
# Вспомогательные функции
# -----------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")

def add_days(days):
    return (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")

# -----------------------
# API для клиента (приложения)
# -----------------------

@app.route("/api/register_device", methods=["POST"])
def register_device():
    data = load_data()
    req = request.get_json(force=True)
    device_id = req.get("device_id")
    if not device_id:
        return jsonify({"error": "missing device_id"}), 400

    if device_id not in data:
        data[device_id] = {
            "device_id": device_id,
            "active": False,
            "free_left": 3,
            "expires_at": None,
            "created_at": now_iso(),
            "last_seen": now_iso()
        }
    else:
        data[device_id]["last_seen"] = now_iso()

    save_data(data)
    return jsonify({"ok": True, "device_id": device_id})

@app.route("/api/device_status", methods=["GET"])
def device_status():
    device_id = request.args.get("device_id")
    data = load_data()
    device = data.get(device_id)
    if not device:
        return jsonify({"active": False, "free_left": 0, "expires_at": None})

    # Проверка истечения подписки
    exp = device.get("expires_at")
    active = device.get("active", False)
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt < datetime.utcnow():
                device["active"] = False
                device["expires_at"] = None
                active = False
        except Exception:
            device["active"] = False
            device["expires_at"] = None
            active = False

    device["last_seen"] = now_iso()
    save_data(data)

    return jsonify({
        "device_id": device_id,
        "active": active,
        "free_left": device.get("free_left", 0),
        "expires_at": device.get("expires_at")
    })

@app.route("/api/update_free_count", methods=["POST"])
def update_free_count():
    req = request.get_json(force=True)
    device_id = req.get("device_id")
    change = int(req.get("change", 0))

    data = load_data()
    if device_id not in data:
        return jsonify({"error": "unknown device"}), 404

    d = data[device_id]
    d["free_left"] = max(0, d.get("free_left", 0) + change)
    d["last_seen"] = now_iso()
    save_data(data)

    return jsonify({"ok": True, "free_left": d["free_left"]})

@app.route("/api/verify_payment", methods=["POST"])
def verify_payment():
    """Заявка на активацию подписки — админ должен потом вручную активировать."""
    req = request.get_json(force=True)
    device_id = req.get("device_id")
    tx = req.get("tx", "")
    comment = req.get("comment", "")
    data = load_data()
    if not device_id:
        return jsonify({"error": "missing device_id"}), 400

    d = data.get(device_id)
    if not d:
        return jsonify({"error": "unknown device"}), 404

    d["last_payment"] = {
        "tx": tx,
        "comment": comment,
        "time": now_iso()
    }
    save_data(data)
    return jsonify({"ok": True})

# -----------------------
# Панель администратора
# -----------------------

@app.route("/admin", methods=["GET"])
def admin_page():
    pwd = request.args.get("pwd")
    if pwd != ADMIN_PASS:
        return "<h3>⛔ Доступ запрещён. Укажите ?pwd=ВашПароль</h3>", 403

    data = load_data()
    devices = list(data.values())
    devices.sort(key=lambda d: d.get("last_seen", ""), reverse=True)
    return render_template("admin.html", devices=devices, pwd=pwd)

@app.route("/admin/action", methods=["POST"])
def admin_action():
    pwd = request.args.get("pwd")
    if pwd != ADMIN_PASS:
        return jsonify({"error": "unauthorized"}), 403

    req = request.get_json(force=True)
    device_id = req.get("device_id")
    action = req.get("action")

    data = load_data()
    if device_id not in data:
        return jsonify({"error": "not found"}), 404

    d = data[device_id]
    result = {}

    if action == "activate_30d":
        d["active"] = True
        d["expires_at"] = add_days(30)
        result = {"msg": "Подписка выдана на 30 дней"}

    elif action == "remove_sub":
        d["active"] = False
        d["expires_at"] = None
        result = {"msg": "Подписка удалена"}

    elif action == "reset_free":
        d["free_left"] = 0
        result = {"msg": "Бесплатные попытки обнулены"}

    elif action == "give_free3":
        d["free_left"] = 3
        result = {"msg": "Выдано 3 бесплатных попытки"}

    elif action == "dev_mode":
        d["active"] = True
        d["expires_at"] = "2099-01-01T00:00:00"
        result = {"msg": "Режим разработчика активирован"}

    elif action == "delete_device":
        del data[device_id]
        save_data(data)
        return jsonify({"msg": "Устройство удалено"})

    d["last_seen"] = now_iso()
    save_data(data)
    return jsonify(result)

# -----------------------
# Главная
# -----------------------
@app.route("/")
def index():
    return "CamFinder API Server is running ✅"

# -----------------------
# Статические файлы (если надо)
# -----------------------
@app.route("/templates/<path:path>")
def send_templates(path):
    return send_from_directory("templates", path)

# -----------------------
# Запуск
# -----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
