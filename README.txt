# CamFinder Server (Railway-ready)

**Админ-ключ по умолчанию:** `MASTER112` (можно переопределить переменной окружения `ADMIN_KEY`).

## Локальный запуск
```bash
pip install -r requirements.txt
python server.py
# открой http://127.0.0.1:8000/admin
```

## API
- `GET /health`
- `GET /api/v1/subscriptions/status?device_id=android-XXXX`
- `POST /api/v1/subscriptions/submit` JSON: `{device_id, tx, comment, amount?, currency?}`
- `GET /api/v1/admin/pending`  (Header: `X-Admin-Key`)
- `POST /api/v1/admin/activate` JSON: `{payment_id?, device_id, months=1, dev=false}` (Header: `X-Admin-Key`)
- `POST /api/v1/admin/reject`   JSON: `{payment_id}` (Header: `X-Admin-Key`)
- `POST /api/v1/admin/revoke`   JSON: `{device_id}` (Header: `X-Admin-Key`)

База — `database.db` (SQLite) рядом с сервером.
