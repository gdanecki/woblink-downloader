#!/usr/bin/env python3
"""
Pobiera wszystkie książki z Twojej półki Woblink (https://woblink.com/account/moja-polka).

Działanie:
  1. Otwiera okno przeglądarki (osobny profil, logujesz się raz, logowanie jest zapamiętywane).
  2. Ty logujesz się, jeśli trzeba, i wciskasz Enter w terminalu, gdy widzisz swoją półkę.
  3. Skrypt przechodzi przez strony półki i klika okładkę każdej książki.
  4. W oknie książki klika ikonę wybranego formatu (Woblink generuje plik), potem klika
     ją ponownie i zapisuje pobrany plik.

Opcja --threads N (1-5) pobiera równolegle w N kartach przeglądarki; każda karta
obsługuje inne strony półki.

Pobrane książki są zapisywane w done.txt (tytuł i autor), więc ponowne uruchomienie
pomija już pobrane. Przy błędzie zapisuje zrzut ekranu i HTML do katalogu debug/,
a szczegóły błędu do errors.log.

Skrypt pobiera wyłącznie pliki, które normalnie możesz pobrać ze swojej półki.
Nie obchodzi zabezpieczeń DRM (książki z Adobe DRM zostaną zapisane jako pliki .acsm).
"""
import argparse
import asyncio
import re
import sys
import time
import traceback
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser-profile"
DEBUG_DIR = BASE_DIR / "debug"
DONE_FILE = BASE_DIR / "done.txt"
FAILED_FILE = BASE_DIR / "failed.txt"
ERRORS_FILE = BASE_DIR / "errors.log"

SHELF_URL = "https://woblink.com/account/moja-polka"
OPERA_PATH = "/Applications/Opera.app/Contents/MacOS/Opera"

# Elementy półki (na podstawie HTML strony z października 2026)
TILE_SELECTOR = ".user-page__shelflist .shelflist-item"
TITLE_SELECTOR = ".shelflist-item__title"
AUTHOR_SELECTOR = ".shelflist-item__author"
COVER_SELECTOR = ".shelflist-item__cover img"
AUDIOBOOK_SELECTOR = ".shelflist-item__type--audiobook"
PAGE_NUMBER_SELECTOR = ".user-page__pagination_dot p"
NEXT_PAGE_SELECTOR = ".user-page__pagination_dot--next"
CURRENT_PAGE_SELECTOR = ".user-page__pagination_dot--current"
CLOSE_SELECTOR = (
    ".user-page__popup_close, "
    "[aria-label*='zamknij' i], [aria-label*='close' i], "
    "[class*='close']:visible, button:text-is('×')"
)

MAX_THREADS = 5
DOWNLOAD_TIMEOUT_MS = 30_000
GENERATION_TIMEOUT_S = 180  # ile najdłużej czekamy, aż Woblink wygeneruje plik
PAUSE_BETWEEN_BOOKS_S = 2.0


def load_lines(path):
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def append_line(path, text):
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def safe_name(text):
    return re.sub(r"[^\w-]+", "-", text, flags=re.UNICODE).strip("-")[:80] or "ksiazka"


async def save_debug(page, name):
    DEBUG_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
    (DEBUG_DIR / f"{name}.html").write_text(await page.content(), encoding="utf-8")


def threads_arg(value):
    number = int(value)
    if not 1 <= number <= MAX_THREADS:
        raise argparse.ArgumentTypeError(f"liczba wątków musi być od 1 do {MAX_THREADS}")
    return number


# --- Półka -------------------------------------------------------------------

async def wait_for_tiles(page):
    await page.wait_for_selector(TILE_SELECTOR, timeout=30_000)
    await page.wait_for_timeout(1_000)


async def tile_info(tile):
    title = (await tile.locator(TITLE_SELECTOR).inner_text()).strip()
    author = (await tile.locator(AUTHOR_SELECTOR).inner_text()).strip()
    is_audiobook = await tile.locator(AUDIOBOOK_SELECTOR).count() > 0
    return title, author, is_audiobook


async def first_title(page):
    titles = page.locator(f"{TILE_SELECTOR} {TITLE_SELECTOR}")
    return (await titles.first.inner_text()).strip() if await titles.count() else ""


