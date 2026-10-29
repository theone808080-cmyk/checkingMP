"""Read-only local dashboard for collected materials and reviewed signals."""

from __future__ import annotations

import html
import base64
import binascii
import hmac
import hashlib
import json
import os
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
from worker import main as worker


def batch_signature(ids):
    message = json.dumps(ids, separators=(",", ":")).encode()
    return hmac.new(os.environ["WORKER_TOKEN"].encode(), message, hashlib.sha256).hexdigest()


def render(conn, filters: dict[str, str]) -> str:
    counts = db.health(conn)
    unhealthy = conn.execute("SELECT count(*) FROM sources WHERE last_error IS NOT NULL").fetchone()[0]
    clauses, params = [], []
    for field in ("platform", "severity", "verification"):
        if filters.get(field):
            clauses.append(f"e.{field} = %s")
            params.append(filters[field])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    events = conn.execute(
        f"""SELECT e.platform, e.topic, e.severity, e.summary, e.recommended_action, e.verification,
                   string_agg(DISTINCT m.url, E'\\n')
            FROM events e JOIN event_materials em ON em.event_id=e.id JOIN materials m ON m.id=em.material_id
            {where}
            GROUP BY e.id ORDER BY CASE e.severity WHEN 'critical' THEN 1 WHEN 'important' THEN 2 ELSE 3 END, e.updated_at DESC""",
        params,
    ).fetchall()
    event_rows = []
    for platform, topic, severity, summary, action, verification, urls in events:
        links = "<br>".join(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">источник</a>'
            for url in urls.split("\n")
        )
        event_rows.append(
            f'<tr><td class="{severity}">{html.escape(severity)}</td><td><b>{html.escape(platform)}</b><br>'
            f'<span class="tag">{html.escape(topic)}</span><br><span class="tag">{html.escape(verification)}</span></td>'
            f'<td>{html.escape(summary)}</td><td>{html.escape(action)}</td><td>{links}</td></tr>'
        )
    if not event_rows:
        event_rows.append('<tr><td colspan="5">Сигналов по выбранному фильтру пока нет</td></tr>')
    source_rows = []
    for name, trust, checked, error in conn.execute(
        "SELECT name, trust, last_checked_at, last_error FROM sources ORDER BY trust, name"
    ).fetchall():
        stamp = checked.strftime("%d.%m %H:%M") if checked else "ещё не проверялся"
        source_rows.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(trust)}</td><td>{stamp}</td><td>{html.escape(error or '')}</td></tr>"
        )
    platforms = [row[0] for row in conn.execute("SELECT DISTINCT platform FROM events ORDER BY platform").fetchall()]
    def options(values, selected):
        return "".join(
            f'<option value="{html.escape(value)}" {"selected" if value == selected else ""}>{html.escape(value)}</option>'
            for value in values
        )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Радар маркетплейсов</title><style>
