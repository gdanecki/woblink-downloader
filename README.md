# woblink-downloader

Skrypt, który pobiera wszystkie książki z Twojej półki na Woblink (https://woblink.com/account/moja-polka).

Woblink nie pozwala pobrać całej biblioteki naraz. Każdą książkę trzeba otworzyć, kliknąć format, poczekać, aż plik się wygeneruje, i kliknąć „Pobierz plik” jeszcze raz. Skrypt robi to za Ciebie w prawdziwej przeglądarce (Playwright), książka po książce, przez wszystkie strony półki.

> Skrypt pobiera tylko pliki, które i tak możesz pobrać ze swojej półki. Nie obchodzi zabezpieczeń DRM: książki z Adobe DRM zostaną zapisane jako pliki `.acsm`.

## Wymagania

- macOS albo Linux
- Python 3.9 lub nowszy
- konto Woblink z kupionymi książkami

## Instalacja

```bash
cd ~/Documents/woblink-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

## Użycie

```bash
source .venv/bin/activate
python woblink_download.py
```

1. Otworzy się okno przeglądarki ze stroną półki.
2. Przy pierwszym uruchomieniu zaloguj się na Woblink. Logowanie zostaje zapamiętane w katalogu `browser-profile/`.
3. Gdy widzisz swoją półkę, wróć do terminala i wciśnij **Enter**.
4. Skrypt przejdzie przez wszystkie strony półki i pobierze książki do katalogu `ksiazki/`.

Na Macu, przy pobieraniu całej biblioteki, uruchamiaj skrypt z `caffeinate`, żeby komputer nie usnął w trakcie:

```bash
caffeinate -i python woblink_download.py --threads 3
```

### Opcje

| Opcja | Opis | Domyślnie |
|---|---|---|
| `--format {epub,mobi,pdf}` | preferowany format e-booków | `epub` |
| `--threads N` | liczba równoległych kart przeglądarki, od 1 do 5 | `1` |
| `--audiobooks` | pobieraj też audiobooki (nieprzetestowane) | wyłączone |
| `--out KATALOG` | katalog na pobrane pliki | `ksiazki/` |
| `--limit N` | pobierz tylko N książek (do testów) | bez limitu |
| `--start-page N` | zacznij od tej strony półki | `1` |
| `--dry-run` | tylko wypisz tytuły z półki, nic nie pobieraj | — |
| `--opera` | użyj Opery zamiast wbudowanego Chromium (eksperymentalne) | — |

### Przykłady

```bash
# sprawdź, czy skrypt widzi całą półkę
python woblink_download.py --dry-run

# pobierz 3 książki na próbę
python woblink_download.py --limit 3

# pobierz wszystko w MOBI, w 3 kartach naraz
python woblink_download.py --format mobi --threads 3
```

## Jak to działa

1. Skrypt ustala liczbę stron półki i rozdziela je między karty przeglądarki (`--threads`). Każda karta obsługuje inne strony, więc karty nie pobierają tych samych książek.
2. Dla każdej książki klika okładkę, a w oknie książki klika ikonę wybranego formatu. Woblink zaczyna wtedy generować plik.
3. Co 2 sekundy klika „Pobierz plik” ponownie, aż plik się pobierze. Czeka najwyżej 3 minuty na książkę.
4. Jeśli książki nie ma w wybranym formacie, pobiera pierwszy dostępny (kolejno EPUB, MOBI, PDF) i wypisuje np. `brak formatu epub, pobieram pdf`.
5. Audiobooki są domyślnie pomijane.

### Wznawianie

Każda pobrana książka trafia do `done.txt` jako „Tytuł — Autor”. Po przerwaniu (Ctrl+C, zamknięcie okna, błąd) wystarczy uruchomić skrypt ponownie: pobrane książki zostaną pominięte.

Żeby pobrać książkę jeszcze raz, usuń jej wiersz z `done.txt`.

## Pliki i katalogi

| Ścieżka | Zawartość |
|---|---|
| `woblink_download.py` | skrypt |
| `ksiazki/` | pobrane książki |
| `browser-profile/` | profil przeglądarki z zapamiętanym logowaniem. **Nie udostępniaj go nikomu.** |
| `done.txt` | lista pobranych książek, używana przy wznawianiu |
| `failed.txt` | książki, których nie udało się pobrać |
| `errors.log` | szczegóły błędów (traceback) |
| `debug/` | zrzuty ekranu i HTML strony z chwili błędu |

## Rozwiązywanie problemów

- **Książka ląduje w `failed.txt`.** Sprawdź `errors.log` i zrzut ekranu w `debug/`, a potem uruchom skrypt ponownie. Często pomaga zwykłe ponowienie, bo generowanie pliku po stronie Woblink bywa wolne.
- **Skrypt nie widzi książek albo klika nie to, co trzeba.** Woblink mógł zmienić wygląd strony. Selektory CSS są zebrane na początku `woblink_download.py` (`TILE_SELECTOR`, `COVER_SELECTOR` itd.) i trzeba je dopasować do nowej strony.
- **Wylogowało Cię.** Zaloguj się w oknie przeglądarki i wciśnij Enter jeszcze raz.
- **Woblink zwalnia albo odrzuca żądania.** Zmniejsz `--threads`. Do pobierania całej biblioteki zalecane są 2–3 karty, nie 5.

## Uwagi

- Pobieraj tylko na własny użytek. Pliki mogą zawierać znak wodny z danymi Twojego konta.
- Dodaj `browser-profile/`, `.venv/`, `ksiazki/` i `debug/` do `.gitignore`, jeśli trzymasz projekt w repozytorium.