async def current_page_number(page):
    current = page.locator(CURRENT_PAGE_SELECTOR)
    if not await current.count():
        return 1
    text = (await current.first.inner_text()).strip()
    return int(text) if text.isdigit() else 1


async def last_page_number(page):
    """Najwyższy numer strony widoczny w stronicowaniu półki (np. 12)."""
    texts = await page.locator(PAGE_NUMBER_SELECTOR).all_inner_texts()
    numbers = [int(t.strip()) for t in texts if t.strip().isdigit()]
    return max(numbers) if numbers else 1


async def go_to_next_page(page):
    """Klika strzałkę "następna strona". Zwraca False, gdy to ostatnia strona."""
    next_button = page.locator(NEXT_PAGE_SELECTOR)
    if await next_button.count() == 0 or not await next_button.first.is_visible():
        return False
    before = await first_title(page)
    await next_button.first.click()
    # Strona półki zmienia się skryptem, bez przeładowania: czekamy na nowe tytuły.
    for _ in range(40):
        await page.wait_for_timeout(500)
        if await first_title(page) != before:
            await page.wait_for_timeout(1_000)
            return True
    return False  # kliknięcie nic nie zmieniło, czyli koniec półki


async def open_shelf_page(page, number):
    """Otwiera półkę od nowa i przechodzi na stronę o podanym numerze (od 1).

    Zwraca True, jeśli udało się dojść do tej strony.
    """
    await page.goto(SHELF_URL, wait_until="domcontentloaded")
    await wait_for_tiles(page)
    for _ in range(number - 1):
        if not await go_to_next_page(page):
            break
    return await current_page_number(page) == number


# --- Pobieranie --------------------------------------------------------------

FORMATS = ["epub", "mobi", "pdf", "mp3"]
ANY_FORMAT_SELECTOR = "[id^='pobierz_'][id$='_popup']"


def format_button(page, fmt):
    """Ikona pobierania w oknie książki, np. <button id="pobierz_epub_popup">."""
    return page.locator(f"#pobierz_{fmt}_popup").first


async def choose_format(page, preferred):
    """Wybrany format, a jeśli książki w nim nie ma, pierwszy dostępny (np. tylko PDF)."""
    try:
        await page.locator(ANY_FORMAT_SELECTOR).first.wait_for(state="visible",
                                                                timeout=DOWNLOAD_TIMEOUT_MS)
    except PlaywrightTimeout:
        return None  # okno książki się nie otworzyło
    for fmt in [preferred] + [f for f in FORMATS if f != preferred]:
        if await format_button(page, fmt).is_visible():
            return fmt
    return None


async def second_download_button(page):
    """Przycisk z napisem "Pobierz plik", który pojawia się po wygenerowaniu pliku (jeśli jest)."""
    loc = page.locator("a:visible, button:visible").filter(
        has_text=re.compile(r"pobierz\s+plik", re.IGNORECASE))
    return loc.first if await loc.count() else None


async def click_and_catch_download(page, element, out_dir, timeout_ms):
    """Klika element i czeka na pobranie pliku w tej karcie lub w otwartym przez nią oknie.

    Nasłuchujemy tylko własnej karty, żeby nie przechwycić pobrań z innych wątków.
    """
    downloads = []

    # Playwright wymaga zwykłych funkcji jako obsługi zdarzeń (nie np. downloads.append).
    def on_download(download):
        downloads.append(download)

    def on_popup(popup):
        popup.on("download", on_download)

    page.on("download", on_download)
    page.on("popup", on_popup)
    try:
        try:
            await element.click(timeout=10_000)
        except PlaywrightTimeout:
            # Przycisk zasłonięty (np. przez baner albo widżet czatu): wywołujemy kliknięcie bezpośrednio.
            await element.dispatch_event("click")
        waited = 0
        while not downloads and waited < timeout_ms:
            await page.wait_for_timeout(500)
            waited += 500
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("popup", on_popup)

    if not downloads:
        return None
    download = downloads[0]
    target = out_dir / download.suggested_filename
    counter = 2
    while target.exists():
        target = out_dir / f"{Path(download.suggested_filename).stem}-{counter}{target.suffix}"
        counter += 1
    await download.save_as(str(target))
    return target


