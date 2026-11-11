"""HTTPS-only fetches for reviewed public source candidates."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlparse, urlunparse

MAX_REDIRECTS = 5


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, ip: str, timeout: float):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _PinnedResponse:
    def __init__(self, response: http.client.HTTPResponse, connection: _PinnedHTTPSConnection):
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = response.headers

    def read(self, size=-1):
        return self._response.read(size)

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()


def _open_pinned(url: str, ip: str, timeout: float):
    parsed = urlparse(url)
    connection = _PinnedHTTPSConnection(parsed.hostname or "", ip, timeout)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    try:
        connection.request("GET", path, headers={
            "Host": parsed.netloc,
            "User-Agent": "CheckingMP/1.0",
            "Connection": "close",
        })
        return _PinnedResponse(connection.getresponse(), connection)
    except Exception:
        connection.close()
        raise


def _validated_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid source URL")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid source URL") from None
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
            or port not in (None, 443)):
        raise ValueError("source must use public HTTPS on the standard port")
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or any(ord(char) < 33 for char in hostname):
        raise ValueError("invalid source host")
    return value, hostname


def _public_addresses(hostname: str) -> list[str]:
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("source host cannot be resolved") from None
    addresses = []
    for record in records:
        try:
            address = str(ipaddress.ip_address(record[4][0]))
        except ValueError:
            raise ValueError("source host returned an invalid address") from None
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("source host resolved to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("source host has no addresses")
    return addresses


def fetch_public(url: str, *, timeout: float = 10, max_bytes: int = 2_000_000, opener=None) -> bytes:
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60:
        raise ValueError("invalid source timeout")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 10_000_000:
        raise ValueError("invalid source response limit")
    request = opener or _open_pinned
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        current, hostname = _validated_url(current)
        addresses = _public_addresses(hostname)
        response = request(current, addresses[0], float(timeout))
        try:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise ValueError("source redirect is invalid or excessive")
                current = urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError("source probe returned an error status")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise ValueError("source response exceeds size limit")
                except ValueError as error:
                    if str(error) == "source response exceeds size limit":
                        raise
                    raise ValueError("invalid source content length") from None
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("source response exceeds size limit")
            return body
        finally:
            response.close()
    raise ValueError("source redirect is excessive")
