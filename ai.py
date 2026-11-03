from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .auth import Auth, AuthError, RESOURCE
from .budget import Budget, BudgetStop
from .store import Store


class AIError(RuntimeError):
    pass


def parse_sse(response):
    data = []
    total = 0
    while True:
        line = response.readline(200001)
        total += len(line)
        if len(line) > 200000 or total > 2500000:
            raise AIError("Ответ ИИ превысил ограничение размера потока.")
        if not line:
            if data:
                yield json.loads("\n".join(data))
            return
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("data:"):
            value = s[5:].lstrip()
            if value == "[DONE]":
                return
            data.append(value)
        elif not s and data:
            yield json.loads("\n".join(data))
            data = []


def source_urls(output):
    urls = set()
    for item in output:
        if item.get("type") == "web_search_call":
            for s in (item.get("action") or {}).get("sources", []):
                if s.get("url"):
                    urls.add(s["url"])
        for content in item.get("content", []):
            for a in content.get("annotations", []):
                if a.get("type") == "url_citation" and a.get("url"):
                    urls.add(a["url"])
    return urls


class AI:
    def __init__(self, store: Store, auth: Auth, opener=None):
        self.store, self.auth = store, auth
        self.budget = Budget(store)
        self.opener = opener or urllib.request.urlopen
        self.last_usage = None

    def call(self, channel_id, slot_id, instructions, context, web=False):
        model = self.store.get("model", "")
        if not model:
            raise AuthError("Модель не выбрана. Выполни LOGIN.bat.")
        token = self.auth.access()
        payload = {"model": model, "store": False, "stream": True,
                   "instructions": instructions,
                   "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]}
        if web:
            payload["tools"] = [{"type": "web_search", "filters": {"allowed_domains": ["wildberries.ru", "ozon.ru"]}}]
            payload["include"] = ["web_search_call.action.sources"]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        rid = self.budget.reserve(channel_id, slot_id, len(raw))
        limits = self.store.get("limits")
        req = urllib.request.Request(RESOURCE+"/responses", data=raw,
                                     headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"})
        usage = None
        finished = threading.Event()
        holder = {}
        abort_reason = []

        def watchdog():
            deadline = time.monotonic()+limits["request_seconds"]
            while not finished.wait(.4):
                reason = ""
                if self.store.get("paused", True):
                    reason = "Операция остановлена владельцем или защитой."
                elif time.monotonic() >= deadline:
                    reason = "Превышено время одного запроса."
                if reason:
                    abort_reason.append(reason)
                    response = holder.get("response")
                    if response:
                        try:
                            response.close()
                        except OSError:
                            pass
                    return

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        try:
            output, text, calls, terminal = [], "", set(), False
            if self.store.get("paused", True):
                raise AIError("Остановлено до отправки запроса.")
            with self.opener(req, timeout=min(25, limits["request_seconds"])) as response:
                holder["response"] = response
                for event in parse_sse(response):
                    if abort_reason or self.store.get("paused", True):
                        raise AIError(abort_reason[0] if abort_reason else "Операция остановлена.")
                    typ = event.get("type", "")
                    if typ == "response.output_text.delta":
                        text += event.get("delta", "")
                        if len(text.encode()) > limits["request_output_bytes"]:
                            raise AIError("Превышен размер ответа; поток закрыт.")
                    if typ == "response.output_item.added" and event.get("item", {}).get("type") == "web_search_call":
                        calls.add(event["item"].get("id", event.get("output_index")))
                        if len(calls) > limits["web_calls"]:
                            raise AIError("ИИ попытался выполнить лишний поиск; поток закрыт.")
                    if typ in ("response.failed", "response.incomplete", "error"):
                        body = event.get("response", {})
                        usage = body.get("usage")
                        raise AIError("ИИ завершил запрос с ошибкой или ограничением подписки.")
                    if typ == "response.completed":
                        body = event.get("response", {})
                        usage = body.get("usage")
                        output = body.get("output", [])
                        terminal = True
                        if not text:
                            text = "".join(p.get("text", "") for x in output for p in x.get("content", []) if p.get("type") == "output_text")
                        break
            if not terminal:
                raise AIError("Поток оборвался без подтверждения завершения.")
            clean = text.strip()
            if clean.startswith("~~~") or clean.startswith(chr(96)*3):
                clean = "\n".join(clean.splitlines()[1:-1])
            try:
                result = json.loads(clean)
            except ValueError:
                raise AIError("ИИ вернул не JSON. Автоматического повторного запроса нет.") from None
            if not isinstance(result, dict):
                raise AIError("Неожиданный формат ответа.")
            self.budget.finish(rid, usage)
            if not isinstance(usage, dict) or self.store.get("paused", True):
                raise BudgetStop("Защита остановила обработку ответа. Проверь «Лимиты».")
            self.last_usage = usage
            return result, source_urls(output)
        except Exception as e:
            self.budget.finish(rid, usage, error=type(e).__name__)
            if isinstance(e, (AIError, BudgetStop)):
                raise
            raise AIError("Запрос не завершён. Новых попыток автоматически не будет.") from None
        finally:
            finished.set()
            watcher.join(timeout=1)
