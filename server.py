# -*- coding: utf-8 -*-
import os
import json
import threading
import uuid
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, Any

from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, make_response, abort
)
from flask_cors import CORS

# CONFIG
APP_NAME = "CamFinder API Server"

DB_FILE = os.environ.get("DEVICES_DB", "devices.json")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Ledevi3656610208")
SECRET_KEY = os.environ.get("SECRET_KEY", "camfinder-secret-" + uuid.uuid4().hex)

IS_HTTPS = os.environ.get("FORCE_HTTPS", "0") == "1"

ADMIN_HASH = hashlib.sha256((ADMIN_PASSWORD + SECRET_KEY).encode()).hexdigest()

INITIAL_FREE = 3

DEFAULT_CONFIG = {
    "prices": {
        "3 дня": {"days": 3, "usd": 3, "rub": "50 руб.", "desc": "3 дня"},
        "7 дней": {"days": 7, "usd": 6, "rub": "100 руб.", "desc": "7 дней"},
        "30 дней": {"days": 30, "usd": 10, "rub": "300 руб.", "desc": "30 дней"},
    },
    "wallets": {
        "BTC": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        "ETH": "0x71C7656EC7ab88b098defB751B7401B5f6d8976F",
        "USDT": "THXrLBqa1QE1ZNFh2p48bWfQKYEDnTSYwT",
    }
}

# FLASK
app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY

CORS(app, resources={r"/api/*": {"origins": "*"}})

_db_lock = threading.Lock()
_config_lock = threading.Lock()

# AUTH (COOKIE)
def check_auth_cookie() -> bool:
    return request.cookies.get("admin_auth") == ADMIN_HASH

def require_admin_cookie():
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not check_auth_cookie():
                abort(401, "Unauthorized")
            return f(*args, **kwargs)
        return wrapped
    return decorator

def create_auth_response(redirect_url):
    resp = make_response(redirect(redirect_url))
    resp.set_cookie(
        "admin_auth",
        ADMIN_HASH,
        max_age=24 * 60 * 60,
        httponly=True,
        secure=IS_HTTPS,
        samesite="Lax",
        path="/",
    )
    return resp

def logout_response():
    resp = make_response(redirect(url_for("admin_page")))
    resp.set_cookie("admin_auth", "", expires=0, path="/")
    return resp

# UTILS
def now_utc():
    return datetime.utcnow()

def to_iso(dt):
    return dt.replace(microsecond=0).isoformat() if dt else None

def from_iso(s):
    return datetime.fromisoformat(s) if s else None

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_FILE):
        return {"devices": {}}
    with _db_lock:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"devices": {}}

def save_db(data: Dict[str, Any]):
    with _db_lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_FILE)

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    with _config_lock:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]):
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

def ensure_device(data: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    devices = data.setdefault("devices", {})
    if device_id not in devices:
        devices[device_id] = {
            "device_id": device_id,
            "created_at": to_iso(now_utc()),
            "last_seen": to_iso(now_utc()),
            "free_left": INITIAL_FREE,
            "sub_active": False,
            "sub_expires_at": None,
            "dev_mode": False,
            "tx_history": [],
            "last_tx": None,
            "last_comment": None,
            "selected_plan": None,
            "last_plan_days": None,
            "last_plan_price": None,
        }
    else:
        devices[device_id]["last_seen"] = to_iso(now_utc())
    return devices[device_id]

def recalc_subscription_state(dev: Dict[str, Any]):
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
    active = dev.get("sub_active") or dev.get("dev_mode")
    free_left = int(dev.get("free_left", 0))
    locked = not active and free_left <= 0
    return {
        "device_id": dev["device_id"],
        "active": active,
        "expires_at": dev["sub_expires_at"],
        "free_left": free_left,
        "locked": locked,
        "dev_mode": bool(dev.get("dev_mode")),
        "last_tx": dev.get("last_tx"),
        "last_comment": dev.get("last_comment"),
        "selected_plan": dev.get("selected_plan"),
        "is_premium": active,
        "sub_until": dev.get("sub_expires_at"),
    }

# INDEX + DASHBOARD STATS
@app.route("/")
def index_page():
    data = load_db()
    devices = list(data.get("devices", {}).values())

    total_devices = len(devices)
    active_subs = 0
    dev_mode = 0
    total_tx = 0

    for d in devices:
        recalc_subscription_state(d)
        if d.get("dev_mode"):
            dev_mode += 1
        if d.get("sub_active") or d.get("dev_mode"):
            active_subs += 1
        total_tx += len(d.get("tx_history", []))

    stats = {
        "total_devices": total_devices,
        "active_subscriptions": active_subs,
        "dev_mode": dev_mode,
        "total_tx": total_tx,
        "timestamp": now_utc().strftime("%Y-%m-%d %H:%M"),
    }

    return render_template("index.html", app_name=APP_NAME, stats=stats)

# ADMIN
@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    is_auth = check_auth_cookie()

    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            return create_auth_response(url_for("admin_dashboard"))
        return render_template(
            "admin.html",
            app_name=APP_NAME,
            error="Неверный пароль",
            devices=[],
            is_authenticated=False,
        )

    if is_auth:
        return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin.html",
        app_name=APP_NAME,
        devices=[],
        is_authenticated=False,
    )