body{{font-family:system-ui,sans-serif;max-width:1180px;margin:auto;padding:24px;background:#f6f5f1;color:#191919}}h1{{margin-bottom:4px}}.muted{{color:#666}}.stats,.filters{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}.stat,.filter{{background:#fff;padding:10px;border:1px solid #ddd}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}}th{{background:#ecebe5}}a{{color:#1a4a6b}}.critical{{color:#9d1717;font-weight:700}}.important{{color:#875900;font-weight:700}}.info{{color:#34643c}}.tag{{font-size:12px;background:#e4ecef;padding:2px 5px}}form{{display:flex;gap:8px;flex-wrap:wrap}}select,button{{padding:6px}}
</style></head><body><h1>Радар маркетплейсов и «Честного Знака»</h1><p class="muted">Ручной пилот: сигналы созданы после проверки агентом. Не является юридическим заключением.</p>
<div class="stats"><div class="stat">Источники: <b>{counts['sources']}</b></div><div class="stat">Материалы: <b>{counts['materials']}</b></div><div class="stat">Сигналы: <b>{counts['events']}</b></div><div class="stat">Ожидают разбора: <b>{counts['pending']}</b></div><div class="stat">Недоступно: <b>{unhealthy}</b></div></div>
<div class="filters"><form method="get"><label>Площадка <select name="platform"><option value="">Все</option>{options(platforms, filters.get('platform', ''))}</select></label><label>Важность <select name="severity"><option value="">Все</option>{options(('critical','important','info'), filters.get('severity', ''))}</select></label><label>Проверка <select name="verification"><option value="">Все</option>{options(('confirmed','needs_check'), filters.get('verification', ''))}</select></label><button>Фильтровать</button></form></div>
<table><tr><th>Важность</th><th>Площадка и тема</th><th>Что изменилось</th><th>Что сделать</th><th>Источники</th></tr>{''.join(event_rows)}</table>
<h2>Статус источников</h2><table><tr><th>Источник</th><th>Доверие</th><th>Последняя проверка</th><th>Ошибка</th></tr>{''.join(source_rows)}</table></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def authorized(self, worker_route=False):
        header = self.headers.get("Authorization", "")
        if worker_route:
            token = os.environ.get("WORKER_TOKEN", "")
            return bool(token) and hmac.compare_digest(header.encode(), ("Bearer " + token).encode())
        user = os.environ.get("WEB_BASIC_USER", "")
        password = os.environ.get("WEB_BASIC_PASSWORD", "")
        if not user or not password or not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except (ValueError, binascii.Error):
            return False
        return hmac.compare_digest(supplied, (user + ":" + password).encode())

    def reject_auth(self, worker_route=False):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer' if worker_route else 'Basic realm="Radar", charset="UTF-8"')
        self.end_headers()

    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=lambda x: x.isoformat() if isinstance(x, (date, datetime)) else str(x)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            try:
                with db.connect() as conn:
                    conn.execute("SELECT 1")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"database unavailable")
            return
        parsed = urlparse(self.path)
        worker_route = parsed.path.startswith("/api/worker/")
        if not self.authorized(worker_route):
            self.reject_auth(worker_route)
            return
        if worker_route:
            if parsed.path != "/api/worker/pending":
                self.json_response(404, {"error": "not found"})
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"limit"} or len(query.get("limit", ["10"])) != 1:
                    raise ValueError("Invalid limit")
                limit = int(query.get("limit", ["10"])[0])
                if not 1 <= limit <= 50:
                    raise ValueError("Invalid limit")
            except ValueError:
                self.json_response(400, {"error": "limit must be between 1 and 50"})
                return
            try:
                with db.connect() as conn:
                    batch = db.pending_materials(conn, limit)
                self.json_response(200, {"materials": batch, "batch_token": batch_signature([item["material_id"] for item in batch])})
            except Exception:
                self.json_response(503, {"error": "database unavailable"})
            return
        allowed = {"platform", "severity", "verification"}
        filters = {name: values[0] for name, values in parse_qs(parsed.query).items() if name in allowed}
        try:
            with db.connect() as conn:
                body = render(conn, filters).encode("utf-8")
            self.send_response(200)
        except Exception as error:
            self.send_response(500)
            body = f"Ошибка страницы: {html.escape(str(error))}".encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        worker_route = parsed.path.startswith("/api/worker/")
        if not self.authorized(worker_route):
            self.reject_auth(worker_route)
            return
        if parsed.path != "/api/worker/results":
            self.json_response(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= 1_000_000:
                raise ValueError("invalid length")
            payload = worker.strict_loads(self.rfile.read(length).decode("utf-8"))
            ids = payload.get("batch_material_ids") if isinstance(payload, dict) else None
            if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(type(i) is not int or i <= 0 for i in ids) or len(ids) != len(set(ids)):
                raise ValueError("invalid batch")
            signature = payload.get("batch_token")
            if not isinstance(signature, str) or not hmac.compare_digest(signature.encode(), batch_signature(ids).encode()):
                raise ValueError("invalid batch token")
            with db.connect() as conn:
                with conn.transaction():
                    rows = conn.execute("SELECT id FROM materials WHERE id = ANY(%s) AND status = 'new' ORDER BY id FOR UPDATE", (ids,)).fetchall()
                    if {row[0] for row in rows} != set(ids):
                        raise ValueError("batch no longer pending")
                    worker.validate(payload, [{"material_id": row[0]} for row in rows])
                    created, linked, skipped = db.import_signals(conn, payload["signals"], payload["skipped_material_ids"])
            self.json_response(200, {"created": created, "linked": linked, "skipped": skipped})
        except (ValueError, UnicodeError, TypeError):
            self.json_response(400, {"error": "invalid or stale batch or payload"})
        except Exception:
            self.json_response(503, {"error": "processing unavailable"})

    def method_not_allowed(self):
        worker_route = urlparse(self.path).path.startswith("/api/worker/")
        if not self.authorized(worker_route):
            self.reject_auth(worker_route)
            return
        self.send_response(405)
        self.end_headers()

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = method_not_allowed

    def log_message(self, _format, *_args):
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    with db.connect() as conn:
        db.init_schema(conn)
        db.sync_sources(conn)
    print(f"Откройте http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
