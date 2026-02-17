# -*- coding: utf-8 -*-
"""
CamFinder API Client

Клиент для взаимодействия с CamFinder API Server.

Использование:
    from client import CamFinderClient

    client = CamFinderClient("http://localhost:8080")
    client.register_device("my-device-123")
    status = client.get_device_status("my-device-123")
    print(status)
"""

import requests
from typing import Optional, Dict, Any


class CamFinderClient:
    """Клиент для CamFinder API Server."""

    def __init__(self, base_url: str = "http://localhost:8080", timeout: int = 10):
        """
        Args:
            base_url: Базовый URL сервера (например, "http://localhost:8080").
            timeout: Таймаут запросов в секундах.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _post(self, path: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(self._url(path), json=json_data, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        resp = self.session.get(self._url(path), params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ── API методы ──────────────────────────────────────────────────────

    def register_device(self, device_id: str) -> Dict[str, Any]:
        """Регистрирует устройство на сервере.

        Args:
            device_id: Уникальный идентификатор устройства.

        Returns:
            {"ok": True, "device": {...}} при успехе.
        """
        return self._post("/api/register_device", {"device_id": device_id})

    def get_device_status(self, device_id: str) -> Dict[str, Any]:
        """Получает текущий статус устройства.

        Args:
            device_id: Уникальный идентификатор устройства.

        Returns:
            Словарь с полями: device_id, active, expires_at, free_left,
            locked, dev_mode, is_premium, sub_until и др.
        """
        return self._get("/api/device_status", {"device_id": device_id})

    def update_free_count(self, device_id: str, consumed: int = 1) -> Dict[str, Any]:
        """Списывает бесплатные попытки.

        Args:
            device_id: Уникальный идентификатор устройства.
            consumed: Количество попыток для списания (по умолчанию 1).

        Returns:
            {"ok": True, "free_left": <int>}
        """
        return self._post("/api/update_free_count", {
            "device_id": device_id,
            "consumed": consumed,
        })

    def verify_payment(
        self,
        device_id: str,
        tx: str,
        plan: str,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Отправляет платёж на верификацию.

        Args:
            device_id: Уникальный идентификатор устройства.
            tx: ID транзакции (хеш).
            plan: Ключ тарифа (например, "3 дня", "7 дней", "30 дней").
            comment: Необязательный комментарий к платежу.

        Returns:
            {"ok": True, "device": {...}} при успехе.
        """
        payload: Dict[str, Any] = {
            "device_id": device_id,
            "tx": tx,
            "plan": plan,
        }
        if comment is not None:
            payload["comment"] = comment
        return self._post("/api/verify_payment", payload)

    def get_config(self) -> Dict[str, Any]:
        """Получает конфигурацию сервера (тарифы и кошельки).

        Returns:
            {"ok": True, "prices": {...}, "wallets": {...}}
        """
        return self._get("/api/config")

    # ── Вспомогательные методы ──────────────────────────────────────────

    def get_plans(self) -> Dict[str, Any]:
        """Возвращает доступные тарифные планы."""
        cfg = self.get_config()
        return cfg.get("prices", {})

    def get_wallets(self) -> Dict[str, str]:
        """Возвращает кошельки для оплаты."""
        cfg = self.get_config()
        return cfg.get("wallets", {})

    def is_active(self, device_id: str) -> bool:
        """Проверяет, активна ли подписка устройства."""
        status = self.get_device_status(device_id)
        return status.get("active", False)

    def is_locked(self, device_id: str) -> bool:
        """Проверяет, заблокировано ли устройство (нет подписки и бесплатных попыток)."""
        status = self.get_device_status(device_id)
        return status.get("locked", False)

    def get_free_left(self, device_id: str) -> int:
        """Возвращает количество оставшихся бесплатных попыток."""
        status = self.get_device_status(device_id)
        return status.get("free_left", 0)


# ── CLI-интерфейс ──────────────────────────────────────────────────────────

def main():
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="CamFinder API Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Примеры:
  python client.py --url http://localhost:8080 register abc123
  python client.py status abc123
  python client.py config
  python client.py use-free abc123
  python client.py pay abc123 --tx 0xabc... --plan "7 дней"
""",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Базовый URL сервера (по умолчанию: http://localhost:8080)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p_reg = sub.add_parser("register", help="Зарегистрировать устройство")
    p_reg.add_argument("device_id", help="ID устройства")

    # status
    p_st = sub.add_parser("status", help="Получить статус устройства")
    p_st.add_argument("device_id", help="ID устройства")

    # use-free
    p_free = sub.add_parser("use-free", help="Списать бесплатную попытку")
    p_free.add_argument("device_id", help="ID устройства")
    p_free.add_argument("--count", type=int, default=1, help="Сколько попыток списать")

    # pay
    p_pay = sub.add_parser("pay", help="Отправить платёж на верификацию")
    p_pay.add_argument("device_id", help="ID устройства")
    p_pay.add_argument("--tx", required=True, help="ID транзакции")
    p_pay.add_argument("--plan", required=True, help='Тарифный план (напр. "7 дней")')
    p_pay.add_argument("--comment", help="Комментарий")

    # config
    sub.add_parser("config", help="Показать конфигурацию сервера")

    # plans
    sub.add_parser("plans", help="Показать доступные тарифы")

    # wallets
    sub.add_parser("wallets", help="Показать кошельки для оплаты")

    args = parser.parse_args()
    client = CamFinderClient(args.url)

    try:
        if args.command == "register":
            result = client.register_device(args.device_id)
        elif args.command == "status":
            result = client.get_device_status(args.device_id)
        elif args.command == "use-free":
            result = client.update_free_count(args.device_id, args.count)
        elif args.command == "pay":
            result = client.verify_payment(
                args.device_id, args.tx, args.plan, args.comment,
            )
        elif args.command == "config":
            result = client.get_config()
        elif args.command == "plans":
            result = client.get_plans()
        elif args.command == "wallets":
            result = client.get_wallets()
        else:
            parser.print_help()
            return

        print(_json.dumps(result, ensure_ascii=False, indent=2))

    except requests.ConnectionError:
        print(f"Ошибка: не удалось подключиться к {args.url}")
        raise SystemExit(1)
    except requests.HTTPError as e:
        print(f"Ошибка HTTP: {e.response.status_code} — {e.response.text}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