async def download_from(page, fmt, out_dir):
    """Pobiera plik z okna książki: "Pobierz plik" (generowanie), potem znowu "Pobierz plik"."""
    available = await choose_format(page, fmt)
    if not available:
        return None
    if available != fmt:
        print(f"      brak formatu {fmt}, pobieram {available}")
    button = format_button(page, available)

    # 1. kliknięcie: Woblink generuje plik. Czasem pobieranie rusza od razu.
    saved = await click_and_catch_download(page, button, out_dir, timeout_ms=5_000)
    if saved:
        return saved

    # 2. kliknięcie, gdy plik jest gotowy: w przycisk "Pobierz plik", jeśli się pojawił,
    # albo ponownie w ikonę formatu. Generowanie może chwilę trwać, więc ponawiamy.
    deadline = time.time() + GENERATION_TIMEOUT_S
    while time.time() < deadline:
        await page.wait_for_timeout(2_000)
        target = await second_download_button(page) or button
        if not await target.is_visible() or not await target.is_enabled():
            continue
        saved = await click_and_catch_download(page, target, out_dir,
                                               timeout_ms=DOWNLOAD_TIMEOUT_MS)
        if saved:
            return saved
    return None


async def close_overlay(page):
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)
    close = page.locator(CLOSE_SELECTOR)
    for i in range(min(await close.count(), 5)):
        if await close.nth(i).is_visible():
            try:
                await close.nth(i).click(timeout=2_000)
            except PlaywrightTimeout:
                pass
            break


async def download_tile(page, tile, fmt, out_dir, debug_name):
    """Klika okładkę książki na półce i pobiera ją z okna, które się otworzy.

    Zwraca (ścieżka_pliku albo None, czy_trzeba_wrócić_na_półkę).
    """
    popups = []

    def on_popup(popup):
        popups.append(popup)

    page.on("popup", on_popup)
    url_before = page.url
    try:
        await tile.locator(COVER_SELECTOR).click()
        await page.wait_for_timeout(2_000)

        if popups:  # książka otworzyła się w nowej karcie
            popup = popups[0]
            await popup.wait_for_load_state("domcontentloaded")
            saved = await download_from(popup, fmt, out_dir)
            if not saved:
                await save_debug(popup, debug_name)
            await popup.close()
            return saved, False

        navigated = page.url != url_before
        saved = await download_from(page, fmt, out_dir)
        if not saved:
            await save_debug(page, debug_name)
        if not navigated:
            await close_overlay(page)
        return saved, navigated
    finally:
        page.remove_listener("popup", on_popup)


# --- Wątki -------------------------------------------------------------------

class Run:
    """Stan wspólny dla wszystkich wątków (asyncio działa w jednym wątku systemu,
    więc nie potrzebujemy blokad)."""

    def __init__(self, args, out_dir, done):
        self.args = args
        self.out_dir = out_dir
        self.done = done
        self.started = 0
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.total = 0

    def limit_reached(self):
        return bool(self.args.limit) and self.started >= self.args.limit


async def process_shelf_page(run, page, page_number, label):
    """Pobiera wszystkie książki z jednej strony półki."""
    count = await page.locator(TILE_SELECTOR).count()
    print(f"{label} === Strona {page_number}: {count} książek ===")

    for index in range(count):
        tile = page.locator(TILE_SELECTOR).nth(index)
        title, author, is_audiobook = await tile_info(tile)
        key = f"{title} — {author}"
        run.total += 1

        if run.args.dry_run:
            print(f"  {'[audio] ' if is_audiobook else ''}{key}")
            continue
        if key in run.done or (is_audiobook and not run.args.audiobooks):
            run.skipped += 1
            continue
        if run.limit_reached():
            return
        run.started += 1

        fmt = "mp3" if is_audiobook else run.args.format
        print(f"{label} [str. {page_number}, {index + 1}/{count}] {key} ({fmt})")
        await tile.scroll_into_view_if_needed()
        try:
            saved, navigated = await download_tile(page, tile, fmt, run.out_dir, safe_name(title))
        except Exception as exc:  # pojedyncza książka nie przerywa całego procesu
            print(f"{label}     błąd: {exc}")
            append_line(ERRORS_FILE, f"=== {key}\n{traceback.format_exc()}")
            try:
                await save_debug(page, safe_name(title))
            except Exception:
                pass
            saved, navigated = None, page.url != SHELF_URL

        if saved:
            print(f"{label}     zapisano: {saved.name}")
            append_line(DONE_FILE, key)
            run.done.add(key)
            run.ok += 1
        else:
            print(f"{label}     nie udało się: {key} (zrzut w {DEBUG_DIR}/)")
            append_line(FAILED_FILE, key)
            run.failed += 1

        if navigated:  # wracamy na tę samą stronę półki
            await open_shelf_page(page, page_number)
        await asyncio.sleep(PAUSE_BETWEEN_BOOKS_S)


