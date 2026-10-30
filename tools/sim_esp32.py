#!/usr/bin/env python3
"""
sim_esp32.py - Simulador del firmware de la ESP32 sobre un pseudo-terminal.

Sirve para probar el cliente (tools/biblia.py) sin tener la placa conectada.
Crea un PTY, imprime su ruta y atiende los mismos comandos que el firmware
leyendo directamente data/bible.dat.

    python tools/sim_esp32.py
    # en otra terminal:
    python tools/biblia.py --port /dev/pts/5

Opciones utiles:
    --selftest     ejecuta comprobaciones internas y termina
    --chunk-delay  retardo artificial por bloque (para ver la barra de carga)
"""

import argparse
import os
import pty
import select
import struct
import sys
import tty
import unicodedata
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "bible.dat"
FIND_LIMIT = 40
LINE_BUFFER = 512

HEADER = struct.Struct("<8sHHHHIIIIIHHI")
BOOK_ENTRY = struct.Struct("<40s8s12sHH")
CHAPTER_ENTRY = struct.Struct("<BBHIHHI")


def fold(text):
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


class Bible:
    """Lectura del archivo binario generado por build_bible.py."""

    def __init__(self, path):
        self.path = Path(path)
        self.blob = self.path.read_bytes()
        (magic, self.version, _flags, self.book_count, self.chapter_count,
         self.verse_count, book_off, chap_off, num_off, data_off, self.max_raw,
         self.max_comp, self.total_size) = HEADER.unpack_from(self.blob, 0)
        if not magic.startswith(b"RVR1960"):
            raise ValueError("el archivo no es una base RVR1960")
        self.num_off = num_off

        self.books = []
        for index in range(self.book_count):
            (name, usfm, short, chapters, first) = BOOK_ENTRY.unpack_from(
                self.blob, book_off + index * BOOK_ENTRY.size)
            self.books.append({
                "n": index + 1,
                "usfm": usfm.split(b"\0")[0].decode("utf-8"),
                "short": short.split(b"\0")[0].decode("utf-8"),
                "name": name.split(b"\0")[0].decode("utf-8"),
                "chapters": chapters,
                "first": first,
            })

        self.chapters = []
        for index in range(self.chapter_count):
            (book, number, verses, offset, comp_len, raw_len, numbers) = (
                CHAPTER_ENTRY.unpack_from(self.blob, chap_off + index * CHAPTER_ENTRY.size))
            self.chapters.append({
                "index": index,
                "book": book,
                "number": number,
                "verses": verses,
                "offset": offset,
                "comp_len": comp_len,
                "raw_len": raw_len,
                "numbers": numbers,
            })

    # -- utilidades ---------------------------------------------------------
    def chapter_lines(self, chapter_index):
        ref = self.chapters[chapter_index]
        raw = zlib.decompress(self.blob[ref["offset"]:ref["offset"] + ref["comp_len"]])
        text = raw.decode("utf-8")
        explicit = None
        if ref["numbers"]:
            start = self.num_off + ref["numbers"]
            explicit = list(self.blob[start:start + ref["verses"]])
        entries = []
        verse = 0
        for line in text.split("\n"):
            if line.startswith("# "):
                entries.append(("h", 0, line[2:]))
            elif line.startswith("~ "):
                entries.append(("~", 0, line[2:]))
            else:
                if explicit is not None:
                    number = explicit[verse] if verse < len(explicit) else verse + 1
                else:
                    number = verse + 1
                verse += 1
                entries.append(("v", number, line))
        return entries

    def find_book(self, query):
        text = query.strip()
        if text.isdigit():
            value = int(text)
            return self.books[value - 1] if 1 <= value <= len(self.books) else None
        key = fold(text)
        compact = key.replace(" ", "")
        for book in self.books:
            if compact in (fold(book["usfm"]),
                           fold(book["short"]).replace(" ", ""),
                           fold(book["name"]).replace(" ", "")):
                return book
        for book in self.books:
            if len(key) >= 2 and fold(book["name"]).startswith(key):
                return book
        for book in self.books:
            if len(key) >= 3 and key in fold(book["name"]).split():
                return book
        return None

    def ref_label(self, chapter_index, verse=0):
        chapter = self.chapters[chapter_index]
        book = self.books[chapter["book"]]
        if verse:
            return f"{book['short']} {chapter['number']}:{verse}"
        return f"{book['short']} {chapter['number']}"

    def search(self, query, start, limit):
        needle = fold(query)
        results = []
        for index in range(max(0, start), self.chapter_count):
            for kind, number, text in self.chapter_lines(index):
                if kind != "v":
                    continue
                if needle in fold(text):
                    results.append((self.ref_label(index, number), text))
                    if len(results) >= limit:
                        return results, index + 1
        return results, self.chapter_count


