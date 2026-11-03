from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from decimal import Decimal, InvalidOperation

from .net import canonical, fetch, public_url
from .sources import Page, date_ts, plain

REQUIRED = {"outerwear", "top", "bottom", "shoes", "hat", "bag"}
CATEGORIES = REQUIRED | {"scarf", "gloves", "accessory"}
LABELS = dict(outerwear="Куртка", top="Верх", bottom="Брюки", shoes="Обувь", hat="Шапка",
              bag="Сумка", scarf="Шарф", gloves="Перчатки", accessory="Аксессуар")


def utf16(text):
    visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(visible.encode("utf-16-le")) // 2


def words(text):
    return set(re.findall(r"[a-zа-яё0-9]{4,}", text.lower()))


def similar(a, b):
    x, y = words(a), words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= .72


def validate_product(item):
    try:
        p = dict(item)
        p["url"] = canonical(str(p["url"]))
        host = urllib.parse.urlsplit(p["url"]).hostname
        path = urllib.parse.urlsplit(p["url"]).path
        good_wb = (host == "wildberries.ru" or host.endswith(".wildberries.ru")) and "/catalog/" in path and "/detail" in path
        good_ozon = (host == "ozon.ru" or host.endswith(".ozon.ru")) and "/product/" in path
        if not (good_wb or good_ozon):
            raise ValueError()
        price = Decimal(str(p["price"]).replace(" ", "").replace(",", "."))
        if not price.is_finite() or price <= 0 or price > 200000 or price.as_tuple().exponent < -2:
            raise ValueError()
        if p["category"] not in CATEGORIES or not str(p["name"]).strip():
            raise ValueError()
        p.update(price=float(price), name=plain(str(p["name"]))[:100],
                 checked_at=date_ts(str(p.get("checked_at", "")), 0) if not isinstance(p.get("checked_at"), int) else p["checked_at"],
                 winter=str(p.get("winter", "true")).lower() in ("true", "1", "yes", "да"))
        if p.get("image"):
            p["image"] = public_url(p["image"], resolve=False)
        return p
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        raise ValueError("У вещи нужны: ссылка на карточку WB/Ozon, название, категория и положительная цена в рублях.") from None


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_json(v)


def verify_product(product, now=None):
    """Public structured offers only. Blocked marketplaces are not bypassed."""
    now = int(time.time() if now is None else now)
    p = validate_product(product)
    try:
        raw, _, _ = fetch(p["url"], cap=900000, timeout=8)
        page = Page()
        page.feed(raw.decode("utf-8", errors="replace"))
        for script in page.jsonld:
            try:
                for obj in walk_json(json.loads(script)):
                    types = obj.get("@type", [])
                    types = [types] if isinstance(types, str) else types
                    if "Product" not in types:
                        continue
                    if isinstance(obj.get("url"), str) and canonical(obj["url"]) != canonical(p["url"]):
                        continue  # A recommendation widget is not this product's offer.
                    offer = obj.get("offers", {})
                    offers = offer if isinstance(offer, list) else [offer]
                    for o in offers:
                        if str(o.get("priceCurrency", "")).upper() != "RUB":
                            continue
                        if str(o.get("availability", "")).rsplit("/", 1)[-1] != "InStock":
                            continue
                        amount = o.get("price")
                        if amount is None:
                            continue
                        verified = validate_product(dict(p, price=amount))
                        verified["checked_at"] = now
                        verified["verified"] = True
                        if page.image:
                            verified["image"] = public_url(page.image, resolve=False)
                        return verified
            except (ValueError, TypeError, KeyError):
                continue
    except (OSError, ValueError, RuntimeError):
        pass
    p["verified"] = False
    p["checked_at"] = 0
    return p


def news_options(result, articles, previous, config, now):
    known = {x["id"]: x for x in articles}
    options, used = [], set()
    raw_options = result.get("options", [])
    if not isinstance(raw_options, list):
        return []
    for raw in raw_options[:2]:
        if not isinstance(raw, dict):
            continue
        ids = raw.get("article_ids", [])
        if not isinstance(ids, list) or not ids or any(type(x) is not int or x not in known for x in ids):
            continue
        if used.intersection(ids):
            continue
        title, text = plain(str(raw.get("title", "")))[:110], str(raw.get("text", "")).strip()
        event = plain(str(raw.get("event_key", "")))[:120].lower()
        if not title or not event or len(text) < 50 or len(text) > 650:
            continue
        if any(w.lower() in (title+" "+text).lower() for w in config["exclude"]):
            continue
        all_old = previous + [{"title": p["title"], "event_key": p["event_key"]} for p in options]
        if any(event == p["event_key"] or similar(title, p["title"]) for p in all_old):
            continue
        refs = [{"url": known[i]["url"], "title": known[i]["title"], "published": known[i]["published"]} for i in ids[:3]]
        media = next((known[i]["image"] for i in ids if known[i]["image"]), "")
        p = dict(title=title, body=text, media=media, event_key=event, article_ids=ids,
                 meta={"sources": refs, "mode": config["mode"], "checked_at": int(now)})
        if utf16(render(p)) > 1000:
            continue
        options.append(p)
        used.update(ids)
    return options


