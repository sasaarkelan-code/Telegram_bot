from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .net import NetError, canonical, fetch, public_url
from .store import Store


def plain(value):
    return " ".join(html.unescape(re.sub("<[^>]*>", " ", value or "")).split())


def date_ts(value, default=0):
    try:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default


def fingerprint(title):
    return hashlib.sha256(" ".join(sorted(set(re.findall(r"\w+", title.lower())))).encode()).hexdigest()


def parse_feed(raw, source, now):
    if raw.lstrip().startswith(b"{"):
        data = json.loads(raw)
        result = []
        for p in data.get("items", [])[:30]:
            try:
                url = canonical(p.get("url") or p["external_url"])
                result.append(dict(url=url, title=plain(p.get("title", ""))[:240],
                                   summary=plain(p.get("summary") or p.get("content_text") or p.get("content_html", ""))[:2500],
                                   published=date_ts(p.get("date_published", ""), 0),
                                   image=p.get("image", ""), source=source))
            except (ValueError, KeyError):
                continue
        return result
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD и внешние XML-сущности не поддерживаются.")
    root = ET.fromstring(raw)
    results = []
    for item in [x for x in root.iter() if x.tag.rsplit("}", 1)[-1] in ("item", "entry")][:30]:
        fields, image, link = {}, "", ""
        for child in item.iter():
            key = child.tag.rsplit("}", 1)[-1]
            text = "".join(child.itertext()).strip()
            fields.setdefault(key, text)
            if key == "link":
                if child.get("rel", "alternate") == "alternate":
                    link = child.get("href") or text or link
            if key in ("thumbnail", "content", "enclosure") and child.get("url"):
                if child.get("type", "").startswith("image") or key in ("thumbnail", "content"):
                    image = child.get("url")
        try:
            url = canonical(link)
        except ValueError:
            continue
        published = date_ts(fields.get("pubDate") or fields.get("published") or fields.get("date") or fields.get("updated", ""), 0)
        title = plain(fields.get("title", ""))[:240]
        if not title:
            continue
        results.append(dict(url=url, title=title,
                            summary=plain(fields.get("description") or fields.get("summary") or fields.get("encoded") or fields.get("content", ""))[:2500],
                            published=published, image=image, source=source))
    return results


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.paragraph = 0
        self.parts = []
        self.image = ""
        self.jsonld = []
        self.in_json = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.in_json = True
        if tag in ("script", "style", "nav", "footer", "header"):
            self.skip += 1
        if tag in ("p", "h1", "h2"):
            self.paragraph += 1
        if tag == "meta" and a.get("property", a.get("name")) == "og:image":
            self.image = a.get("content", "")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.skip = max(0, self.skip-1)
        if tag == "script":
            self.in_json = False
        if tag in ("p", "h1", "h2"):
            self.paragraph = max(0, self.paragraph-1)
            self.parts.append("\n")

    def handle_data(self, text):
        if self.in_json:
            self.jsonld.append(text)
        if self.paragraph and not self.skip:
            self.parts.append(text)


class Sources:
    def __init__(self, store: Store):
        self.store = store

    def collect(self, channel, now=None):
        now = int(time.time() if now is None else now)
        errors = []
        for source in channel["config"]["sources"][:30]:
            cache = self.store.rows("SELECT * FROM feed_cache WHERE url=?", (source,))
            cache = cache[0] if cache else {}
            if cache.get("checked", 0) > now-600:
                continue
            headers = {}
            if cache.get("etag"):
                headers["If-None-Match"] = cache["etag"]
            if cache.get("modified"):
                headers["If-Modified-Since"] = cache["modified"]
            try:
                raw, h, _ = fetch(source, headers, timeout=8)
                if h.get("Status") != "304":
                    for p in parse_feed(raw, source, now):
                        if not p["published"] or p["published"] > now+300:
                            continue
                        try:
                            p["image"] = public_url(p["image"], resolve=False) if p["image"] else ""
                        except ValueError:
                            p["image"] = ""
                        self.store.execute("""INSERT INTO articles(url,title,summary,source,published,image,fingerprint,fetched)
                                              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                                              summary=excluded.summary,image=CASE WHEN excluded.image!='' THEN excluded.image ELSE articles.image END,fetched=excluded.fetched""",
                                           (p["url"], p["title"], p["summary"], source, p["published"], p["image"], fingerprint(p["title"]), now))
                self.store.execute("INSERT OR REPLACE INTO feed_cache VALUES(?,?,?,?,?)",
                                   (source, h.get("ETag", cache.get("etag")), h.get("Last-Modified", cache.get("modified")), now, ""))
            except (NetError, ValueError, ET.ParseError, KeyError):
                errors.append(urllib.parse.urlsplit(source).hostname or "источник")
                self.store.execute("INSERT OR REPLACE INTO feed_cache VALUES(?,?,?,?,?)",
                                   (source, cache.get("etag"), cache.get("modified"), now, "Не удалось прочитать RSS/JSON Feed"))
        return errors

    def shortlist(self, channel, now=None):
        now = int(time.time() if now is None else now)
        cfg = channel["config"]
        srcs = cfg["sources"][:30]
        if not srcs:
            return []
        marks = ",".join("?" for _ in srcs)
        # Already offered links are not burned through again on every time slot.
        rows = self.store.rows(f"""SELECT * FROM articles a WHERE source IN ({marks}) AND published BETWEEN ? AND ?
            AND NOT EXISTS(SELECT 1 FROM candidate_articles ca JOIN candidates p ON p.id=ca.candidate_id
              JOIN slots s ON s.id=p.slot_id WHERE ca.article_id=a.id AND s.channel_id=?)
            ORDER BY published DESC LIMIT 50""", (*srcs, now-cfg["max_age_hours"]*3600, now, channel["id"]))
        chosen, seen = [], set()
        # Mix publishers before spending tokens on a short model context.
        groups={}
        for r in rows: groups.setdefault(r["source"],[]).append(r)
        rows=[group[i] for i in range(max((len(g) for g in groups.values()),default=0)) for group in groups.values() if i<len(group)]
        for r in rows:
            text = (r["title"]+" "+r["summary"]).lower()
            if any(w.lower() in text for w in cfg["exclude"]):
                continue
            if cfg["include"] and not any(w.lower() in text for w in cfg["include"]):
                continue
            if r["fingerprint"] in seen:
                continue
            seen.add(r["fingerprint"])
            chosen.append(r)
            if len(chosen) == 6:
                break
        return chosen

    def enrich(self, article):
        if article["text"]:
            return article
        try:
            raw, _, url = fetch(article["url"], cap=800000, timeout=10)
            page = Page()
            page.feed(raw.decode("utf-8", errors="replace"))
            text = " ".join(" ".join(page.parts).split())[:3500]
            image = article["image"]
            if not image and page.image:
                image = public_url(urllib.parse.urljoin(url, page.image), resolve=False)
            self.store.execute("UPDATE articles SET text=?,image=? WHERE id=?", (text, image, article["id"]))
            article.update(text=text, image=image)
        except (NetError, ValueError):
            pass
        return article

    def products(self):
        # Optional local, real catalogue. Never manufacture demo products/prices.
        return self.store.get("products", [])

    def import_products(self, path):
        import csv
        from pathlib import Path
        path = Path(path)
        if path.stat().st_size > 2000000:
            raise ValueError("Каталог слишком большой.")
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as f:
                data = list(csv.DictReader(f))
        else:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        from .editor import validate_product
        result = [validate_product(p) for p in data]
        if len(result) > 300:
            raise ValueError("В одном каталоге максимум 300 вещей.")
        self.store.put("products", result)
        return len(result)
