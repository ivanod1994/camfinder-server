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
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Ledevi3656610208")
SECRET_KEY = os.environ.get("SECRET_KEY", "camfinder-secret-" + uuid.uuid4().hex)

# Сколько бесплатных поисков давать изначально
INITIAL_FREE = 3

# Конфигурация по умолчанию
DEFAULT_CONFIG = {
    "prices": {
        "3 дня": {"days": 3, "usd": 3, "rub": "50 руб.", "desc": "3 дня"},
        "7 дней": {"days": 7, "usd": 6, "rub": "100 руб.", "desc": "7 дней"},
        "30 дней": {"days": 30, "usd": 10, "rub": "300 руб.", "desc": "30 дней"}
    },
    "wallets": {
        "BTC": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        "ETH": "0x71C7656EC7ab88b098defB751B7401B5f6d8976F",
        "USDT": "THXrLBqa1QE1ZNFh2p48bWfQKYEDnTSYwT"
    }
}

app = Flask(__name__, template_folder="templates", static_folder=None)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
CORS(app, resources={r"/api/*": {"origins": "*"}})

_db_lock = threading.Lock()
_config_lock = threading.Lock()

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

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    with _config_lock:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Проверяем наличие обязательных полей
            if "prices" not in data:
                data["prices"] = DEFAULT_CONFIG["prices"].copy()
            if "wallets" not in data:
                data["wallets"] = DEFAULT_CONFIG["wallets"].copy()
            return data
        except Exception:
            return DEFAULT_CONFIG.copy()

def save_config(data: Dict[str, Any]) -> None:
    # Валидируем конфиг перед сохранением
    if "prices" not in data:
        data["prices"] = DEFAULT_CONFIG["prices"].copy()
    if "wallets" not in data:
        data["wallets"] = DEFAULT_CONFIG["wallets"].copy()
    
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

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
            "tx_history": [],            # [{tx, comment, at, plan, plan_days, plan_price}]
            "last_tx": None,
            "last_comment": None,
            "selected_plan": None,       # Последний выбранный план
            "last_plan_days": None,      # Последнее количество дней
            "last_plan_price": None,     # Последняя цена
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
        "selected_plan": dev.get("selected_plan"),
        "is_premium": active,
        "sub_until": dev.get("sub_expires_at"),
    }

# ------------------------------------------------------------------------------
# HTML: главная + админ
# ------------------------------------------------------------------------------
def require_admin():
    if not session.get("is_admin"):
        abort(401)

@app.route("/")
def index_page():
    data = load_db()
    devices = list(data.get("devices", {}).values())
    
    # Собираем статистику
    total_devices = len(devices)
    
    # Считаем активные подписки
    active_subscriptions = 0
    dev_mode = 0
    total_tx = 0
    
    for d in devices:
        recalc_subscription_state(d)
        if d.get("dev_mode"):
            dev_mode += 1
        if d.get("sub_active") or d.get("dev_mode"):
            active_subscriptions += 1
        total_tx += len(d.get("tx_history", []))
    
    stats = {
        "total_devices": total_devices,
        "active_subscriptions": active_subscriptions,
        "dev_mode": dev_mode,
        "total_tx": total_tx,
        "timestamp": now_utc().strftime("%Y-%m-%d %H:%M")
    }
    
    return render_template("index.html", app_name=APP_NAME, stats=stats)

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            session.permanent = True  # Ключевая строка - сохраняем сессию
            return redirect(url_for("admin_dashboard"))
        return render_template("admin.html", app_name=APP_NAME, error="Неверный пароль", devices=[])
    
    # Если GET запрос и уже авторизован - перенаправляем на дашборд
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    
    # Показываем форму входа
    return render_template("admin.html", app_name=APP_NAME, devices=[])

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
    
    # Загружаем конфиг для отображения цен
    config = load_config()
    prices = config.get("prices", {})
    
    return render_template("admin.html", app_name=APP_NAME, devices=devices, prices=prices)

