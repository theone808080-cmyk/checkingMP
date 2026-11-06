import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from web import telegram_auth


TOKEN = '12345:example-token'
NOW = 1700000000


def signed_data(user=42, timestamp=NOW):
    fields = {'auth_date': str(timestamp), 'query_id': 'query', 'user': json.dumps({'id': user})}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TelegramAuthTests(unittest.TestCase):
    def test_valid_and_invalid_payloads(self):
        valid = signed_data()
        self.assertTrue(telegram_auth.validate_init_data(valid, TOKEN, '42', NOW))
        for raw, token, owner in [(valid + '&auth_date=1', TOKEN, '42'),
                                  (valid.replace('query', 'wrong'), TOKEN, '42'),
                                  (valid, '', '42'), (valid, TOKEN, '43'),
                                  ('', TOKEN, '42'), (signed_data(timestamp=NOW-301), TOKEN, '42'),
                                  (signed_data(timestamp=NOW+31), TOKEN, '42')]:
            with self.subTest(raw=raw, owner=owner):
                self.assertFalse(telegram_auth.validate_init_data(raw, token, owner, NOW))

    def test_session_expires_and_cannot_be_forged(self):
        cookie = telegram_auth.new_session(TOKEN, '42', NOW)
        self.assertTrue(telegram_auth.valid_session(cookie, TOKEN, '42', NOW + 1799))
        self.assertFalse(telegram_auth.valid_session(cookie, TOKEN, '42', NOW + 1800))
        self.assertFalse(telegram_auth.valid_session(cookie, TOKEN, '43', NOW + 1))
        self.assertFalse(telegram_auth.valid_session(cookie[:-1] + ('0' if cookie[-1] != '0' else '1'), TOKEN, '42', NOW + 1))


if __name__ == '__main__':
    unittest.main()
