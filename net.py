from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request


class NetError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def public_url(url: str, resolve: bool = True) -> str:
    p = urllib.parse.urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Нужна обычная публичная ссылка http/https без логина и пароля.")
    if p.port not in (None, 80, 443):
        raise ValueError("Для источников разрешены только порты 80 и 443.")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("Локальные адреса нельзя использовать как новостной источник.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
        if resolve:
            try:
                addresses = [ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)]
            except OSError:
                raise NetError("Не удалось определить адрес источника.") from None
    if any(not x.is_global for x in addresses):
        raise ValueError("Источник должен находиться в публичном интернете.")
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(public_url(url, resolve=False))
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "yclid"}]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(query), ""))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, headers: dict | None = None, cap: int = 1500000, timeout: int = 12):
    public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "SMMManager/1.0 (personal RSS reader)", **(headers or {})})
    try:
        with urllib.request.build_opener(SafeRedirect).open(req, timeout=timeout) as r:
            raw = r.read(cap + 1)
            if len(raw) > cap:
                raise NetError("Источник вернул слишком большой документ.")
            return raw, dict(r.headers), r.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return b"", {"Status": "304"}, url
        raise NetError(f"Источник ответил HTTP {e.code}.", e.code) from None
    except (OSError, ValueError) as e:
        if isinstance(e, ValueError):
            raise
        raise NetError("Источник недоступен или превышено время ожидания.") from None


def api_json(url: str, data: dict | None = None, headers: dict | None = None,
             form: bool = False, timeout: int = 20) -> dict:
    # Only fixed service endpoints are passed here; source URLs use fetch().
    raw = None
    h = dict(headers or {})
    if data is not None:
        raw = (urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
        h["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(url, data=raw, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(2000001)
            if len(body) > 2000000:
                raise NetError("Слишком большой ответ сервиса.")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        code = ""
        try:
            err = json.loads(e.read(8000))
            code = str(err.get("error", {}).get("code", "")) if isinstance(err.get("error"), dict) else str(err.get("error", ""))
        except (ValueError, AttributeError):
            pass
        raise NetError(f"Сервис ответил HTTP {e.code}.", e.code, code[:100]) from None
    except (OSError, ValueError):
        raise NetError("Нет корректного ответа сервиса; автоматического повтора нет.") from None
