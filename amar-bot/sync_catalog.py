#!/usr/bin/env python3
"""
Синхронизация карточек A/MAR из Dropbox -> база для чат-бота (Instagram / WhatsApp).

Что делает:
  1. Обходит папку КАРТОЧКИ ГОТОВЫЕ в Dropbox (все категории, кроме _старое).
  2. Для каждого изделия (папка с артикулом, например R-168741) находит:
       фото      ART.jpg, ART-2.jpg ... ART-4.jpg
       видео     ART.mp4
       короткая  ART.pdf              (карточка с ценой)
       подробная ART-описание.pdf     (полная карточка)
  3. Берёт публичные фото и видео с сайта каталога
     (serene-cucurucho-853f94.netlify.app/assets/img/<АРТ>/N.jpg и assets/360/<АРТ>.mp4).
     С флагом --pdf-links дополнительно создаёт публичные ссылки Dropbox на PDF-карточки.
  4. Читает ART-описание.html и вытаскивает название, характеристики, цены.
  5. Пишет:
       catalog/products.json   — полная база (для автоматизаций: Make, ChatPlace, API)
       catalog/products.csv    — та же база таблицей (для импорта в конструкторы ботов)
       catalog/knowledge_base.md — текст для «базы знаний» ИИ-ассистента

Запуск (Windows / Mac / Linux, нужен только Python 3.8+):
    set DROPBOX_TOKEN=sl.xxxxx          (Windows cmd)
    $env:DROPBOX_TOKEN="sl.xxxxx"       (PowerShell)
    export DROPBOX_TOKEN=sl.xxxxx       (Mac/Linux)
    python sync_catalog.py

Токен: https://www.dropbox.com/developers/apps -> Create app -> Scoped access ->
права files.metadata.read, files.content.read (+ sharing.read, sharing.write для --pdf-links) ->
Generate access token.

Запускать заново каждый раз, когда добавились/изменились карточки.
Уже созданные ссылки кэшируются в catalog/.links_cache.json, повторный запуск быстрый.
"""
import csv
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = os.environ.get("AMAR_DROPBOX_ROOT", "/ювилирка/для сайта офиц/КАРТОЧКИ ГОТОВЫЕ")
SKIP_DIRS = {"_старое"}
OUT = Path(__file__).resolve().parent / "catalog"
CATALOG_SITE = "https://serene-cucurucho-853f94.netlify.app"
WHATSAPP = "https://wa.me/996550339966"
ART_RE = re.compile(r"^[A-Z]{1,3}-\d{3,}(?:-\d+)?$")
PHOTO_RE = lambda art: re.compile(r"^" + re.escape(art) + r"(?:-(\d))?\.jpe?g$", re.I)

API = "https://api.dropboxapi.com/2/"
CONTENT = "https://content.dropboxapi.com/2/"


# ---------------------------------------------------------------- Dropbox API
def _token():
    t = os.environ.get("DROPBOX_TOKEN")
    if not t:
        sys.exit("Нет DROPBOX_TOKEN. См. инструкцию в начале файла.")
    return t


