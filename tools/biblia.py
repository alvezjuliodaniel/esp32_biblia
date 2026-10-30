#!/usr/bin/env python3
"""
biblia.py - Cliente de puerto serie para la Biblia electronica RVR1960 (ESP32).

Uso rapido:
    python tools/biblia.py                    # abrir el lector interactivo
    python tools/biblia.py juan 3:16          # mostrar un versiculo
    python tools/biblia.py read "sal 23"      # mostrar un capitulo
    python tools/biblia.py search amor        # buscar en toda la Biblia
    python tools/biblia.py upload             # enviar data/bible.dat a la ESP32
    python tools/biblia.py ports              # listar puertos serie

El dispositivo habla un protocolo de texto: cada comando termina en una linea
"[OK]" o "[ERR] codigo mensaje".
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "bible.dat"
DEFAULT_BAUD = 115200
UPLOAD_CHUNK = 192  # el firmware usa un buffer de linea de 512 bytes

# ---------------------------------------------------------------------------
# Colores ANSI
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"

COLOR_ENABLED = True


def style(text, *codes):
    if not COLOR_ENABLED or not codes:
        return text
    return "".join(codes) + text + RESET


# ---------------------------------------------------------------------------
# Referencias ("juan 3:16", "1 co 13", "salmo 23")
# ---------------------------------------------------------------------------
REF_PATTERN = re.compile(
    r"^\s*(?P<book>.*?)\s*(?P<chapter>\d{1,3})\s*(?:[:.,]\s*(?P<verse>\d{1,3}))?"
    r"\s*(?:-\s*(?P<end>\d{1,3}))?\s*$")

# Referencia sin libro, relativa al libro que se esta leyendo: "3", "3:14".
LOCAL_REF_PATTERN = re.compile(
    r"^\s*(\d{1,3})\s*(?:[:.,]\s*(\d{1,3}))?\s*(?:-\s*(\d{1,3}))?\s*$")

BOOK_ALIASES = {
    "genesis": "GEN", "exodo": "EXO", "levitico": "LEV", "numeros": "NUM",
    "deuteronomio": "DEU", "josue": "JOS", "jueces": "JDG", "rut": "RUT",
    "1 samuel": "1SA", "2 samuel": "2SA", "1 reyes": "1KI", "2 reyes": "2KI",
    "1 cronicas": "1CH", "2 cronicas": "2CH", "esdras": "EZR", "nehemias": "NEH",
    "ester": "EST", "job": "JOB", "salmos": "PSA", "salmo": "PSA", "proverbios": "PRO",
    "eclesiastes": "ECC", "cantares": "SNG", "isaias": "ISA", "jeremias": "JER",
    "lamentaciones": "LAM", "ezequiel": "EZK", "daniel": "DAN", "oseas": "HOS",
    "joel": "JOL", "amos": "AMO", "abdias": "OBA", "jonas": "JON", "miqueas": "MIC",
    "nahum": "NAM", "habacuc": "HAB", "sofonias": "ZEP", "hageo": "HAG",
    "zacarias": "ZEC", "malaquias": "MAL", "mateo": "MAT", "marcos": "MRK",
    "lucas": "LUK", "juan": "JHN", "hechos": "ACT", "romanos": "ROM",
    "1 corintios": "1CO", "2 corintios": "2CO", "galatas": "GAL", "efesios": "EPH",
    "filipenses": "PHP", "colosenses": "COL", "1 tesalonicenses": "1TH",
    "2 tesalonicenses": "2TH", "1 timoteo": "1TI", "2 timoteo": "2TI", "tito": "TIT",
    "filemon": "PHM", "hebreos": "HEB", "santiago": "JAS", "1 pedro": "1PE",
    "2 pedro": "2PE", "1 juan": "1JN", "2 juan": "2JN", "3 juan": "3JN",
    "judas": "JUD", "apocalipsis": "REV",
}


def fold(text: str) -> str:
    """Minusculas sin tildes, para comparar nombres de libros."""
    import unicodedata

    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


class Reference:
    """Referencia biblica ya resuelta contra la lista de libros del dispositivo."""

    def __init__(self, book, chapter, verse=None, end=None):
        self.book = book          # dict con usfm, short, name, chapters
        self.chapter = chapter
        self.verse = verse
        self.end = end

    @property
    def label(self):
        base = f"{self.book['short']} {self.chapter}"
        if self.verse and self.end:
            return f"{base}:{self.verse}-{self.end}"
        if self.verse:
            return f"{base}:{self.verse}"
        return base


def parse_reference(text, books):
    """Convierte 'juan 3:16' en Reference. Devuelve None si no se entiende."""
    match = REF_PATTERN.match(text)
    verse = None
    end = None
    if match and match.group("book").strip():
        name = match.group("book").strip()
    else:
        # Sin capitulo explicito: "juan" o "juan 3 : 16"
        number_only = re.match(r"^\s*(?P<book>[^\d]*\d?[^\d]*)$", text)
        if not number_only:
            return None
        name = text.strip()
        match = None

    book = resolve_book(name, books)
    if book is None:
        return None

    if match is None:
        chapter = 1
    else:
        chapter = int(match.group("chapter"))
        if match.group("verse"):
            verse = int(match.group("verse"))
        if match.group("end"):
            end = int(match.group("end"))
    if chapter < 1:
        chapter = 1
    if chapter > book["chapters"]:
        chapter = book["chapters"]
    return Reference(book, chapter, verse, end)


def parse_local_reference(text, book):
    """Resuelve "3" o "3:14" dentro del libro que se esta leyendo."""
    match = LOCAL_REF_PATTERN.match(text)
    if not match:
        return None
    chapter = max(1, min(int(match.group(1)), book["chapters"]))
    verse = int(match.group(2)) if match.group(2) else None
    end = int(match.group(3)) if match.group(3) else None
    return Reference(book, chapter, verse, end)


def resolve_book(name, books):
    key = fold(name).strip()
    key = re.sub(r"\s+", " ", key)
    if not key:
        return None

    if key.isdigit():
        index = int(key)
        if 1 <= index <= len(books):
            return books[index - 1]
        return None

    if key in BOOK_ALIASES:
        usfm = BOOK_ALIASES[key]
        for book in books:
            if book["usfm"] == usfm:
                return book

    compact = key.replace(" ", "")
    for book in books:
        candidates = {
            fold(book["usfm"]),
            fold(book["short"]).replace(" ", ""),
            fold(book["name"]).replace(" ", ""),
        }
        if compact in candidates:
            return book
    for book in books:
        if fold(book["name"]).startswith(key) and len(key) >= 2:
            return book
    for book in books:
        if fold(book["name"]).replace(" ", "").startswith(compact) and len(compact) >= 2:
            return book
    return None


# ---------------------------------------------------------------------------
# Conexion con la ESP32
# ---------------------------------------------------------------------------
class DeviceError(RuntimeError):
    pass


class Response:
    def __init__(self, lines, ok, detail):
        self.lines = lines
        self.ok = ok
        self.detail = detail

    def number(self, index=0):
        parts = self.detail.split()
        try:
            return int(parts[index])
        except (IndexError, ValueError):
            return None


class Device:
    def __init__(self, port=None, baud=DEFAULT_BAUD, timeout=8.0):
        if serial is None:
            raise DeviceError("falta pyserial: pip install pyserial")
        self.port = port or autodetect_port(baud)
        self.baud = baud
        self.timeout = timeout
        self.link = None

    def open(self):
        if self.link and self.link.is_open:
            return self
        try:
            self.link = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                      write_timeout=10)
        except Exception as exc:  # pragma: no cover - depende del hardware
            raise DeviceError(f"no se pudo abrir {self.port}: {exc}") from exc

        # Abrir el puerto reinicia la placa (DTR), asi que el primer saludo se
        # pierde mientras arranca: esperamos y reintentamos antes de rendirnos.
        try:
            time.sleep(0.4)
            self.link.timeout = 2.0
            for _attempt in range(4):
                self.link.reset_input_buffer()
                self.link.write(b"ping\n")
                self.link.flush()
                try:
                    response = self._read_response()
                except DeviceError:
                    response = None
                if response is not None and response.ok:
                    return self
                time.sleep(0.9)
        except Exception as exc:
            self.close()
            raise DeviceError(f"error al abrir {self.port}: {exc}") from exc

        self.close()
        raise DeviceError(
            f"{self.port} no responde al protocolo de la Biblia "
            f"(revisa que el firmware este grabado y la velocidad sea {self.baud})")

    def close(self):
        if self.link is not None:
            try:
                if self.link.is_open:
                    self.link.close()
            finally:
                self.link = None

    def ask(self, command, timeout=None):
        if not self.link:
            self.open()
        self.link.timeout = timeout or self.timeout
        self.link.reset_input_buffer()
        self.link.write((command + "\n").encode("utf-8"))
        self.link.flush()
        return self._read_response()

    def _read_response(self):
        lines = []
        while True:
            raw = self.link.readline()
            if not raw:
                raise DeviceError("el dispositivo no responde (tiempo agotado)")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.startswith("["):
                if text.startswith("[OK]"):
                    return Response(lines, True, text[4:].strip())
                if text.startswith("[ERR]"):
                    return Response(lines, False, text[5:].strip())
                if text.startswith("[RDY]") or text.startswith("[BOOT]"):
                    continue
                # Cualquier otra linea entre corchetes se ignora.
                continue
            lines.append(text)

    # -- comandos concretos -------------------------------------------------
    def info(self):
        response = self.ask("info", timeout=6)
        if not response.ok:
            raise DeviceError(response.detail)
        data = {}
        for line in response.lines:
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0] == "i":
                data[parts[1]] = parts[2]
        return data

    def books(self):
        response = self.ask("books", timeout=10)
        if not response.ok:
            raise DeviceError(response.detail)
        books = []
        for line in response.lines:
            parts = line.split("|")
            if len(parts) >= 6 and parts[0] == "b":
                books.append({
                    "n": int(parts[1]),
                    "usfm": parts[2],
                    "short": parts[3],
                    "name": parts[4],
                    "chapters": int(parts[5]),
                })
        if not books:
            raise DeviceError("el dispositivo no tiene datos cargados; "
                              "ejecuta: python tools/biblia.py upload")
        return books

    def chapter(self, book, chapter):
        response = self.ask(f"chap {book['usfm']} {chapter}", timeout=25)
        if not response.ok:
            raise DeviceError(response.detail)
        return parse_chapter(response.lines)

    def verse(self, book, chapter, verse):
        response = self.ask(f"verse {book['usfm']} {chapter} {verse}", timeout=20)
        if not response.ok:
            raise DeviceError(response.detail)
        entries = parse_chapter(response.lines)
        texts = [text for kind, _, text in entries if kind == "v"]
        return texts[0] if texts else ""

    def search(self, query, start=0, limit=40, timeout=90):
        response = self.ask(f"find {start} {limit} {query}", timeout=timeout)
        if not response.ok:
            raise DeviceError(response.detail)
        matches = []
        for line in response.lines:
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0] == "m":
                matches.append((parts[1], parts[2]))
        numbers = [int(x) for x in response.detail.split() if x.isdigit()]
        next_chapter = numbers[0] if numbers else 0
        return matches, next_chapter

    def random(self):
        response = self.ask("rand", timeout=15)
        if not response.ok:
            raise DeviceError(response.detail)
        entries = parse_chapter(response.lines)
        for kind, number, text in entries:
            if kind == "v":
                return response.detail, number, text
        raise DeviceError("respuesta vacia del dispositivo")

    # -- carga de datos -----------------------------------------------------
    def upload(self, path, progress=True, on_progress=None):
        """Envia un archivo al dispositivo.

        'on_progress(enviado, total)' sustituye a la barra de consola y permite
        cancelar lanzando una excepcion desde el callback.
        """
        path = Path(path)
        if not path.exists():
            raise DeviceError(f"no existe {path}")
        size = path.stat().st_size
        response = self.ask(f"putbegin {size}", timeout=10)
        if not response.ok:
            raise DeviceError(response.detail)

        sent = 0
        started = time.time()
        with path.open("rb") as handle:
            while sent < size:
                block = handle.read(UPLOAD_CHUNK)
                if not block:
                    break
                response = self.ask(f"put {sent} {block.hex()}", timeout=20)
                if not response.ok:
                    raise DeviceError(f"fallo al escribir en {sent}: {response.detail}")
                sent += len(block)
                if on_progress is not None:
                    on_progress(sent, size)
                elif progress:
                    render_progress(sent, size, started)
        if progress and on_progress is None:
            print()
        response = self.ask(f"putend {size}", timeout=30)
        if not response.ok:
            raise DeviceError(response.detail)
        return sent


def parse_chapter(lines):
    """Convierte las lineas del dispositivo en (tipo, numero, texto)."""
    entries = []
    for line in lines:
        if line.startswith("v|"):
            _, number, text = line.split("|", 2)
            entries.append(("v", int(number), text))
        elif line.startswith("h|"):
            entries.append(("h", 0, line[2:]))
        elif line.startswith("~|"):
            entries.append(("~", 0, line[2:]))
    return entries


def render_progress(done, total, started):
    ratio = done / total if total else 1
    width = 30
    filled = int(width * ratio)
    speed = done / max(time.time() - started, 0.001) / 1024
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r  [{bar}] {ratio * 100:5.1f}%  {done / 1048576:5.2f} MB  "
                     f"{speed:6.1f} KB/s")
    sys.stdout.flush()


def autodetect_port(baud=DEFAULT_BAUD):
    if serial is None:
        raise DeviceError("falta pyserial: pip install pyserial")
    candidates = []
    if list_ports:
        for port in list_ports.comports():
            candidates.append(port.device)
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*"):
        candidates.extend(sorted(str(p) for p in Path("/").glob(pattern[1:])))
    unique = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    if not unique:
        raise DeviceError("no se encontro ningun puerto serie; usa --port")

    for port in unique:
        try:
            link = serial.Serial(port, baud, timeout=2, write_timeout=2)
        except Exception:
            continue
        try:
            time.sleep(0.25)
            link.reset_input_buffer()
            link.write(b"ping\n")
            deadline = time.time() + 2.5
            while time.time() < deadline:
                line = link.readline().decode("utf-8", "replace").strip()
                if line.startswith("[OK]"):
                    return port
                if not line and time.time() > deadline:
                    break
        except Exception:
            pass
        finally:
            link.close()
    raise DeviceError("ningun dispositivo respondio; usa --port para indicarlo")


# ---------------------------------------------------------------------------
# Presentacion
# ---------------------------------------------------------------------------
def terminal_width(default=100):
    try:
        return max(40, os.get_terminal_size().columns)
    except OSError:
        return default


def wrap_text(text, width):
    import textwrap

    return textwrap.wrap(text, width=width, break_long_words=True,
                         break_on_hyphens=True) or [""]


def print_chapter(book, chapter, entries, width=None, show_headings=True):
    width = width or terminal_width()
    title = f"{book['short']} — {book['name']} {chapter}"
    print()
    print(style(f" {title} ".center(width, "─"), BOLD, YELLOW))
    print()
    number_width = 4
    body_width = width - number_width - 1
    for kind, number, text in entries:
        if kind == "h":
            if not show_headings:
                continue
            for line in wrap_text(text, width - 2):
                print(style(f"  {line}", ITALIC, CYAN))
        elif kind == "~":
            print(style(f"  ({text})", DIM))
        else:
            wrapped = wrap_text(text, body_width)
            numbered = f"{number:>{number_width}}"
            print(f"{style(numbered, DIM)} {wrapped[0]}")
            for line in wrapped[1:]:
                print(" " * number_width + " " + line)
    print()


def print_verse(ref_label, number, text, width=None):
    width = width or terminal_width()
    body_width = width - 6
    prefix = f"{ref_label}:{number}"
    print()
    print(style(prefix, BOLD, YELLOW))
    for line in wrap_text(text, body_width):
        print(f"  {line}")
    print()


def print_matches(query, matches, width=None):
    width = width or terminal_width()
    if not matches:
        print(style("Sin resultados.", DIM))
        return
    needle = fold(query)
    for ref, text in matches:
        if len(text) > width - len(ref) - 6:
            body = text[: width - len(ref) - 9].rstrip() + "…"
        else:
            body = text
        body = highlight(body, needle)
        print(f"{style(ref.ljust(12), BOLD, CYAN)} {body}")


def highlight(text, needle):
    if not needle or not COLOR_ENABLED:
        return text
    folded = fold(text)
    index = folded.find(needle)
    if index < 0:
        return text
    end = index + len(needle)
    return text[:index] + style(text[index:end], BOLD, YELLOW) + text[end:]


# ---------------------------------------------------------------------------
# Marcadores
# ---------------------------------------------------------------------------
def bookmarks_path():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "biblia" / "bookmarks.json"


def load_bookmarks():
    path = bookmarks_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def save_bookmarks(items):
    path = bookmarks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Lector interactivo
# ---------------------------------------------------------------------------
HELP_TEXT = f"""
{style('Biblia RVR1960 en la ESP32', BOLD, YELLOW)}  {style('(enter = capitulo siguiente)', DIM)}

  {style('juan 3', CYAN)}         ir al capitulo        {style('juan 3:16', CYAN)}   ir al versiculo
  {style('n', GREEN)} / {style('p', GREEN)}             siguiente / anterior   {style('r', GREEN)}          versiculo aleatorio
  {style('/texto', CYAN)}         buscar en la Biblia    {style('/', CYAN)}          repetir la busqueda
  {style('b', GREEN)}               lista de libros       {style('i', GREEN)}          informacion del dispositivo
  {style('m', GREEN)} / {style('ml', GREEN)} / {style('md n', GREEN)} copiar marca / listar / borrar
  {style('w n', GREEN)}             ancho de linea         {style('?', GREEN)}          esta ayuda      {style('q', GREEN)}  salir
