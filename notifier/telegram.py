"""Private Telegram notifications with a durable, retryable outbox."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

LOCK_ID = 21790321


def settings():
    token = os.environ.get('TELEGRAM_TOKEN', '')
    chat = os.environ.get('TELEGRAM_CHAT_ID', '')
    url = os.environ.get('RADAR_MINIAPP_URL', '')
    if not token or not chat or not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if (not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token)
            or not re.fullmatch(r'[1-9][0-9]*', chat)
            or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(c) < 33 for c in url)):
        raise ValueError('Invalid Telegram notification configuration')
    return token, chat, url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_url(request, timeout=20):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def request_api(config, method, payload):
    token, _, _ = config
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                     data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with open_url(request) as response:
            data = json.loads(response.read(65537))
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise ValueError('API rejected request')
        return data.get('result')
    except Exception:
        raise RuntimeError('Telegram request failed') from None


def send_message(config, count):
    _, chat, url = config
    result = request_api(config, 'sendMessage', {
        'chat_id': chat, 'text': f'Новых новостей: {count}',
        'reply_markup': {'inline_keyboard': [[{'text': 'Открыть ленту', 'web_app': {'url': url}}]]},
    })
    if not isinstance(result, dict) or type(result.get('message_id')) is not int or result['message_id'] <= 0:
        raise RuntimeError('Telegram returned an invalid response')
    return result['message_id']


def set_menu():
    config = settings()
    if config is None:
        raise ValueError('Telegram notification configuration is missing')
    request_api(config, 'setChatMenuButton', {'chat_id': config[1], 'menu_button': {
        'type': 'web_app', 'text': 'Лента', 'web_app': {'url': config[2]}}})
    print('Telegram menu updated')


def baseline(conn):
    """Initialize once under the caller's session advisory lock, before collection."""
    with conn.transaction():
        created = conn.execute("INSERT INTO notification_state (id, initialized) VALUES (1, true) ON CONFLICT DO NOTHING RETURNING id").fetchone()
        if created:
            conn.execute('INSERT INTO notification_materials (material_id) SELECT id FROM materials ON CONFLICT DO NOTHING')


def claim_batch(conn):
    """Return existing unsent batch or atomically capture currently untracked IDs."""
    with conn.transaction():
        row = conn.execute('SELECT id, count FROM notification_batches WHERE sent_at IS NULL ORDER BY id LIMIT 1').fetchone()
        if row:
            return row
        ids = [r[0] for r in conn.execute('SELECT id FROM materials WHERE NOT EXISTS (SELECT 1 FROM notification_materials n WHERE n.material_id = materials.id) ORDER BY id').fetchall()]
        if not ids:
            return None
        batch = conn.execute('INSERT INTO notification_batches (count) VALUES (%s) RETURNING id, count', (len(ids),)).fetchone()
        conn.execute('INSERT INTO notification_materials (material_id, batch_id) SELECT unnest(%s::int[]), %s', (ids, batch[0]))
        return batch


def mark_sent(conn, batch_id, message_id):
    conn.execute('UPDATE notification_batches SET sent_at = now(), telegram_message_id = %s WHERE id = %s AND sent_at IS NULL', (message_id, batch_id))


def initialize(conn):
    conn.execute('SELECT pg_advisory_lock(%s)', (LOCK_ID,))
    try:
        baseline(conn)
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))


def notify_new_materials_status(conn, config, *, cycle_id=None):
    acquired = conn.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]
    if not acquired:
        return ("busy", None)
    try:
        batch = claim_batch(conn)
        if not batch:
            return ("empty", None)
        message_id = send_message(config, batch[1])
        delivered_ids = [row[0] for row in conn.execute(
            "SELECT material_id FROM notification_materials WHERE batch_id = %s ORDER BY material_id", (batch[0],)
        ).fetchall()]
        with conn.transaction():
            mark_sent(conn, batch[0], message_id)
            if cycle_id is not None:
                conn.execute(
                    """UPDATE cycles SET notification_status = 'sent', notification_checked_at = now(),
                              notification_delivered_material_ids = %s::jsonb WHERE id = %s""",
                    (json.dumps(delivered_ids), cycle_id),
                )
        return ("sent", delivered_ids)
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))


def notify_new_materials(conn, config):
    return notify_new_materials_status(conn, config)[0] == "sent"
