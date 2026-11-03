from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import atomic_json
from .net import NetError, api_json

AUTH = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_LOCK = threading.RLock()


class AuthError(RuntimeError):
    pass


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_id_token(token, jwks, issuer, client_id, nonce=None, subject=None, now=None):
    """Verify, never merely decode, the OIDC identity."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    now = time.time() if now is None else now
    try:
        head64, payload64, signature64 = token.split(".")
        head, claims = json.loads(unb64(head64)), json.loads(unb64(payload64))
        alg = head.get("alg")
        if alg not in ("RS256", "ES256"):
            raise ValueError()
        keys = [k for k in jwks["keys"] if k.get("kid") == head.get("kid")
                and k.get("use", "sig") == "sig" and k.get("alg", alg) == alg]
        if len(keys) != 1:
            raise ValueError()
        key = keys[0]
        message = (head64 + "." + payload64).encode()
        signature = unb64(signature64)
        if alg == "RS256" and key["kty"] == "RSA":
            public = rsa.RSAPublicNumbers(int.from_bytes(unb64(key["e"]), "big"), int.from_bytes(unb64(key["n"]), "big")).public_key()
            public.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256" and key["kty"] == "EC" and key["crv"] == "P-256" and len(signature) == 64:
            public = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(key["x"]), "big"), int.from_bytes(unb64(key["y"]), "big"), ec.SECP256R1()).public_key()
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            public.verify(der, message, ec.ECDSA(hashes.SHA256()))
        else:
            raise ValueError()
        aud = claims.get("aud", [])
        aud = [aud] if isinstance(aud, str) else aud
        if claims.get("iss") != issuer or client_id not in aud:
            raise ValueError()
        if len(aud) > 1 and claims.get("azp") != client_id:
            raise ValueError()
        if type(claims.get("exp")) not in (int, float) or claims["exp"] <= now-30:
            raise ValueError()
        if claims.get("iat", now) > now+60 or claims.get("nbf", 0) > now+30:
            raise ValueError()
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError()
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError()
        if subject is not None and claims["sub"] != subject:
            raise ValueError()
        return claims
    except Exception:
        raise AuthError("Подпись или параметры входа не прошли проверку. Токены не сохранены.") from None


class Auth:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "credentials.json"

    def saved(self):
        with _LOCK:
            if not self.path.exists():
                return {}
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise AuthError("Не удалось прочитать локальное подключение. Выполни LOGIN.bat.") from None

    def discovery(self):
        d = api_json(AUTH + "/.well-known/openid-configuration")
        # A compromised metadata response must not forward credentials elsewhere.
        for key in ("issuer", "jwks_uri"):
            if urllib.parse.urlsplit(d.get(key, "")).scheme != "https" or urllib.parse.urlsplit(d.get(key, "")).hostname != "auth.openai.com":
                raise AuthError("Неожиданные параметры официального входа.")
        return d

    def host_id(self):
        """Persist one host URI; migrate legacy UUIDs without changing identity."""
        with _LOCK:
            path = self.root / "host.json"
            if not path.exists():
                host = {"id": uuid.uuid4().urn}
                atomic_json(path, host)
                return host["id"]
            try:
                host = json.loads(path.read_text(encoding="utf-8"))
                value = host["id"]
                if not isinstance(value, str) or not value or any(c.isspace() for c in value):
                    raise ValueError()
                # Keep other documented host URI formats if already configured.
                for prefix in ("did:key:", "urn:ietf:params:oauth:jwk-thumbprint:"):
                    if value.startswith(prefix) and len(value) > len(prefix):
                        return value
                normalized = uuid.UUID(value).urn
            except (OSError, ValueError, KeyError, TypeError):
                raise AuthError("Не удалось прочитать ID установки в host.json. "
                                "Восстанови этот файл из резервной копии; настройки удалять не нужно.") from None
            if value != normalized:
                host["id"] = normalized
                atomic_json(path, host)
            return normalized

    def login(self):
        old = self.saved()
        host_id = self.host_id()
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # OAuth codes and query strings never reach logs.

            def do_GET(self):
                p = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(p.query)
                good = p.path == "/auth/callback" and secrets.compare_digest(q.get("state", [""])[0], state)
                self.send_response(200 if good else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(("Вернись в окно настройки бота." if good else "Неверный ответ входа.").encode())
                if good:
                    result.update({k: v[0] for k, v in q.items()})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = .5
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        issued = old.get("client_id")
        params = dict(client_id=issued or "dynamic_agent_client", ext_agent_host_id=host_id,
                      response_type="code", redirect_uri=redirect, scope=SCOPE, resource=RESOURCE,
                      state=state, nonce=nonce, code_challenge_method="S256",
                      code_challenge=b64(hashlib.sha256(verifier.encode()).digest()))
        if not issued:
            params["agent_name_hint"] = "SMM Manager Local"
        url = AUTH + "/api/accounts/authorize?" + urllib.parse.urlencode(params)
        print("\nContinue with ChatGPT — вход в текущую подписку.")
        print("В браузере выбери свой аккаунт и разреши использование плана этому приложению.")
        print("Если браузер не открылся, открой эту ссылку НА ЭТОМ ЖЕ компьютере:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        try:
            while not result and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            raise AuthError("Время входа истекло. Запусти LOGIN.bat ещё раз.")
        if result.get("error"):
            raise AuthError("Вход или разрешение использования подписки не подтверждены.")
        callback_client = result.get("client_id", issued)
        if not callback_client or callback_client == "dynamic_agent_client" or (issued and callback_client != issued):
            raise AuthError("Регистрация приложения не завершена или изменился client_id.")
        if not result.get("code"):
            raise AuthError("В ответе входа отсутствует код.")
        tokens = api_json(AUTH + "/api/accounts/oauth/token",
                          dict(grant_type="authorization_code", client_id=callback_client,
                               code=result["code"], code_verifier=verifier, redirect_uri=redirect, resource=RESOURCE), form=True)
        discovery = self.discovery()
        jwks = api_json(discovery["jwks_uri"])
        claims = validate_id_token(tokens.get("id_token", ""), jwks, discovery["issuer"], callback_client, nonce,
                                   old.get("sub") if issued else None)
        if "chatgpt.tokens.use.direct" not in tokens.get("scope", "").split():
            raise AuthError("Аккаунт вошёл, но не разрешил запросы за счёт подписки. ИИ не включён.")
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise AuthError("Сервис не выдал необходимые токены. Проверь доступность подключения.")
        safe = dict(tokens, client_id=callback_client, sub=claims["sub"], email=claims.get("email", ""),
                    expires_at=time.time()+int(tokens.get("expires_in", 3600)))
        atomic_json(self.path, safe)
        print("Вход подтверждён. Пароль в бот не передавался.")
        return self.models()

    def access(self):
        with _LOCK:
            saved = self.saved()
            if not saved:
                raise AuthError("Сначала запусти LOGIN.bat на компьютере с ботом.")
            if saved.get("expires_at", 0) > time.time()+90:
                return saved["access_token"]
            try:
                result = api_json(AUTH + "/api/accounts/oauth/token",
                                  dict(grant_type="refresh_token", client_id=saved["client_id"],
                                       refresh_token=saved["refresh_token"], resource=RESOURCE), form=True)
                if not result.get("access_token"):
                    raise AuthError("Не удалось продлить вход. Запусти LOGIN.bat.")
                if "scope" in result and "chatgpt.tokens.use.direct" not in result["scope"].split():
                    raise AuthError("Разрешение использования подписки отозвано.")
                if result.get("id_token"):
                    d = self.discovery()
                    validate_id_token(result["id_token"], api_json(d["jwks_uri"]), d["issuer"],
                                      saved["client_id"], subject=saved["sub"])
                saved.update(result)
                saved["expires_at"] = time.time()+int(result.get("expires_in", 3600))
                atomic_json(self.path, saved)
                return saved["access_token"]
            except NetError:
                raise AuthError("Не удалось продлить вход. ИИ остановлен; выполни LOGIN.bat.") from None

    def models(self):
        body = api_json(RESOURCE + "/models", headers={"Authorization": "Bearer "+self.access()})
        result = [{"slug": x["slug"], "name": x.get("display_name", x["slug"])}
                  for x in body.get("models", []) if x.get("visibility") == "list" and x.get("slug")]
        if not result:
            raise AuthError("Сервис не вернул доступные модели для подписки. Платный API автоматически не включается.")
        return result

    def logout(self):
        saved = self.saved()
        confirmed = False
        if saved.get("refresh_token"):
            # Revocation may have an empty successful body, unlike normal API JSON.
            import urllib.request
            d = self.discovery()
            endpoint = d.get("revocation_endpoint", "")
            if urllib.parse.urlsplit(endpoint).hostname != "auth.openai.com":
                raise AuthError("Неизвестный адрес отключения. Отключи приложение в настройках ChatGPT.")
            req = urllib.request.Request(endpoint, data=urllib.parse.urlencode(
                dict(token=saved["refresh_token"], token_type_hint="refresh_token", client_id=saved["client_id"])).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    confirmed = r.status == 200
            except OSError:
                pass
        if self.path.exists():
            self.path.unlink()
        return confirmed
