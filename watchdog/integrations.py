"""Small, credential-safe HTTP clients for Coolify and Telegram."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_url(request, timeout=15):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def _https_base(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("Invalid HTTPS endpoint")
    return value.rstrip("/")


def restart_coolify(api_url: str, token: str, application_uuid: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", application_uuid):
        raise ValueError("Invalid Coolify application identifier")
    url = _https_base(api_url) + "/api/v1/applications/" + urllib.parse.quote(application_uuid, safe="") + "/restart"
    request = urllib.request.Request(url, data=b"{}", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, method="POST")
    try:
        with open_url(request) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError("Coolify restart rejected")
            response.read(65537)
    except Exception:
        raise RuntimeError("Coolify restart request failed") from None


def send_telegram_alert(token: str, chat_id: str, text: str) -> bool:
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9][0-9]*", chat_id):
        raise ValueError("Invalid Telegram alert configuration")
    body = json.dumps({"chat_id": chat_id, "text": text[:4000]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                                     data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with open_url(request) as response:
            payload = json.loads(response.read(65537))
        result = payload.get("result") if isinstance(payload, dict) and payload.get("ok") is True else None
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise ValueError("Invalid Telegram response")
        return True
    except Exception:
        raise RuntimeError("Telegram alert delivery failed") from None