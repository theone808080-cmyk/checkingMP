"""Telegram Mini App initData and short-lived session verification."""

import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl


def validate_init_data(raw, token, allowed_user_id, now=None):
    if not isinstance(raw, str) or not raw or len(raw) > 8192 or not token:
        return False
    if not isinstance(allowed_user_id, str) or not re.fullmatch(r'[1-9][0-9]*', allowed_user_id):
        return False
    try:
        if re.search(r'%(?![0-9a-fA-F]{2})', raw):
            return False
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding='utf-8', errors='strict', max_num_fields=100)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            return False
        signature = fields.pop('hash')
        if not re.fullmatch(r'[0-9a-f]{64}', signature):
            return False
        auth_date = fields['auth_date']
        if not re.fullmatch(r'[0-9]{1,12}', auth_date):
            return False
        now = time.time() if now is None else now
        if not now - 300 <= int(auth_date) <= now + 30:
            return False
        user = json.loads(fields['user'])
        if not isinstance(user, dict) or type(user.get('id')) is not int or str(user['id']) != allowed_user_id:
            return False
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (KeyError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return False


def new_session(token, owner, now=None):
    expires = int(time.time() if now is None else now) + 1800
    payload = f'{expires}.{secrets.token_hex(32)}'
    key = hmac.new(token.encode(), ('radar-session:' + owner).encode(), hashlib.sha256).digest()
    return payload + '.' + hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def valid_session(value, token, owner, now=None):
    if not token or not owner or not isinstance(value, str):
        return False
    if not re.fullmatch(r'[0-9]{1,12}\.[0-9a-f]{64}\.[0-9a-f]{64}', value):
        return False
    expiry, nonce, signature = value.split('.')
    current = time.time() if now is None else now
    if not current < int(expiry) <= current + 1800:
        return False
    key = hmac.new(token.encode(), ('radar-session:' + owner).encode(), hashlib.sha256).digest()
    expected = hmac.new(key, f'{expiry}.{nonce}'.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