def fashion_options(result, config, limit, now, allowed_urls=None, catalog=None):
    options = []
    catalogue = {str(i+1): p for i, p in enumerate(catalog or [])}
    allow = {canonical(x) for x in (allowed_urls or []) if x.startswith(("http://", "https://"))}
    cache = {}
    raw_options = result.get("options", [])
    if not isinstance(raw_options, list):
        return []
    for raw in raw_options[:2]:
        try:
            if catalogue:
                products = [dict(catalogue[str(i)]) for i in raw["item_ids"]]
                # Local catalogue has explicit human-checked timestamps.
                for p in products:
                    p["verified"] = (bool(p.get("verified")) and p.get("checked_at", 0) >= now-86400) if p.get("automatic") else p.get("checked_at", 0) >= now-86400
            else:
                products = [validate_product(x) for x in raw["items"]]
                if any(p["url"] not in allow for p in products):
                    continue
                for i, p in enumerate(products):
                    if p["url"] not in cache:
                        cache[p["url"]] = verify_product(p, now)
                    products[i] = cache[p["url"]]
            cats = {p["category"] for p in products}
            if not REQUIRED.issubset(cats) or len(products) > 9 or len({p["url"] for p in products}) != len(products):
                continue
            if not all(p["winter"] for p in products):
                continue
            total = sum(Decimal(str(p["price"])) for p in products)
            if total > limit:
                continue
            key = "outfit:" + "|".join(sorted(p["url"] for p in products))
            if any(key == x["event_key"] for x in options):
                continue
            title = plain(str(raw.get("title", "Зимний образ")))[:80]
            description = plain(str(raw.get("description", "")))[:180]
            meta = {"mode": "fashion", "products": products, "total": float(total), "budget": limit,
                    "description": description, "unverified": not all(p.get("verified") for p in products),
                    "checked_at": int(now)}
            p = dict(title=title, body=description, media="", event_key=key, meta=meta, article_ids=[])
            if utf16(render(p)) > 1000:
                # Keep product links and prices, remove only optional prose.
                p["body"] = ""
            if utf16(render(p)) <= 1000:
                options.append(p)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return options


def money(number):
    n = Decimal(str(number))
    return (f"{n:,.0f}" if n == n.to_integral() else f"{n:,.2f}").replace(",", " ")


def render(p):
    meta = p.get("meta", {})
    title, body = html.escape(p["title"]), html.escape(p["body"])
    lines = [f"<b>{title}</b>"]
    if body:
        lines += ["", body]
    if meta.get("mode") == "fashion":
        lines.append("")
        for item in meta["products"]:
            url = html.escape(item["url"], quote=True)
            lines.append(f'• <a href="{url}">{html.escape(LABELS[item["category"]])}</a> — {money(item["price"])} ₽')
        lines += ["", f'<b>Весь образ: {money(meta["total"])} ₽</b>',
                  "Цены на момент проверки. На изображении — ИИ-визуализация."]
    else:
        refs = meta.get("sources", [])[:3]
        if refs:
            lines += ["", "Источники: " + " · ".join(f'<a href="{html.escape(s["url"], quote=True)}">{i+1}</a>' for i, s in enumerate(refs))]
    return "\n".join(lines)


def image_prompt(p):
    products = p["meta"].get("products", [])
    details = "\n".join(f'{LABELS[x["category"]]}: {x["name"]}. Карточка: {x["url"]}' for x in products)
    return (
        "Создай вертикальную модную editorial-фотографию 4:5 для Telegram-канала «Собрано». "
        "Две взрослые модели рядом, в полный рост, включая шапки и обувь. Одна спортивного телосложения, "
        "вторая немного более полная, с естественными пропорциями. Лица неразличимы: скрыты мягкой тенью или "
        "нейтральной маской без черт; никаких узнаваемых людей. На ОБЕИХ один и тот же полный зимний комплект "
        "из прикреплённых фотографий реальных товаров. Сохрани цвет, крой, материал, застёжки и детали каждой вещи. "
        "Вещи надеты аккуратно, гармоничная многослойность, естественные складки и правильная анатомия. "
        "Сумка и все аксессуары видны. Чистый спокойный фон, красивый свет, без цен и надписей. "
        "Если нет фотографий товаров, сначала попроси приложить их, не придумывай внешний вид по ссылкам.\n\n" + details
    )