async def worker(run, page, queue, number):
    """Jeden "wątek": własna karta przeglądarki, pobiera kolejne strony półki z kolejki."""
    label = f"[w{number}]"
    while not queue.empty() and not run.limit_reached():
        page_number = queue.get_nowait()
        if not await open_shelf_page(page, page_number):
            print(f"{label} nie udało się otworzyć strony {page_number} półki, pomijam ją")
            append_line(ERRORS_FILE, f"=== strona półki {page_number}: nie udało się jej otworzyć")
            continue
        await process_shelf_page(run, page, page_number, label)


# --- Program -----------------------------------------------------------------

async def main_async(args):
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    run = Run(args, out_dir, load_lines(DONE_FILE))

    async with async_playwright() as p:
        launch_options = {"headless": False, "accept_downloads": True}
        if args.opera:
            launch_options["executable_path"] = OPERA_PATH
        context = await p.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(SHELF_URL, wait_until="domcontentloaded")

        prompt = (f"\nZaloguj się w otwartym oknie, jeśli trzeba, i upewnij się, że widzisz swoją półkę\n"
                  f"({SHELF_URL}), a potem wciśnij Enter tutaj... ")
        await asyncio.get_running_loop().run_in_executor(None, input, prompt)

        await wait_for_tiles(page)
        last_page = await last_page_number(page)
        queue = asyncio.Queue()
        for number in range(args.start_page, last_page + 1):
            queue.put_nowait(number)

        # Podgląd listy idzie po kolei w jednej karcie, żeby wynik był czytelny.
        threads = 1 if args.dry_run else min(args.threads, queue.qsize()) or 1
        print(f"Półka ma {last_page} stron. Strony do przejścia: {queue.qsize()}, wątki: {threads}.")

        pages = [page] + [await context.new_page() for _ in range(threads - 1)]
        await asyncio.gather(*(worker(run, tab, queue, i + 1) for i, tab in enumerate(pages)))

        if args.dry_run:
            print(f"\nZnaleziono {run.total} książek.")
        else:
            print(f"\nGotowe. Pobrano: {run.ok}, nieudane: {run.failed}, "
                  f"pominięte (już pobrane lub audiobooki): {run.skipped}.")
            print(f"Pliki w: {out_dir}")
            if run.failed:
                print(f"Lista nieudanych: {FAILED_FILE}")
        await context.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Pobiera wszystkie książki z półki Woblink.")
    parser.add_argument("--format", default="epub", choices=["epub", "mobi", "pdf"],
                        help="format e-booków (domyślnie: epub)")
    parser.add_argument("--threads", type=threads_arg, default=1, metavar="N",
                        help=f"liczba równoległych wątków (kart przeglądarki), 1-{MAX_THREADS}, "
                             "domyślnie 1")
    parser.add_argument("--audiobooks", action="store_true",
                        help="pobieraj też audiobooki (domyślnie pomijane, nieprzetestowane)")
    parser.add_argument("--out", default=str(BASE_DIR / "ksiazki"), help="katalog na pobrane pliki")
    parser.add_argument("--limit", type=int, default=0, help="pobierz tylko N książek (do testów)")
    parser.add_argument("--dry-run", action="store_true",
                        help="tylko przejdź przez wszystkie strony i wypisz tytuły")
    parser.add_argument("--start-page", type=int, default=1, help="zacznij od tej strony półki")
    parser.add_argument("--opera", action="store_true",
                        help="użyj Opery zamiast wbudowanego Chromium (eksperymentalne)")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
