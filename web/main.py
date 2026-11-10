"""Read-only local dashboard for collected materials and reviewed signals."""

from __future__ import annotations

import html
import base64
import binascii
import hmac
import json
import os
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
from source_discovery.probe import probe_source
from web import telegram_auth


def render(conn, filters: dict[str, str]) -> str:
    counts = db.health(conn)
    backup = db.latest_backup_status(conn)
    backup_stamp = (backup["last_success_at"].strftime("%d.%m.%Y %H:%M UTC")
                    if backup.get("last_success_at") else "успешных копий пока нет")
    restore_stamp = (backup["last_restore_checked_at"].strftime("%d.%m.%Y %H:%M UTC")
                     if backup.get("last_restore_checked_at") else "проверка восстановления ещё не выполнялась")
    restore_label = {"success": "пройдена", "failed": "ошибка"}.get(
        backup.get("last_restore_status"), "ожидает проверки")
    unhealthy = conn.execute("SELECT count(*) FROM sources WHERE last_error IS NOT NULL").fetchone()[0]
    clauses, params = [], []
    for field in ("platform", "importance", "verification"):
        if filters.get(field):
            value = filters[field]
            if field == "importance":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError("invalid importance") from None
                if not 1 <= value <= 5:
                    raise ValueError("invalid importance")
            clauses.append(f"e.{field} = %s")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    events = conn.execute(
        f"""SELECT e.id, e.platform, e.topic, e.importance, e.summary, e.recommended_action, e.verification,
                   string_agg(DISTINCT m.url, E'\\n')
            FROM events e JOIN event_materials em ON em.event_id=e.id JOIN materials m ON m.id=em.material_id
            {where}
            GROUP BY e.id ORDER BY e.importance DESC, e.updated_at DESC, e.id DESC""",
        params,
    ).fetchall()
    event_rows = []
    for finding_id, platform, topic, importance, summary, action, verification, urls in events:
        links = "<br>".join(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">источник</a>'
            for url in urls.split("\n")
        )
        event_rows.append(
            f'<tr><td><span class="importance importance-{importance}" aria-label="Важность {importance}">{importance}</span></td><td><b>{html.escape(platform)}</b><br>'
            f'<span class="tag">{html.escape(topic)}</span><br><span class="tag">{html.escape(verification)}</span></td>'
            f'<td>{html.escape(summary)}</td><td>{html.escape(action)}</td>'
            f'<td>{links}<br><a href="/api/findings/{int(finding_id)}/trace">путь обработки</a></td></tr>'
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
body{{font-family:system-ui,sans-serif;max-width:1180px;margin:auto;padding:24px;background:#f6f5f1;color:#191919}}h1{{margin-bottom:4px}}.muted{{color:#666}}.stats,.filters{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}.stat,.filter{{background:#fff;padding:10px;border:1px solid #ddd}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}}th{{background:#ecebe5}}a{{color:#1a4a6b}}.importance{{display:inline-flex;align-items:center;justify-content:center;width:2em;height:2em;border-radius:4px;color:#111;font-weight:700}}.importance-1{{background:#b9c1c7}}.importance-2{{background:#78b7e8}}.importance-3{{background:#f2d45c}}.importance-4{{background:#f49a4b}}.importance-5{{background:#ee6666}}.tag{{font-size:12px;background:#e4ecef;padding:2px 5px}}form{{display:flex;gap:8px;flex-wrap:wrap}}select,button{{padding:6px}}
</style></head><body><h1>Радар маркетплейсов и «Честного Знака»</h1><p class="muted">Ручной пилот: сигналы созданы после проверки агентом. Не является юридическим заключением.</p>
<div class="stats"><div class="stat">Источники: <b>{counts['sources']}</b></div><div class="stat">Материалы: <b>{counts['materials']}</b></div><div class="stat">Сигналы: <b>{counts['events']}</b></div><div class="stat">Ожидают разбора: <b>{counts['pending']}</b></div><div class="stat">Недоступно: <b>{unhealthy}</b></div></div>
<h2>Резервное копирование</h2><p>Последняя успешная копия: {html.escape(backup_stamp)}; размер: {int(backup.get('last_success_size_bytes') or 0)} байт. Проверка восстановления: {html.escape(restore_label)} ({html.escape(restore_stamp)}).</p>
<div class="filters"><form method="get"><label>Площадка <select name="platform"><option value="">Все</option>{options(platforms, filters.get('platform', ''))}</select></label><label>Важность <select name="importance"><option value="">Все</option>{options(('1','2','3','4','5'), filters.get('importance', ''))}</select></label><label>Проверка <select name="verification"><option value="">Все</option>{options(('confirmed','needs_check'), filters.get('verification', ''))}</select></label><button>Фильтровать</button></form></div>
<table><tr><th>Важность</th><th>Площадка и тема</th><th>Что изменилось</th><th>Что сделать</th><th>Источники</th></tr>{''.join(event_rows)}</table>
<h2>Статус источников</h2><table><tr><th>Источник</th><th>Доверие</th><th>Последняя проверка</th><th>Ошибка</th></tr>{''.join(source_rows)}</table></body></html>"""


def render_source_review(conn) -> str:
    candidates = db.list_source_candidates(conn, status="all", limit=500)
    last_run = conn.execute(
        """SELECT status, started_at, finished_at, items, note FROM runs
           WHERE service='source_discovery' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    pause = db.exa_pause_until(conn)
    cards = []
    for item in candidates:
        if item["status"] not in ("pending", "needs_setup"):
            continue
        examples = item.get("example_urls") or []
        links = " ".join(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">пример {index}</a>'
            for index, url in enumerate(examples, 1)
        ) or "Примеров пока нет"
        options = lambda values, selected: "".join(
            f'<option value="{html.escape(value, quote=True)}" {"selected" if value == selected else ""}>{html.escape(value)}</option>'
            for value in values
        )
        cards.append(f"""<article class="candidate" data-id="{int(item['id'])}">
<h2>{html.escape(item['name'])} <small>{html.escape(item['publisher_domain'])}</small></h2>
<p>Проверка: {html.escape(item['probe_status'])} {html.escape(item.get('probe_error') or '')}</p><p>{links}</p>
<form><label>Название<input name="name" maxlength="200" required value="{html.escape(item['name'], quote=True)}"></label>
<label>Адрес сбора<input name="collection_url" maxlength="2048" required value="{html.escape(item['collection_url'], quote=True)}"></label>
<label>Тип<select name="kind">{options(('rss','html','telegram'), item['kind'])}</select></label>
<label>Площадка<select name="category">{options(('ozon','wildberries','yandex','chestny_znak','industry'), item['category'])}</select></label>
<label>Доверие<select name="trust">{options(('official','regulator','industry'), item['trust'])}</select></label>
<label>Область<input name="scope" maxlength="300" required value="{html.escape(item['scope'], quote=True)}"></label>
<button name="action" value="approve">Проверить и одобрить</button><button name="action" value="reject">Отклонить</button>
<output aria-live="polite"></output></form></article>""")
    if not cards:
        cards.append("<p>Новых кандидатов нет.</p>")
    if last_run:
        provider_status = f"Последний поиск: {html.escape(last_run[0])}; найдено кандидатов: {int(last_run[3] or 0)}"
        if last_run[2]:
            provider_status += f"; завершён {html.escape(last_run[2].isoformat())}"
        try:
            details = json.loads(last_run[4] or "{}")
            providers = details.get("providers", {})
            labels = {"ok": "работает", "not_configured": "не настроен", "paused": "пауза до следующего месяца UTC"}
            provider_status += "<br>Google News RSS: " + html.escape(labels.get(providers.get("google_news"), providers.get("google_news", "неизвестно")))
            provider_status += "<br>Exa: " + html.escape(labels.get(providers.get("exa"), providers.get("exa", "неизвестно")))
        except (AttributeError, TypeError, ValueError):
            provider_status += "<br>Статус провайдеров недоступен."
    else:
        provider_status = "Поиск ещё не запускался."
    exa_status = f"Exa приостановлен до {html.escape(pause.isoformat())}." if pause else "Exa доступен только при заданном ключе и свободном лимите."
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Проверка источников</title><style>body{{font:16px system-ui;max-width:960px;margin:auto;padding:20px}}.candidate{{border:1px solid #bbb;border-radius:12px;padding:16px;margin:16px 0}}form{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{display:grid;gap:4px}}input,select,button{{font:inherit;padding:8px}}output{{grid-column:1/-1}}</style></head><body>
<h1>Кандидаты в источники</h1><p>{provider_status}</p><p>{exa_status}</p>{''.join(cards)}
<script>for(const form of document.querySelectorAll('.candidate form')){{form.addEventListener('submit',async event=>{{event.preventDefault();const button=event.submitter;const values=Object.fromEntries(new FormData(form).entries());const action=button.value;delete values.action;const output=form.querySelector('output');output.textContent='Сохраняем…';try{{const response=await fetch('/api/source-candidates/'+form.closest('.candidate').dataset.id,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action,fields:values}})}});const result=await response.json();output.textContent=response.ok?'Статус: '+result.status:'Не удалось выполнить действие: '+(result.error||result.status||response.status);if(response.ok&&result.status!=='needs_setup')form.closest('.candidate').remove();}}catch(_){{output.textContent='Не удалось связаться с сервером.';}}}});}}</script></body></html>"""


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
        if parsed.path == "/sources/review":
            if not self.authorized():
                self.reject_auth()
                return
            try:
                with db.connect() as conn:
                    body = render_source_review(conn).encode("utf-8")
            except Exception:
                self.json_response(500, {"error": "service unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ("/api/feed", "/api/findings", "/api/sources"):
            if not (self.authorized() or self.session_authorized()):
                self.reject_auth()
                return
            try:
                params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
                if parsed.path == "/api/feed":
                    allowed = {"period", "platform", "cursor", "limit"}
                elif parsed.path == "/api/findings":
                    allowed = {"period", "platform", "importance", "topic", "verification", "source_type", "cursor", "limit"}
                else:
                    allowed = set()
                if any(key not in allowed or len(values) != 1 for key, values in params.items()):
                    raise ValueError("invalid filters")
                with db.connect() as conn:
                    if parsed.path == "/api/feed":
                        limit = int(params.get("limit", ["20"])[0])
                        result = db.feed_materials(conn, params.get("period", ["all"])[0], params.get("platform", ["all"])[0], params.get("cursor", [None])[0], limit)
                    elif parsed.path == "/api/findings":
                        importance = int(params["importance"][0]) if "importance" in params else None
                        limit = int(params.get("limit", ["20"])[0])
                        result = db.feed_findings(
                            conn, params.get("period", ["all"])[0], params.get("platform", ["all"])[0], importance,
                            params.get("topic", [None])[0], params.get("verification", [None])[0],
                            params.get("source_type", [None])[0], params.get("cursor", [None])[0], limit,
                        )
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
        trace_match = re.fullmatch(r"/api/findings/([1-9][0-9]*)/trace", parsed.path)
        if trace_match:
            try:
                with db.connect() as conn:
                    result = db.finding_trace(conn, int(trace_match.group(1)))
                if result is None:
                    self.json_response(404, {"error": "not found"})
                else:
                    self.json_response(200, result)
            except Exception:
                self.json_response(500, {"error": "service unavailable"})
            return
        if parsed.path != "/":
            self.json_response(404, {"error": "not found"})
            return
        allowed = {"platform", "importance", "verification"}
        filters = {name: values[0] for name, values in parse_qs(parsed.query).items() if name in allowed}
        try:
            with db.connect() as conn:
                body = render(conn, filters).encode("utf-8")
            self.send_response(200)
        except ValueError:
            self.send_response(400)
            body = "Некорректный фильтр".encode("utf-8")
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
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/source-candidates/"):
            import re
            match = re.fullmatch(r"/api/source-candidates/([1-9][0-9]{0,17})", parsed.path)
            if not match or parsed.query or parsed.fragment:
                self.json_response(404, {"error": "not found"})
                return
            origin = urlparse(self.headers.get("Origin", ""))
            expected_scheme = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip().lower()
            host = self.headers.get("Host", "")
            if (origin.scheme not in ("http", "https") or origin.scheme != expected_scheme
                    or origin.netloc.casefold() != host.casefold() or origin.path not in ("", "/")
                    or origin.params or origin.query or origin.fragment or origin.username or origin.password):
                self.json_response(403, {"error": "same-origin request required"})
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 8192
                        or self.headers.get("Transfer-Encoding")
                        or self.headers.get_content_type() != "application/json"):
                    raise ValueError("invalid request body")
                payload = json.loads(self.rfile.read(int(lengths[0])).decode("utf-8"))
                if (not isinstance(payload, dict) or set(payload) != {"action", "fields"}
                        or payload["action"] not in ("approve", "reject")
                        or not isinstance(payload["fields"], dict)
                        or set(payload["fields"]) - {"name", "collection_url", "kind", "category", "trust", "scope"}):
                    raise ValueError("invalid request schema")
                candidate_id = int(match.group(1))
                with db.connect() as conn:
                    candidates = db.list_source_candidates(conn, status="all", limit=500)
                    candidate = next((item for item in candidates if item["id"] == candidate_id), None)
                if candidate is None:
                    self.json_response(404, {"error": "candidate not found"})
                    return
                probe = None
                fields = dict(payload["fields"])
                if payload["action"] == "approve":
                    kind = fields.get("kind", candidate["kind"])
                    url = fields.get("collection_url", candidate["collection_url"])
                    probe = probe_source(url, kind=kind)
                    if probe.ok:
                        fields["kind"] = probe.kind
                        fields["collection_url"] = probe.collection_url
                with db.connect() as conn:
                    result = db.review_source_candidate(conn, candidate_id, payload["action"], fields,
                                                        probe.__dict__ if probe is not None else None)
                self.json_response(200 if result["status"] != "needs_setup" else 422, result)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                self.json_response(400, {"error": "invalid request"})
            except Exception:
                self.json_response(500, {"error": "service unavailable"})
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
