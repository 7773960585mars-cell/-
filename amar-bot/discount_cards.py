#!/usr/bin/env python3
"""
Снизить все цены в карточках A/MAR на N % (по умолчанию 30 %) и пересохранить PDF.

Работает с локальной папкой (синхронизируется с Dropbox):
    C:\\Users\\m7013\\Desktop\\раб стол\\Рабочий стол\\ювилирка\\для сайта офиц\\КАРТОЧКИ ГОТОВЫЕ

Для каждого изделия:
  1. копирует оригиналы ART.html, ART-описание.html, ART.pdf, ART-описание.pdf в
     КАРТОЧКИ ГОТОВЫЕ\\_старое\\до_скидки_30\\<путь изделия>\\  (если копии ещё нет);
  2. во всех суммах вида «$1 450» в HTML ставит цену −30 %, округляя до $10;
  3. печатает HTML заново в PDF через Microsoft Edge или Google Chrome (headless).

Повторный запуск безопасен: уже обработанные изделия записаны в
КАРТОЧКИ ГОТОВЫЕ\\_старое\\до_скидки_30\\done.txt и второй раз не уменьшаются.

Запуск (PowerShell, в папке amar-bot):
    python discount_cards.py --only R-185470     # сначала одно изделие — проверить PDF
    python discount_cards.py                     # все изделия
Параметры:
    --percent 30       размер скидки
    --root "C:\\...\\КАРТОЧКИ ГОТОВЫЕ"
    --dry-run          только показать, что изменится
    --no-pdf           изменить HTML, PDF не трогать
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = r"C:\Users\m7013\Desktop\раб стол\Рабочий стол\ювилирка\для сайта офиц\КАРТОЧКИ ГОТОВЫЕ"
ART_RE = re.compile(r"^[A-Z]{1,3}-\d{3,}(?:-\d+)?$")
# $1 450 · $1&nbsp;450 · $14 200 · $980 (пробел, неразрывный, узкий неразрывный, &nbsp;)
SEP = r"(?:[ \u00a0\u202f]|&nbsp;|&#160;|&#8239;)"
PRICE_RE = re.compile(r"\$(" + r"\d{1,3}(?:" + SEP + r"\d{3})+|\d+" + r")")

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/chromium", "/usr/bin/google-chrome",
]


def new_price(match, factor):
    raw = match.group(1)
    sep = re.search(SEP, raw)
    value = int(re.sub(r"\D", "", raw))
    value = int(round(value * factor / 10.0)) * 10
    s = f"{value:,}".replace(",", sep.group(0) if sep else "\u00a0")
    if value < 1000:
        s = str(value)
    return "$" + s


def discount_html(text, factor):
    return PRICE_RE.subn(lambda m: new_price(m, factor), text)


def find_browser():
    for b in BROWSERS:
        if b and Path(b).exists():
            return b
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def print_pdf(browser, html_path, pdf_path):
    tmp = pdf_path
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
           "--run-all-compositor-stages-before-draw", "--virtual-time-budget=5000",
           f"--print-to-pdf={tmp}", html_path.resolve().as_uri()]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if not tmp.exists() or tmp.stat().st_size < 1000:
        raise RuntimeError("браузер не создал PDF")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--percent", type=float, default=30)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()

    root = Path(a.root)
    if not root.is_dir():
        sys.exit(f"Папка не найдена: {root}")
    factor = 1 - a.percent / 100
    backup = root / "_старое" / f"до_скидки_{a.percent:g}"
    done_f = backup / "done.txt"
    done = set(done_f.read_text("utf-8").split()) if done_f.exists() else set()
    browser = None if (a.no_pdf or a.dry_run) else find_browser()
    if not (a.no_pdf or a.dry_run) and not browser:
        sys.exit("Не нашёл Microsoft Edge или Google Chrome для печати PDF (или запустите с --no-pdf).")

    folders = [d for d in root.rglob("*") if d.is_dir() and ART_RE.match(d.name)
               and "_старое" not in d.relative_to(root).parts]
    if a.only:
        folders = [d for d in folders if d.name in a.only]
    print(f"Изделий: {len(folders)}, скидка {a.percent:g} %, уже обработано ранее: {len(done)}")

    changed = errors = 0
    for d in sorted(folders):
        art = d.name
        if art in done:
            continue
        pairs = [(d / f"{art}.html", d / f"{art}.pdf"), (d / f"{art}-описание.html", d / f"{art}-описание.pdf")]
        pairs = [(h, p) for h, p in pairs if h.exists()]
        if not pairs:
            continue
        try:
            dst = backup / d.relative_to(root)
            work = []
            for h, p in pairs:
                orig = dst / h.name                      # цены всегда считаем от оригинала
                text = (orig if orig.exists() else h).read_text("utf-8")
                new, n = discount_html(text, factor)
                work.append((h, p, text, new, n))
            total = sum(w[4] for w in work)
            m_old, m_new = PRICE_RE.search(work[0][2]), PRICE_RE.search(work[0][3])
            print(f"{art}: цен {total}" + (f", напр. {m_old.group(0)} -> {m_new.group(0)}" if m_old else ""))
            if a.dry_run or total == 0:
                continue
            # 1) оригиналы в _старое (один раз)
            dst.mkdir(parents=True, exist_ok=True)
            for h, p, *_ in work:
                for f in (h, p):
                    if f.exists() and not (dst / f.name).exists():
                        shutil.copy2(f, dst / f.name)
            # 2) печать во временные файлы рядом с карточкой (чтобы подтянулись картинки/шрифты)
            ready = []
            for h, p, text, new, n in work:
                tmp_html = h.with_name(h.stem + ".__new__.html")
                tmp_html.write_text(new, "utf-8")
                tmp_pdf = p.with_name(p.stem + ".__new__.pdf")
                try:
                    if browser:
                        print_pdf(browser, tmp_html, tmp_pdf)
                finally:
                    pass
                ready.append((h, p, tmp_html, tmp_pdf))
            # 3) только теперь заменяем файлы
            for h, p, tmp_html, tmp_pdf in ready:
                tmp_html.replace(h)
                if tmp_pdf.exists():
                    tmp_pdf.replace(p)
            done.add(art)
            done_f.write_text("\n".join(sorted(done)), "utf-8")
            changed += 1
        except Exception as ex:  # noqa: BLE001
            errors += 1
            print(f"  ОШИБКА {art}: {ex} — файлы изделия не изменены")
            for t in d.glob("*.__new__.*"):
                t.unlink(missing_ok=True)
    print(f"Готово: изменено {changed}, ошибок {errors}. Оригиналы: {backup}")


if __name__ == "__main__":
    main()