@app.route("/admin/config", methods=["GET", "POST"])
def admin_config():
    require_admin()
    config = load_config()
    
    if request.method == "POST":
        try:
            # Обработка тарифов
            prices = {}
            for key in request.form:
                if key.startswith("price_key_"):
                    idx = key.split('_')[-1]
                    price_key = request.form.get(f"price_key_{idx}", "").strip()
                    price_value = request.form.get(f"price_value_{idx}", "").strip()
                    price_desc = request.form.get(f"price_desc_{idx}", "").strip()
                    price_rub = request.form.get(f"price_rub_{idx}", "").strip()
                    price_days = request.form.get(f"price_days_{idx}", "").strip()
                    
                    if price_key and price_value and price_desc:
                        try:
                            prices[price_key] = {
                                "usd": float(price_value),
                                "desc": price_desc,
                                "rub": price_rub or f"{float(price_value) * 15:.0f} руб.",
                                "days": int(price_days) if price_days.isdigit() else 30
                            }
                        except ValueError:
                            continue
            
            # Обработка кошельков
            wallets = {}
            for key in request.form:
                if key.startswith("wallet_name_"):
                    idx = key.split('_')[-1]
                    wallet_name = request.form.get(f"wallet_name_{idx}", "").strip()
                    wallet_addr = request.form.get(f"wallet_addr_{idx}", "").strip()
                    
                    if wallet_name and wallet_addr:
                        wallets[wallet_name] = wallet_addr
            
            # Обновляем конфиг
            config["prices"] = prices
            config["wallets"] = wallets
            save_config(config)
            return redirect(url_for("admin_dashboard"))
        except Exception as e:
            return render_template(
                "admin_config.html", 
                app_name=APP_NAME, 
                config=config,
                error=f"Ошибка сохранения: {str(e)}"
            )
    
    return render_template("admin_config.html", app_name=APP_NAME, config=config)

@app.route("/admin/action", methods=["POST"])
def admin_action():
    require_admin()
    act = request.form.get("action")
    device_id = request.form.get("device_id")
    plan_days = request.form.get("plan_days")

    data = load_db()
    dev = ensure_device(data, device_id)

    if act == "grant_custom":
        # Выдать подписку на указанное количество дней
        if plan_days and plan_days.isdigit():
            days = int(plan_days)
            dev["sub_active"] = True
            dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=days))
    elif act == "grant30":
        # Выдать подписку на 30 дней
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=30))
    elif act == "grant7":
        # Выдать подписку на 7 дней
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=7))
    elif act == "grant3":
        # Выдать подписку на 3 дня
        dev["sub_active"] = True
        dev["sub_expires_at"] = to_iso(now_utc() + timedelta(days=3))
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
        dev["selected_plan"] = None
        dev["last_plan_days"] = None
        dev["last_plan_price"] = None
        dev["tx_history"] = []

    save_db(data)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_page"))

# ------------------------------------------------------------------------------
# API: регистрация, статус, попытки, платежи, подписки
# И конфигурации
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
    Клиент отправляет TX/комментарий после оплаты. Сохраняем информацию о выбранном плане.
    """
    payload = request.get_json(force=True, silent=True) or {}
    device_id = (payload.get("device_id") or "").strip()
    tx = (payload.get("tx") or "").strip()
    comment = (payload.get("comment") or "").strip()
    plan = (payload.get("plan") or "").strip()
    plan_days = payload.get("plan_days")
    plan_price = (payload.get("plan_price") or "").strip()

    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400
    if not tx and not comment:
        return jsonify({"ok": False, "error": "tx or comment required"}), 400

    data = load_db()
    dev = ensure_device(data, device_id)

    rec = {
        "tx": tx or None,
        "comment": comment or None,
        "plan": plan or None,
        "plan_days": plan_days,
        "plan_price": plan_price or None,
        "at": to_iso(now_utc()),
        "status": "pending",  # ожидает подтверждения
    }
    dev["tx_history"] = list(dev.get("tx_history", []))
    dev["tx_history"].append(rec)
    dev["last_tx"] = rec["tx"]
    dev["last_comment"] = rec["comment"]
    dev["selected_plan"] = rec["plan"]
    dev["last_plan_days"] = rec["plan_days"]
    dev["last_plan_price"] = rec["plan_price"]

    save_db(data)
    return jsonify({"ok": True, "message": "TX received", "device": snapshot(dev)})

@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Отдаёт конфигурацию: кошельки, цены, описание тарифов."""
    config = load_config()
    return jsonify({
        "ok": True,
        "prices": config.get("prices", {}),
        "wallets": config.get("wallets", {}),
    })

# Новый эндпоинт для получения полной информации об устройстве
@app.route("/api/device_full_info", methods=["GET"])
def api_device_full_info():
    """Возвращает полную информацию об устройстве для админки."""
    device_id = (request.args.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    data = load_db()
    if device_id not in data.get("devices", {}):
        return jsonify({"ok": False, "error": "Device not found"}), 404
    
    dev = data["devices"][device_id]
    return jsonify({"ok": True, "device": dev})

# ------------------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------------------
# Для Railway / Gunicorn точка входа: app
if __name__ == "__main__":
    # локально: python server.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