@app.route("/admin/dashboard")
@require_admin_cookie()
def admin_dashboard():
    data = load_db()
    devices = list(data.get("devices", {}).values())

    devices.sort(
        key=lambda d: (
            0 if (d.get("dev_mode") or d.get("sub_active")) else 1,
            d.get("last_seen", ""),
        ), reverse=True  # Новые сверху
    )

    return render_template(
        "admin.html",
        app_name=APP_NAME,
        devices=devices,
        is_authenticated=True,
    )

@app.route("/admin/config", methods=["GET", "POST"])
@require_admin_cookie()
def admin_config():
    config = load_config()

    if request.method == "POST":
        prices = {}
        wallets = {}

        for k in request.form:
            if k.startswith("price_key_"):
                i = k.split("_")[-1]
                key = request.form.get(f"price_key_{i}")
                usd = request.form.get(f"price_value_{i}")
                rub = request.form.get(f"price_rub_{i}")
                days = request.form.get(f"price_days_{i}")
                desc = request.form.get(f"price_desc_{i}")
                if key and usd and days:
                    try:
                        prices[key] = {
                            "usd": float(usd),
                            "rub": rub,
                            "days": int(days),
                            "desc": desc,
                        }
                    except ValueError:
                        pass  # Игнор invalid

            if k.startswith("wallet_name_"):
                i = k.split("_")[-1]
                name = request.form.get(f"wallet_name_{i}")
                addr = request.form.get(f"wallet_addr_{i}")
                if name and addr:
                    wallets[name] = addr

        config["prices"] = prices
        config["wallets"] = wallets
        save_config(config)
        return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin_config.html",
        app_name=APP_NAME,
        config=config,
    )

@app.route("/admin/action", methods=["POST"])
@require_admin_cookie()
def admin_action():
    device_id = request.form.get("device_id")
    action = request.form.get("action")

    data = load_db()
    dev = data.get("devices", {}).get(device_id)
    if not dev:
        return redirect(url_for("admin_dashboard"))

    if action == "grant7":
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=7))
    elif action == "grant30":
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=30))
    elif action == "toggle_dev":
        dev["dev_mode"] = not dev.get("dev_mode", False)
    elif action == "delete":
        del data["devices"][device_id]

    save_db(data)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/logout")
def admin_logout():
    return logout_response()

# API
@app.route("/api/register_device", methods=["POST"])
def api_register_device():
    payload = request.get_json(force=True)
    device_id = payload.get("device_id")
    if not device_id:
        return jsonify(ok=False, error="device_id required"), 400

    data = load_db()
    dev = ensure_device(data, device_id)
    save_db(data)
    return jsonify(ok=True, device=snapshot(dev))

@app.route("/api/device_status")
def api_device_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify(ok=False, error="device_id required"), 400

    data = load_db()
    dev = ensure_device(data, device_id)
    save_db(data)
    return jsonify(snapshot(dev))

@app.route("/api/update_free_count", methods=["POST"])
def api_update_free_count():
    payload = request.get_json(force=True)
    device_id = payload.get("device_id")
    consumed = int(payload.get("consumed", 1))

    data = load_db()
    dev = ensure_device(data, device_id)
    recalc_subscription_state(dev)

    if not dev.get("sub_active") and not dev.get("dev_mode"):
        dev["free_left"] = max(0, dev.get("free_left", 0) - consumed)

    save_db(data)
    return jsonify(ok=True, free_left=dev["free_left"])

@app.route("/api/verify_payment", methods=["POST"])
def api_verify_payment():
    payload = request.get_json(force=True)
    device_id = payload.get("device_id")
    tx = payload.get("tx")
    plan = payload.get("plan")
    if not device_id or not tx or not plan:
        return jsonify(ok=False, error="Missing fields"), 400

    data = load_db()
    dev = ensure_device(data, device_id)

    # Валидация плана (из config)
    cfg = load_config()
    if plan not in cfg["prices"]:
        return jsonify(ok=False, error="Invalid plan"), 400

    plan_info = cfg["prices"][plan]

    rec = {
        "tx": tx,
        "comment": payload.get("comment"),
        "plan": plan,
        "plan_days": plan_info["days"],
        "plan_price": plan_info["usd"],  # Используем USD для internal
        "at": to_iso(now_utc()),
        "status": "pending",
    }

    dev["tx_history"].append(rec)
    dev["last_tx"] = rec["tx"]
    dev["last_comment"] = rec["comment"]
    dev["selected_plan"] = rec["plan"]
    dev["last_plan_days"] = rec["plan_days"]
    dev["last_plan_price"] = rec["plan_price"]

    save_db(data)
    return jsonify(ok=True, device=snapshot(dev))

@app.route("/api/config")
def api_get_config():
    cfg = load_config()
    return jsonify(ok=True, prices=cfg["prices"], wallets=cfg["wallets"])

# RUN
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
