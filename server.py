# server.py
# Flask сервер для CamGirlsFinder
# Поддерживает:
#  - хранение активных подписок и оставшихся бесплатных попыток
#  - выдачу статуса (GET /api/subscriptions/status)
#  - списание попытки (POST /api/subscriptions/consume)
#  - создание заявки (POST /api/verify_payment)
#  - админ-панель /admin (templates/admin.html)
# Совместим с Railway + gunicorn

import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from threading import Lock

app = Flask(__name__, template_folder="templates")
CORS(app)

DATA_FILE = "subscriptions.json"
ADMIN_PASSWORD = "Ledevi3656610208"
DEV_CODE = "MASTER112"
SUBSCRIPTION_DAYS = 30
FREE_TRIES = 3

_lock = Lock()

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_data(data):
    with _lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def cleanup_expired(data):
    now = datetime.utcnow()
    to_del = []
    for dev, info in data.items():
        exp = info.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp)
                if exp_dt < now:
                    info["active"] = False
                    info["expires_at"] = None
            except Exception:
                continue
    return data

# ============ API ============

@app.route("/api/subscriptions/status")
def api_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "no device_id"}), 400
    data = load_data()
    data = cleanup_expired(data)
    user = data.get(device_id, {"free_left": FREE_TRIES, "active": False, "expires_at": None})

    # если у юзера нет записи — создаем
    if device_id not in data:
        data[device_id] = user
        save_data(data)

    return jsonify({
        "device_id": device_id,
        "active": user.get("active", False),
        "expires_at": user.get("expires_at"),
        "free_left": user.get("free_left", 0)
    })

@app.route("/api/subscriptions/consume", methods=["POST"])
def api_consume():
    payload = request.get_json(force=True)
    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "no device_id"}), 400
    data = load_data()
    user = data.get(device_id)
    if not user:
        user = {"free_left": FREE_TRIES, "active": False, "expires_at": None}

    if user.get("active"):
        return jsonify({"status": "subscribed"}), 200

    if user["free_left"] <= 0:
        return jsonify({"error": "no tries left"}), 403

    user["free_left"] -= 1
    data[device_id] = user
    save_data(data)
    return jsonify({"status": "ok", "free_left": user["free_left"]})

@app.route("/api/verify_payment", methods=["POST"])
def api_verify_payment():
    payload = request.get_json(force=True)
    device_id = payload.get("device_id")
    tx = (payload.get("tx") or "").strip()
    comment = (payload.get("comment") or "").strip()

    if not device_id:
        return jsonify({"error": "no device_id"}), 400

    data = load_data()
    user = data.get(device_id, {"free_left": FREE_TRIES, "active": False, "expires_at": None})

    # Режим разработчика через код в комментарии
    if comment.strip().upper() == DEV_CODE:
        user["active"] = True
        user["expires_at"] = (datetime.utcnow() + timedelta(days=3650)).isoformat()
        data[device_id] = user
        save_data(data)
        return jsonify({"status": "activated_dev"})

    # Сохраняем заявку для ручного подтверждения
    user["pending_tx"] = tx
    data[device_id] = user
    save_data(data)
    return jsonify({"status": "pending"})

# ============ ADMIN PANEL ============

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    data = load_data()
    data = cleanup_expired(data)
    save_data(data)

    if request.method == "POST":
        password = request.form.get("password")
        if password != ADMIN_PASSWORD:
            return "❌ Неверный пароль администратора", 403

        action = request.form.get("action")
        device_id = request.form.get("device_id")

        if not device_id:
            return "Нет device_id", 400

        if action == "activate":
            days = int(request.form.get("days") or SUBSCRIPTION_DAYS)
            exp = (datetime.utcnow() + timedelta(days=days)).isoformat()
            data[device_id] = {
                "active": True,
                "expires_at": exp,
                "free_left": 0,
            }
            save_data(data)
            msg = f"✅ Активирована подписка на {days} дней"
        elif action == "reset_free":
            user = data.get(device_id, {"free_left": FREE_TRIES})
            user["free_left"] = FREE_TRIES
            data[device_id] = user
            save_data(data)
            msg = "🔁 Бесплатные попытки сброшены"
        elif action == "delete":
            if device_id in data:
                del data[device_id]
                save_data(data)
            msg = "🗑️ Удалено"
        else:
            msg = "Неизвестное действие"

        return f"<h3>{msg}</h3><a href='/admin'>← Назад</a>"

    # GET
    return render_template("admin.html", devices=sorted(load_data().items()))

@app.route("/")
def root():
    return jsonify({"status": "ok", "message": "CamFinder Server active"})

# ============ START ============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
