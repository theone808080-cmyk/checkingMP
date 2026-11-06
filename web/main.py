"""Read-only local dashboard for collected materials and reviewed signals."""

from __future__ import annotations

import html
import base64
import binascii
import hmac
import json
import os
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
from web import telegram_auth


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
    def __getattr__(self, name):
        # BaseHTTPRequestHandler otherwise returns an unauthenticated 501 for
        # unrecognized methods, before any of our route checks run.
        if name.startswith("do_"):
            return self.method_not_allowed
        raise AttributeError(name)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        user = os.environ.get("WEB_BASIC_USER", "")
        password = os.environ.get("WEB_BASIC_PASSWORD", "")
        if not user or not password or not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except (ValueError, binascii.Error):
            return False
        return hmac.compare_digest(supplied, (user + ":" + password).encode())

    def reject_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Radar", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def session_authorized(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("radar_session")
            return bool(value and telegram_auth.valid_session(value.value, os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")))
        except CookieError:
            return False

    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=lambda x: x.isoformat() if isinstance(x, (date, datetime)) else str(x)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        if parsed.path == "/miniapp":
            body = Path(__file__).with_name("miniapp.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/api/feed", "/api/sources"):
            if not (self.authorized() or self.session_authorized()):
                self.reject_auth()
                return
            try:
                params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
                allowed = {"period", "platform", "cursor", "limit"} if parsed.path == "/api/feed" else set()
                if any(key not in allowed or len(values) != 1 for key, values in params.items()):
                    raise ValueError("invalid filters")
                with db.connect() as conn:
                    if parsed.path == "/api/feed":
                        limit = int(params.get("limit", ["20"])[0])
                        result = db.feed_materials(conn, params.get("period", ["all"])[0], params.get("platform", ["all"])[0], params.get("cursor", [None])[0], limit)
                    else:
                        result = db.feed_sources(conn)
                self.json_response(200, result)
            except (ValueError, TypeError):
                self.json_response(400, {"error": "invalid request"})
            except Exception:
                self.json_response(500, {"error": "service unavailable"})
            return
        if not self.authorized():
            self.reject_auth()
            return
        if parsed.path != "/":
            self.json_response(404, {"error": "not found"})
            return
        allowed = {"platform", "severity", "verification"}
        filters = {name: values[0] for name, values in parse_qs(parsed.query).items() if name in allowed}
        try:
            with db.connect() as conn:
                body = render(conn, filters).encode("utf-8")
            self.send_response(200)
        except Exception:
            self.send_response(500)
            body = "Ошибка страницы".encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/miniapp/session":
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid body size")
                payload = json.loads(self.rfile.read(int(lengths[0])).decode("utf-8"))
                token = os.environ.get("TELEGRAM_TOKEN", "")
                owner = os.environ.get("TELEGRAM_CHAT_ID", "")
                if not isinstance(payload, dict) or set(payload) != {"initData"} or not telegram_auth.validate_init_data(payload["initData"], token, owner):
                    self.json_response(401, {"error": "unauthorized"})
                    return
                value = telegram_auth.new_session(token, owner)
                self.send_response(200)
                self.send_header("Set-Cookie", f"radar_session={value}; Path=/; Max-Age=1800; HttpOnly; Secure; SameSite=Strict")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (ValueError, UnicodeError, TypeError):
                self.json_response(400, {"error": "invalid request"})
            return
        if not self.authorized():
            self.reject_auth()
            return
        self.json_response(404, {"error": "not found"})

    def method_not_allowed(self):
        if not self.authorized():
            self.reject_auth()
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