class Simulator:
    def __init__(self, bible, verbose=False, upload_path=None, chunk_delay=0.0):
        self.bible = bible
        self.verbose = verbose
        self.upload_path = Path(upload_path) if upload_path else bible.path
        self.chunk_delay = chunk_delay
        self.upload_file = None
        self.upload_size = 0
        self.upload_written = 0
        self.buffer = bytearray()

    # -- protocolo ----------------------------------------------------------
    def handle(self, line):
        if self.verbose:
            print(f"<< {line}", file=sys.stderr)
        parts = line.split()
        if not parts:
            return []
        command = parts[0].lower()
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            return [f"[ERR] cmd comando desconocido"]
        return handler(line, parts)

    def cmd_help(self, line, parts):
        return [
            "i|comandos|ping info books chap verse find rand",
            "i|lectura|chap <libro> [capitulo] | verse <libro> <capitulo>:<versiculo>",
            "i|busqueda|find <texto> | find <desde> <limite> <texto>",
            "i|carga|putbegin <bytes> / put <offset> <hex> / putend <bytes>",
            "[OK]",
        ]

    def cmd_ping(self, line, parts):
        return ["[OK] pong"]

    def cmd_info(self, line, parts):
        bible = self.bible
        return [
            "i|nombre|Biblia Reina-Valera 1960",
            "i|formato|RVR1960 v1",
            f"i|libros|{bible.book_count}",
            f"i|capitulos|{bible.chapter_count}",
            f"i|versiculos|{bible.verse_count}",
            "i|almacenamiento|simulador",
            f"i|archivo_bytes|{bible.total_size}",
            "i|libre_bytes|0",
            "[OK]",
        ]

    def cmd_books(self, line, parts):
        out = [f"b|{b['n']}|{b['usfm']}|{b['short']}|{b['name']}|{b['chapters']}"
               for b in self.bible.books]
        out.append(f"[OK] {self.bible.book_count}")
        return out

    def cmd_chap(self, line, parts):
        return self._chapter_command(parts, verse_mode=False)

    def cmd_verse(self, line, parts):
        return self._chapter_command(parts, verse_mode=True)

    def _chapter_command(self, parts, verse_mode):
        if len(parts) < 2:
            return ["[ERR] sintaxis uso: chap <libro> [capitulo]"]
        book = self.bible.find_book(parts[1])
        if book is None:
            return ["[ERR] nobook libro no reconocido"]
        chapter = 1
        verse = 0
        if len(parts) >= 3:
            token = parts[2]
            if ":" in token:
                token, _, verse_text = token.partition(":")
                verse = int(verse_text or 0)
            if not token.isdigit():
                return ["[ERR] sintaxis numero de capitulo invalido"]
            chapter = int(token)
            if verse_mode and len(parts) >= 4:
                verse = int(parts[3])
        if verse_mode and verse == 0:
            return ["[ERR] sintaxis uso: verse <libro> <capitulo>:<versiculo>"]
        if chapter < 1 or chapter > book["chapters"]:
            return ["[ERR] nochap capitulo inexistente"]
        index = book["first"] + chapter - 1
        entries = self.bible.chapter_lines(index)
        out = []
        found = False
        for kind, number, text in entries:
            if verse_mode and (kind != "v" or number != verse):
                continue
            if verse_mode:
                found = True
            if kind == "v":
                out.append(f"v|{number}|{text}")
            elif kind == "h":
                out.append(f"h|{text}")
            else:
                out.append(f"~|{text}")
        if verse_mode and not found:
            return ["[ERR] noverse el versiculo no existe en ese capitulo"]
        label = self.bible.ref_label(index, verse if verse_mode else 0)
        out.append(f"[OK] {label}")
        return out

    def cmd_rand(self, line, parts):
        import random

        index = random.randrange(self.bible.chapter_count)
        verses = [entry for entry in self.bible.chapter_lines(index) if entry[0] == "v"]
        kind, number, text = random.choice(verses)
        return [f"v|{number}|{text}", f"[OK] {self.bible.ref_label(index, number)}"]

    def cmd_find(self, line, parts):
        if len(parts) < 2:
            return ["[ERR] sintaxis uso: find <texto>"]
        start = 0
        limit = FIND_LIMIT
        query_tokens = parts[1:]
        if parts[1].isdigit():
            start = int(parts[1])
            query_tokens = parts[2:]
            if len(parts) >= 3 and parts[2].isdigit():
                limit = int(parts[2])
                query_tokens = parts[3:]
        query = " ".join(query_tokens)
        if not query:
            return ["[ERR] sintaxis falta el texto a buscar"]
        results, next_chapter = self.bible.search(query, start, limit)
        out = [f"m|{ref}|{text}" for ref, text in results]
        out.append(f"[OK] {next_chapter} {len(results)}")
        return out

    def cmd_putbegin(self, line, parts):
        if len(parts) < 2 or not parts[1].isdigit():
            return ["[ERR] sintaxis uso: putbegin <bytes>"]
        self.upload_size = int(parts[1])
        self.upload_written = 0
        self.upload_file = self.upload_path.open("wb")
        return [f"[OK] {self.upload_size}"]

    def cmd_put(self, line, parts):
        if self.upload_file is None:
            return ["[ERR] write no hay carga en curso"]
        if len(parts) < 3 or not parts[1].isdigit():
            return ["[ERR] sintaxis uso: put <offset> <hex>"]
        offset = int(parts[1])
        if offset != self.upload_written:
            return ["[ERR] write offset inesperado"]
        try:
            data = bytes.fromhex(parts[2])
        except ValueError:
            return ["[ERR] sintaxis datos hex invalidos"]
        self.upload_file.write(data)
        self.upload_written += len(data)
        return [f"[OK] {self.upload_written}"]

    def cmd_putend(self, line, parts):
        if self.upload_file is None:
            return ["[ERR] write no hay carga en curso"]
        self.upload_file.close()
        self.upload_file = None
        expected = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else self.upload_size
        if expected and self.upload_written != expected:
            return ["[ERR] write la carga quedo incompleta"]
        try:
            self.bible = Bible(self.upload_path)
        except Exception as exc:  # pragma: no cover
            return [f"[ERR] io {exc}"]
        return [f"[OK] {self.bible.total_size}"]