"""


class Reader:
    def __init__(self, device, data_path=None):
        self.device = device
        self.data_path = data_path or DEFAULT_DATA
        self.books = device.books()
        if not self.books:
            raise DeviceError("el dispositivo no devolvio la lista de libros")
        self.book = self.books[0]
        self.chapter_no = 1
        self.width = terminal_width()
        self.entries = []
        self.total_chapters = sum(book["chapters"] for book in self.books)
        self.last_query = None
        self.search_next = None
        self.search_hits = 0

    # -- navegacion ---------------------------------------------------------
    def go(self, reference):
        self.book = reference.book
        self.chapter_no = reference.chapter
        self.entries = self.device.chapter(self.book, self.chapter_no)
        if reference.verse:
            self.render_verses(reference)
        else:
            print_chapter(self.book, self.chapter_no, self.entries, self.width)

    def render_verses(self, reference):
        end = reference.end or reference.verse
        found = False
        for kind, number, text in self.entries:
            if kind == "v" and reference.verse <= number <= end:
                print_verse(f"{self.book['short']} {self.chapter_no}", number, text,
                            self.width)
                found = True
        if not found:
            print(style(f"No existe {reference.label} en el capitulo.", RED))

    def move(self, delta):
        chapter = self.chapter_no + delta
        if 1 <= chapter <= self.book["chapters"]:
            self.chapter_no = chapter
        else:
            index = self.books.index(self.book) + (1 if delta > 0 else -1)
            if 0 <= index < len(self.books):
                self.book = self.books[index]
                self.chapter_no = 1 if delta > 0 else self.book["chapters"]
            else:
                print(style("Inicio/fin de la Biblia.", DIM))
                return
        self.entries = self.device.chapter(self.book, self.chapter_no)
        print_chapter(self.book, self.chapter_no, self.entries, self.width)

    # -- busqueda -----------------------------------------------------------
    def search(self, query, reset=True):
        if reset:
            self.last_query = query
            start = 0
            self.search_hits = 0
        else:
            start = self.search_next if self.search_next is not None else 0
        matches, next_chapter = self.device.search(query, start=start, limit=25)
        self.search_next = next_chapter
        if matches:
            self.search_hits += len(matches)
            print(style(f"«{query}» — {len(matches)} resultado(s)", BOLD))
            print_matches(query, matches, self.width)
            if next_chapter and next_chapter < self.total_chapters:
                print(style("  (pulsa / para ver mas)", DIM))
        elif self.search_hits == 0:
            print(style(f"«{query}» no aparece en el texto.", DIM))
            self.search_next = None
        else:
            print(style(f"No hay mas resultados ({self.search_hits} en total).", DIM))
            self.search_next = None
        print()

    # -- bucle --------------------------------------------------------------
    def run(self):
        print(style(f"Conectado a {self.device.port} — {len(self.books)} libros", GREEN))
        print(style("Escribe ? para la ayuda.", DIM))
        self.go(Reference(self.book, 1))
        while True:
            try:
                prompt = (f"{style(self.book['short'], CYAN)} "
                          f"{style(str(self.chapter_no), BOLD)}> ")
                entry = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not entry:
                self.move(1)
                continue
            command, _, rest = entry.partition(" ")
            rest = rest.strip()
            try:
                if command.lower() in ("q", "quit", "salir", "exit"):
                    break
                elif command in ("?", "h", "ayuda"):
                    print(HELP_TEXT)
                elif command.lower() in ("n", "s", "next"):
                    self.move(1)
                elif command.lower() in ("p", "a", "prev"):
                    self.move(-1)
                elif command.lower() == "r":
                    label, number, text = self.device.random()
                    print_verse(label.rsplit(":", 1)[0], number, text, self.width)
                elif command.lower() == "b":
                    self.print_books()
                elif command.lower() == "i":
                    self.print_info()
                elif command.lower() == "m":
                    self.add_bookmark()
                elif command.lower() == "ml":
                    self.list_bookmarks()
                elif command.lower() == "md":
                    self.remove_bookmark(rest)
                elif command.lower() == "w":
                    self.set_width(rest)
                elif command.startswith("/"):
                    query = entry[1:].strip()
                    if query:
                        self.search(query)
                    elif self.last_query:
                        self.search(self.last_query, reset=False)
                    else:
                        print(style("Escribe la palabra a buscar, por ejemplo: /amor", DIM))
                else:
                    reference = (parse_local_reference(entry, self.book)
                                 or parse_reference(entry, self.books))
                    if reference:
                        self.go(reference)
                    else:
                        print(style(f"No entiendo «{entry}». Escribe ? para la ayuda.", RED))
            except DeviceError as exc:
                print(style(f"error: {exc}", RED))

    # -- utilidades ---------------------------------------------------------
    def print_books(self):
        width = self.width
        columns = max(1, width // 22)
        cells = []
        for book in self.books:
            cells.append(f"{book['n']:>2} {book['short']:<6}{book['chapters']:>4}")
        for index in range(0, len(cells), columns):
            print("".join(cell.ljust(22) for cell in cells[index:index + columns]))
        print()

    def print_info(self):
        info = self.device.info()
        for key, value in info.items():
            print(f"  {style(key.ljust(18), DIM)} {value}")
        print()

    def add_bookmark(self):
        items = load_bookmarks()
        label = f"{self.book['short']} {self.chapter_no}"
        for item in items:
            if item["ref"] == label:
                print(style(f"{label} ya estaba en las marcas.", DIM))
                return
        items.append({"ref": label, "usfm": self.book["usfm"],
                      "chapter": self.chapter_no,
                      "added": time.strftime("%Y-%m-%d %H:%M")})
        save_bookmarks(items)
        print(style(f"Marca guardada: {label}", GREEN))

    def list_bookmarks(self):
        items = load_bookmarks()
        if not items:
            print(style("Sin marcas guardadas.", DIM))
            return
        for index, item in enumerate(items, start=1):
            print(f"  {index:>2}. {style(item['ref'].ljust(12), CYAN)} {item.get('added', '')}")
        print()

    def remove_bookmark(self, argument):
        items = load_bookmarks()
        if not argument.isdigit():
            print(style("uso: md <numero>", DIM))
            return
        index = int(argument) - 1
        if 0 <= index < len(items):
            removed = items.pop(index)
            save_bookmarks(items)
            print(style(f"Marca borrada: {removed['ref']}", GREEN))
        else:
            print(style("numero de marca invalido", RED))

    def set_width(self, argument):
        if argument.isdigit() and 40 <= int(argument) <= 300:
            self.width = int(argument)
            print(style(f"Ancho ajustado a {self.width} columnas.", GREEN))
        else:
            print(style(f"Ancho actual: {self.width}. uso: w 90", DIM))


# ---------------------------------------------------------------------------
# Comandos de linea de comandos
# ---------------------------------------------------------------------------
def command_info(device, args):
    info = device.info()
    for key, value in info.items():
        print(f"{key.ljust(18)} {value}")
    return 0


def command_books(device, args):
    books = device.books()
    for book in books:
        print(f"{book['n']:>2}  {book['usfm']:<4} {book['short']:<7} "
              f"{book['name']:<22} {book['chapters']:>3} cap.")
    return 0


def command_read(device, args, ref_text=None, verse_only=False):
    books = device.books()
    reference = parse_reference(ref_text or args.ref, books)
    if reference is None:
        print(style(f"referencia no valida: {ref_text or args.ref}", RED))
        return 2
    if verse_only or reference.verse:
        entries = device.chapter(reference.book, reference.chapter)
        end = reference.end or reference.verse or 0
        if reference.verse:
            found = False
            for kind, number, text in entries:
                if kind == "v" and reference.verse <= number <= end:
                    print_verse(f"{reference.book['short']} {reference.chapter}",
                                number, text, args.width)
                    found = True
            if not found:
                print(style(f"No existe {reference.label}", RED))
                return 3
            return 0
        print_chapter(reference.book, reference.chapter, entries, args.width)
        return 0

    entries = device.chapter(reference.book, reference.chapter)
    print_chapter(reference.book, reference.chapter, entries, args.width)
    return 0


def command_search(device, args):
    query = " ".join(args.query)
    start = 0
    total = 0
    limit = args.limit
    while True:
        matches, next_chapter = device.search(query, start=start, limit=limit)
        if matches:
            print_matches(query, matches, args.width)
        total += len(matches)
        # Un lote incompleto significa que ya se recorrió toda la Biblia.
        if len(matches) < limit or not args.all:
            break
        if next_chapter is None or next_chapter <= start:
            break
        start = next_chapter
    print()
    if total == 0:
        print(style(f"«{query}» no aparece en el texto.", DIM))
    else:
        print(style(f"{total} resultado(s).", DIM))
    return 0


def command_random(device, args):
    label, number, text = device.random()
    print_verse(label.rsplit(":", 1)[0], number, text, args.width)
    return 0


def command_upload(device, args):
    path = Path(args.file) if args.file else DEFAULT_DATA
    if not path.exists():
        print(style(f"No existe {path}. Genera el archivo con: "
                    f"python tools/build_bible.py", RED))
        return 2
    size = path.stat().st_size
    print(f"Enviando {path.name} ({size / 1048576:.2f} MB) a {device.port} ...")
    started = time.time()
    device.upload(path, progress=sys.stdout.isatty())
    elapsed = time.time() - started
    info = device.info()
    print(style(f"Listo en {elapsed:.1f} s. El dispositivo informa "
                f"{info.get('versiculos', '?')} versiculos.", GREEN))
    return 0


def command_ports(args):
    if list_ports is None:
        print(style("falta pyserial: pip install pyserial", RED))
        return 2
    for port in list_ports.comports():
        print(f"{port.device:<22} {port.description}")
    try:
        port = autodetect_port(args.baud)
        print(style(f"\nDispositivo de la Biblia detectado en {port}", GREEN))
    except DeviceError as exc:
        print(style(f"\n{exc}", DIM))
    return 0


def command_shell(device, args):
    reader = Reader(device, args.file)
    reader.run()
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
COMMAND_NAMES = {"shell", "info", "books", "random", "upload", "ports",
                 "read", "verse", "search"}
VALUE_OPTIONS = {"--port", "--baud", "--width", "--file", "--limit"}


def expand_reference_argv(argv):
    """Permite escribir 'biblia.py juan 3:16' sin subcomando."""
    positional = []
    options = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in VALUE_OPTIONS and index + 1 < len(argv):
            options.extend([item, argv[index + 1]])
            index += 2
            continue
        if item.startswith("--"):
            options.append(item)
            index += 1
            continue
        positional.append(item)
        index += 1

    if positional and positional[0] not in COMMAND_NAMES:
        reference = " ".join(positional)
        sub = "verse" if ":" in reference else "read"
        return options + [sub, reference]
    return argv


def build_parser():
    parser = argparse.ArgumentParser(
        description="Biblia Reina-Valera 1960 sobre ESP32 por puerto serie.")
    parser.add_argument("--port", help="puerto serie (por defecto se detecta solo)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--width", type=int, default=None, help="ancho de linea")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--file", help="archivo de datos (upload/shell)")
    parser.add_argument("--limit", type=int, default=40,
                        help="resultados por lote en las busquedas")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("shell", help="lector interactivo (por defecto)")
    sub.add_parser("info", help="informacion del dispositivo")
    sub.add_parser("books", help="lista de libros")
    sub.add_parser("random", help="versiculo aleatorio")
    sub.add_parser("upload", help="enviar data/bible.dat a la ESP32")
    sub.add_parser("ports", help="listar puertos serie")
    sub.add_parser("read", help="leer un capitulo").add_argument("ref")
    sub.add_parser("verse", help="leer un versiculo").add_argument("ref")
    search = sub.add_parser("search", help="buscar texto en la Biblia")
    search.add_argument("query", nargs="+")
    search.add_argument("--all", action="store_true", help="recorrer todos los resultados")
    search.add_argument("--limit", type=int, default=argparse.SUPPRESS,
                        help="resultados por lote (por defecto 40)")
    return parser


def main(argv=None):
    global COLOR_ENABLED

    argv = list(argv if argv is not None else sys.argv[1:])
    argv = expand_reference_argv(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.width is None:
        args.width = terminal_width()
    COLOR_ENABLED = not args.no_color and sys.stdout.isatty()

    if args.command == "ports":
        return command_ports(args)

    try:
        device = Device(args.port, args.baud).open()
    except DeviceError as exc:
        print(style(f"error: {exc}", RED))
        return 1

    try:
        if args.command in (None, "shell"):
            return command_shell(device, args)
        if args.command == "info":
            return command_info(device, args)
        if args.command == "books":
            return command_books(device, args)
        if args.command == "read":
            return command_read(device, args)
        if args.command == "verse":
            return command_read(device, args, verse_only=True)
        if args.command == "search":
            return command_search(device, args)
        if args.command == "random":
            return command_random(device, args)
        if args.command == "upload":
            return command_upload(device, args)
        parser.print_help()
        return 2
    except DeviceError as exc:
        print(style(f"error: {exc}", RED))
        return 1
    finally:
        device.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Ocurre al canalizar la salida a head/less.
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)