def rpc(endpoint, payload, retries=5):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(API + endpoint, data=data, headers={
            "Authorization": "Bearer " + _token(),
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{endpoint}: {e.code} {body}") from None
    raise RuntimeError(f"{endpoint}: слишком много повторов")


def download_text(path):
    req = urllib.request.Request(CONTENT + "files/download", data=b"", headers={
        "Authorization": "Bearer " + _token(),
        "Dropbox-API-Arg": json.dumps({"path": path}, ensure_ascii=True),
        "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode("utf-8", "replace")


def list_all(path):
    res = rpc("files/list_folder", {"path": path, "recursive": True, "limit": 2000})
    entries = res["entries"]
    while res.get("has_more"):
        res = rpc("files/list_folder/continue", {"cursor": res["cursor"]})
        entries += res["entries"]
    return entries


def direct(url):
    url = re.sub(r"([?&])dl=[01]", r"\1raw=1", url)
    return url if "raw=1" in url else url + ("&" if "?" in url else "?") + "raw=1"


def shared_link(path, cache):
    if path in cache:
        return cache[path]
    try:
        url = rpc("sharing/create_shared_link_with_settings", {"path": path})["url"]
    except RuntimeError as e:
        if "shared_link_already_exists" not in str(e):
            raise
        links = rpc("sharing/list_shared_links", {"path": path, "direct_only": True})["links"]
        if not links:
            raise
        url = links[0]["url"]
    cache[path] = direct(url)
    return cache[path]


# ------------------------------------------------------------- парсинг карточки
class _Text(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "section", "table", "header", "footer"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, a):
        if tag in ("style", "script"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_lines(src):
    p = _Text()
    p.feed(src)
    text = re.sub(r"[  ]+", " ", "".join(p.out))
    return [l.strip() for l in text.split("\n") if l.strip()]


def parse_card(src, art):
    lines = html_to_lines(src)
    card = {"name": "", "tagline": "", "specs": {}, "description": "", "prices": [], "price_note": ""}
    try:
        i = lines.index(art) + 1
    except ValueError:
        i = 0
    head = []
    while i < len(lines) and lines[i] not in ("Характеристики", "Описание", "Цена по карату", "Цена") and "\t" not in lines[i]:
        head.append(lines[i]); i += 1
    if head:
        card["tagline"] = head[-1] if len(head) > 1 else ""
        card["name"] = " ".join(head[:-1] if len(head) > 1 else head)
    section, header = None, None
    for l in lines[i:]:
        cells = [c.strip() for c in l.split("\t") if c.strip()]
        if l in ("Характеристики", "Описание") or l.startswith("Цена по") or l == "Цена":
            section = l; header = None; continue
        if l.startswith("Цена ориентировочная"):
            card["price_note"] = l; section = None; continue
        if l.startswith("Instagram") or l.startswith("WhatsApp"):
            break
        if section == "Характеристики" and len(cells) == 2:
            card["specs"][cells[0]] = cells[1]
        elif section == "Описание":
            card["description"] = (card["description"] + " " + l).strip()
        elif section and section.startswith("Цена") and len(cells) >= 2:
            if header is None and not any("$" in c or "запрос" in c for c in cells):
                header = cells
            else:
                keys = header if header and len(header) == len(cells) else [f"col{k}" for k in range(len(cells))]
                card["prices"].append(dict(zip(keys, cells)))
    return card


# ------------------------------------------------------------- тексты ответов
def price_lines(prices):
    out = []
    for row in prices:
        vals = list(row.values()) if isinstance(row, dict) else list(row)
        keys = list(row.keys()) if isinstance(row, dict) else []
        if not vals:
            continue
        if len(vals) == 2 and "$" not in vals[0] and "запрос" not in vals[0]:
            out.append(f"{vals[0]}: {vals[1]}")
        elif "$" not in vals[0] and "запрос" not in vals[0]:
            out.append(f"{vals[0]}: " + " · ".join(
                f"{k.replace('Золото ', '')} — {v}" for k, v in zip(keys[1:], vals[1:])))
        else:
            out.append(" · ".join(f"{k} — {v}" for k, v in zip(keys, vals)))
    return out


INTERNAL = re.compile(r"исходник|перенес[её]н|в папке", re.I)


def public_specs(specs):
    return {k: v for k, v in specs.items() if not INTERNAL.search(f"{k} {v}")}


def short_text(p):
    s = public_specs(p["specs"])
    metal = s.get("Металл", "")
    lines = [f"{p['art']} — {p['name']}".strip(" —")]
    if metal:
        lines.append(f"Металл: {metal}")
    lines += price_lines(p["prices"])
    cert = " ".join(v for k, v in s.items() if k.lower().startswith("сертификат"))
    lines.append(("Выращенные бриллианты, сертификат IGI · " if "IGI" in cert else "")
                 + ("рассрочка" if "IGI" in cert else "Рассрочка") + " 0% MBANK / ZERO")
    if p["category"] == "Кольца":
        lines.append("Цена может измениться в зависимости от размера кольца.")
    lines.append("Хотите подробную карточку с характеристиками?")
    return "\n".join(lines)


def long_text(p):
    lines = [f"{p['art']} — {p['name']}", p["tagline"], ""]
    lines += [f"{k}: {v}" for k, v in public_specs(p["specs"]).items()]
    if p["description"]:
        lines += ["", p["description"]]
    if p["prices"]:
        lines += ["", "Цены:"] + price_lines(p["prices"])
    if p["price_note"]:
        lines += ["", p["price_note"]]
    lines += ["", f"Каталог с ценами: {CATALOG_SITE}",
              "Весь ассортимент (около 480 изделий): https://alma-do-mar.com/magazin",
              "Напишите, что понравилось — подберём размер и пришлём фото/видео 🤍",
              f"WhatsApp: {WHATSAPP}"]
    return "\n".join(l for l in lines if l is not None).strip()


# ------------------------------------------------ фото и видео с сайта каталога
def load_site_items():
    """Список изделий с сайта каталога (там лежат публичные фото и видео)."""
    req = urllib.request.Request(CATALOG_SITE + "/", headers={"User-Agent": "amar-catalog-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        src = r.read().decode("utf-8", "replace")
    i = src.index("const ITEMS=") + len("const ITEMS=")
    items, _ = json.JSONDecoder().raw_decode(src[i:])
    return {x["art"]: x for x in items}


def site_media(art, site):
    """Публичные ссылки на фото/видео. Варианты (BR-270106-3) берут фото основного артикула."""
    key = art if art in site else re.sub(r"-\d+$", "", art)
    item = site.get(key)
    if not item:
        return None
    n = max(1, min(int(item.get("n") or 1), 4))
    return {"site_art": key,
            "photos": [f"{CATALOG_SITE}/assets/img/{key}/{k}.jpg" for k in range(1, n + 1)],
            "video": f"{CATALOG_SITE}/assets/360/{key}.mp4" if item.get("vid") else None,
            "same_as_base": key != art}


# ------------------------------------------------------------------- main
def main():
    OUT.mkdir(exist_ok=True)
    cache_f = OUT / ".links_cache.json"
    cache = json.loads(cache_f.read_text("utf-8")) if cache_f.exists() else {}
    pdf_links = "--pdf-links" in sys.argv
    print("Читаю каталог на сайте (фото и видео)…")
    try:
        site = load_site_items()
    except Exception as ex:  # noqa: BLE001
        print("  не удалось открыть сайт каталога:", ex)
        site = {}

    print("Читаю список файлов в Dropbox…")
    entries = list_all(ROOT)
    root_l = ROOT.lower().rstrip("/")
    folders = {}
    for e in entries:
        if e[".tag"] != "file":
            continue
        rel = e["path_lower"][len(root_l) + 1:]
        parts = e["path_display"].split("/")[-(rel.count("/") + 1):]
        if parts[0] in SKIP_DIRS or len(parts) < 2:
            continue
        art = parts[-2]
        if not ART_RE.match(art):
            continue
        f = folders.setdefault(art, {"category": parts[0].capitalize(),
                                     "subfolder": parts[1] if len(parts) > 3 else None,
                                     "files": {}})
        f["files"][parts[-1]] = e["path_display"]

    products = []
    for n, (art, f) in enumerate(sorted(folders.items()), 1):
        files = f["files"]
        print(f"[{n}/{len(folders)}] {art}")
        photos = sorted((name for name in files if PHOTO_RE(art).match(name)),
                        key=lambda x: int(PHOTO_RE(art).match(x).group(1) or 1))
        p = {"art": art, "category": f["category"], "subfolder": f["subfolder"]}
        desc_html = files.get(f"{art}-описание.html") or files.get(f"{art}.html")
        if desc_html:
            try:
                p.update(parse_card(download_text(desc_html), art))
            except Exception as ex:  # noqa: BLE001
                p["error"] = str(ex)
        media = {"photos": [files[x] for x in photos][:4],
                 "video": files.get(f"{art}.mp4"),
                 "card_short": files.get(f"{art}.pdf"),
                 "card_long": files.get(f"{art}-описание.pdf")}
        p["dropbox"] = media
        p["media"] = site_media(art, site)
        if pdf_links:
            try:
                p["links"] = {
                    "photos": [shared_link(x, cache) for x in media["photos"]],
                    "video": shared_link(media["video"], cache) if media["video"] else None,
                    "card_short": shared_link(media["card_short"], cache) if media["card_short"] else None,
                    "card_long": shared_link(media["card_long"], cache) if media["card_long"] else None,
                }
            except Exception as ex:  # noqa: BLE001
                p["links_error"] = str(ex)
            cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
        p.setdefault("name", ""); p.setdefault("tagline", ""); p.setdefault("specs", {})
        p.setdefault("description", ""); p.setdefault("prices", []); p.setdefault("price_note", "")
        p["reply_short"] = short_text(p)
        p["reply_long"] = long_text(p)
        products.append(p)

    write_outputs(products)
    print(f"Готово: {len(products)} изделий -> {OUT}")


def write_outputs(products):
    OUT.mkdir(exist_ok=True)
    (OUT / "products.json").write_text(json.dumps(
        {"updated": time.strftime("%Y-%m-%d"), "count": len(products), "products": products},
        ensure_ascii=False, indent=1), "utf-8")

    with open(OUT / "products.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["art", "category", "name", "metal", "photo1", "photo2", "photo3", "photo4",
                    "video", "card_short", "card_long", "reply_short", "reply_long"])
        for p in products:
            L = p.get("links") or {}
            M = p.get("media") or {}
            ph = (M.get("photos") or L.get("photos") or []) + [""] * 4
            w.writerow([p["art"], p["category"], p["name"], p["specs"].get("Металл", ""),
                        *ph[:4], M.get("video") or L.get("video") or "", L.get("card_short") or "",
                        L.get("card_long") or "", p["reply_short"], p["reply_long"]])

    md = ["# База знаний A/MAR — карточки изделий", "",
          f"Обновлено: {time.strftime('%Y-%m-%d')} · изделий: {len(products)}", ""]
    for p in products:
        L = p.get("links") or {}
        M = p.get("media") or {}
        md += [f"## {p['art']}", "", f"Категория: {p['category']}" + (f" / {p['subfolder']}" if p.get("subfolder") else ""), ""]
        md += ["### Ответ 1 — фото и видео", ""]
        photos = M.get("photos") or L.get("photos") or []
        video = M.get("video") or L.get("video")
        if photos or video:
            if M.get("same_as_base"):
                md.append(f"(фото модели {M['site_art']} — та же модель, другой вариант)")
            md += [f"- Фото {i}: {u}" for i, u in enumerate(photos[:2], 1)]
            if video:
                md.append(f"- Видео: {video}")
            md += [f"- Ещё фото: {u}" for u in photos[2:]]
        else:
            md.append("Фото и видео в базе нет — скажи, что менеджер пришлёт фото, и передай диалог менеджеру.")
        md += ["", f"Подпись: {p['art']} — {p['name']}. Прислать карточку с ценой?", ""]
        md += ["### Ответ 2 — короткая карточка", "", p["reply_short"], ""]
        if L.get("card_short"):
            md += [f"PDF: {L['card_short']}", ""]
        md += ["### Ответ 3 — подробная карточка", "", p["reply_long"], ""]
        if L.get("card_long"):
            md += [f"PDF: {L['card_long']}", ""]
    (OUT / "knowledge_base.md").write_text("\n".join(md), "utf-8")


if __name__ == "__main__":
    main()