def serve(simulator, master_fd):
    buffer = bytearray()
    while True:
        try:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            return
        if not data:
            return
        buffer.extend(data)
        while b"\n" in buffer:
            raw, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            line = raw.decode("utf-8", errors="replace").replace("\r", "")
            if not line.strip():
                continue
            response = simulator.handle(line)
            payload = ("\n".join(response) + "\n").encode("utf-8")
            os.write(master_fd, payload)
            if simulator.chunk_delay:
                import time

                time.sleep(simulator.chunk_delay)


def selftest(data_path):
    bible = Bible(data_path)
    print(f"libros={bible.book_count} capitulos={bible.chapter_count} "
          f"versiculos={bible.verse_count}")
    book = bible.find_book("juan")
    assert book and book["usfm"] == "JHN", book
    index = book["first"] + 2
    entries = {number: text for kind, number, text in bible.chapter_lines(index)
               if kind == "v"}
    print("Juan 3:16 ->", entries[16])
    assert "vida eterna" in entries[16]
    psalm = bible.find_book("salmos")
    entries = {number: text for kind, number, text in bible.chapter_lines(psalm["first"] + 22)
               if kind == "v"}
    print("Salmo 23:1 ->", entries[1])
    assert "pastor" in entries[1]
    results, _ = bible.search("pastor", 0, 5)
    print(f"busqueda 'pastor': {len(results)} resultados, primero: {results[0][0]}")
    assert results
    results, _ = bible.search("corazon", 0, 3)
    print(f"busqueda 'corazon' (sin tilde): {[ref for ref, _ in results]}")
    assert results
    print("selftest: OK")


def main():
    parser = argparse.ArgumentParser(description="Simulador de la ESP32 para pruebas.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--verbose", action="store_true", help="mostrar comandos")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--upload-path", type=Path,
                        help="archivo donde se guardan las cargas de prueba")
    parser.add_argument("--chunk-delay", type=float, default=0.0)
    args = parser.parse_args()

    if not args.data.exists():
        print(f"error: no existe {args.data}; ejecuta tools/build_bible.py",
              file=sys.stderr)
        return 2
    if args.selftest:
        selftest(args.data)
        return 0

    bible = Bible(args.data)
    simulator = Simulator(bible, verbose=args.verbose,
                          upload_path=args.upload_path or args.data,
                          chunk_delay=args.chunk_delay)

    master_fd, slave_fd = pty.openpty()
    tty.setraw(slave_fd)
    tty.setraw(master_fd)
    path = os.ttyname(slave_fd)
    print(f"Simulador de la Biblia en {path}", flush=True)
    print(f"  python tools/biblia.py --port {path}", flush=True)
    print(f"  {bible.book_count} libros / {bible.chapter_count} capitulos / "
          f"{bible.verse_count} versiculos", flush=True)
    print("Ctrl+C para terminar.", flush=True)
    try:
        serve(simulator, master_fd)
    except KeyboardInterrupt:
        print()
    finally:
        os.close(master_fd)
        os.close(slave_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
