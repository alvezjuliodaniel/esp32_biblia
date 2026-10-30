#!/usr/bin/env python3
"""
build_bible.py - Convierte el JSON de la Reina-Valera 1960 en la base binaria
que usa la ESP32 (data/bible.dat).

Formato generado (little endian), version 1:

  CABECERA (64 bytes)
     0  char magic[8]      "RVR1960\\0"
     8  u16  version       1
    10  u16  flags         0
    12  u16  book_count
    14  u16  chapter_count
    16  u32  verse_count
    20  u32  book_table_off
    24  u32  chap_table_off
    28  u32  num_table_off
    32  u32  data_off
    36  u16  max_raw_len   (capitulo mas grande ya descomprimido)
    38  u16  max_comp_len  (capitulo mas grande comprimido)
    40  u32  total_size
    44..     reservado

  LIBRO (64 bytes)
     0  char name[40]      nombre largo UTF-8 ("Génesis")
    40  char usfm[8]       codigo USFM ("GEN")
    48  char short[12]     abreviatura en espanol ("Gn")
    60  u16  chapter_count
    62  u16  first_chapter indice global del primer capitulo

  CAPITULO (16 bytes)
     0  u8   book_idx
     1  u8   chapter_no    (1..150)
     2  u16  verse_count
     4  u32  offset        absoluto en el archivo
     8  u16  comp_len
    10  u16  raw_len
    12  u32  num_off       (0 => numeros correlativos 1..verse_count)

  SECCION NUM_TABLE
     Para los capitulos donde la numeracion no es correlativa (hay versiculos
     omitidos o repetidos) se guarda un u8 por versiculo con su numero real.

  SECCION DATA
     Cada capitulo es un bloque deflate (zlib). Una vez descomprimido es texto
     UTF-8 con un versiculo por linea. Las lineas que empiezan con "# " son
     titulos de seccion y las que empiezan con "~ " son etiquetas (p.ej. "Alef").
"""

import argparse
import json
import re
import struct
import sys
import zlib
from pathlib import Path

MAGIC = b"RVR1960\x00"
FORMAT_VERSION = 1
HEADER_SIZE = 64
BOOK_ENTRY_SIZE = 64
CHAPTER_ENTRY_SIZE = 16

HEADING_PREFIX = "# "
LABEL_PREFIX = "~ "

# Abreviaturas usadas en espanol para cada libro (orden canonico).
SPANISH_SHORT = {
    "GEN": "Gn", "EXO": "Ex", "LEV": "Lv", "NUM": "Nm", "DEU": "Dt",
    "JOS": "Jos", "JDG": "Jc", "RUT": "Rt", "1SA": "1 S", "2SA": "2 S",
    "1KI": "1 R", "2KI": "2 R", "1CH": "1 Cr", "2CH": "2 Cr", "EZR": "Esd",
    "NEH": "Neh", "EST": "Est", "JOB": "Job", "PSA": "Sal", "PRO": "Pr",
    "ECC": "Ec", "SNG": "Cnt", "ISA": "Is", "JER": "Jer", "LAM": "Lm",
    "EZK": "Ez", "DAN": "Dn", "HOS": "Os", "JOL": "Jl", "AMO": "Am",
    "OBA": "Abd", "JON": "Jon", "MIC": "Miq", "NAM": "Nah", "HAB": "Hab",
    "ZEP": "Sof", "HAG": "Hag", "ZEC": "Zac", "MAL": "Mal", "MAT": "Mt",
    "MRK": "Mr", "LUK": "Lc", "JHN": "Jn", "ACT": "Hch", "ROM": "Ro",
    "1CO": "1 Co", "2CO": "2 Co", "GAL": "Gá", "EPH": "Ef", "PHP": "Fil",
    "COL": "Col", "1TH": "1 Ts", "2TH": "2 Ts", "1TI": "1 Ti", "2TI": "2 Ti",
    "TIT": "Tit", "PHM": "Flm", "HEB": "He", "JAS": "Stg", "1PE": "1 P",
    "2PE": "2 P", "1JN": "1 Jn", "2JN": "2 Jn", "3JN": "3 Jn", "JUD": "Jud",
    "REV": "Ap",
}

# Tipos de item del JSON que se convierten en titulos o etiquetas.
HEADING_TYPES = {"heading1", "heading2", "heading3", "section1", "section2"}
LABEL_TYPES = {"label"}


def clean_text(text: str) -> str:
    """Normaliza espacios y saltos de linea de un texto del JSON."""
    return " ".join(text.replace("\u00a0", " ").split())


def clean_book_name(name: str) -> str:
    """Quita el prefijo de santidad ("S. Juan" -> "Juan")."""
    return re.sub(r"^S\.\s*", "", clean_text(name))


def chapter_lines(chapter: dict):
    """Devuelve (lineas_de_texto, numeros_de_versiculo) de un capitulo."""
    lines = []
    numbers = []
    for item in chapter["items"]:
        kind = item["type"]
        text = clean_text(" ".join(item.get("lines") or []))
        if kind in HEADING_TYPES:
            if text:
                lines.append(HEADING_PREFIX + text)
        elif kind in LABEL_TYPES:
            if text:
                lines.append(LABEL_PREFIX + text)
        elif kind == "verse":
            if not text:
                continue
            lines.append(text)
            for number in item["verse_numbers"] or [0]:
                numbers.append(number)
    return lines, numbers


def is_sequential(numbers):
    return numbers == list(range(1, len(numbers) + 1))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Genera data/bible.dat a partir del JSON de la RVR1960.")
    parser.add_argument("--input", type=Path,
                        default=root / "RVR1960_vid_149.json",
                        help="JSON de origen (por defecto el de la carpeta del proyecto)")
    parser.add_argument("--output", type=Path, default=root / "data" / "bible.dat",
                        help="archivo binario de salida")
    parser.add_argument("--level", type=int, default=9,
                        help="nivel de compresion deflate (1-9, por defecto 9)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: no existe el archivo {args.input}", file=sys.stderr)
        return 1

    data = json.loads(args.input.read_text(encoding="utf-8"))
    books = data["books"]

    book_table = bytearray()
    chapter_table = bytearray()
    numbers_section = bytearray()
    compressed_blocks = bytearray()

    chapter_count = 0
    verse_count = 0
    max_raw = 0
    max_comp = 0
    max_raw_ref = ""
    non_sequential = []
    book_rows = []

    # La cabecera y las tablas van antes de los datos: primero se calculan las
    # posiciones y luego se compone el archivo final.
    seen_chapters = set()
    for book_index, book in enumerate(books):
        chapters = book["chapters"]
        book_row = {
            "usfm": book["usfm"],
            "name": clean_book_name(book["human"]),
            "short": SPANISH_SHORT.get(book["usfm"], book["usfm"].title()),
            "chapters": 0,
            "first_chapter": chapter_count,
        }
        book_rows.append(book_row)

        for chapter_no, chapter in enumerate(chapters, start=1):
            # Los libros de un solo capitulo vienen duplicados en el JSON:
            # se descarta la segunda copia para no repetir el texto.
            if chapter["usfm"] in seen_chapters:
                continue
            seen_chapters.add(chapter["usfm"])
            lines, numbers = chapter_lines(chapter)
            if not numbers:
                continue
            raw = "\n".join(lines).encode("utf-8")
            comp = zlib.compress(raw, args.level)

            number_offset = 0
            if numbers and not is_sequential(numbers) and max(numbers) < 256:
                number_offset = len(numbers_section)
                numbers_section.extend(bytes(numbers))
                non_sequential.append((chapter["usfm"], numbers))
            elif numbers and max(numbers) >= 256:
                raise ValueError(f"numeracion fuera de rango en {chapter['usfm']}")

            chapter_table.extend(struct.pack(
                "<BBHIHHI",
                book_index,
                chapter_no,
                len(numbers),
                0,               # offset: se rellena despues
                len(comp),
                len(raw),
                number_offset,
            ))

            compressed_blocks.extend(comp)
            chapter_count += 1
            book_row["chapters"] += 1
            verse_count += len(numbers)
            if len(raw) > max_raw:
                max_raw, max_raw_ref = len(raw), chapter["usfm"]
            max_comp = max(max_comp, len(comp))

    # Tabla de libros
    for row in book_rows:
        name = row["name"].encode("utf-8")[:39]
        usfm = row["usfm"].encode("ascii")[:7]
        short = row["short"].encode("utf-8")[:11]
        book_table.extend(name.ljust(40, b"\x00"))
        book_table.extend(usfm.ljust(8, b"\x00"))
        book_table.extend(short.ljust(12, b"\x00"))
        book_table.extend(struct.pack("<HH", row["chapters"], row["first_chapter"]))

    book_table_off = HEADER_SIZE
    chap_table_off = book_table_off + len(book_table)
    num_table_off = chap_table_off + len(chapter_table)
    data_off = num_table_off + len(numbers_section)

    # Completa los offsets absolutos de cada capitulo (segundo campo u32).
    table = bytearray(chapter_table)
    running = data_off
    for index in range(chapter_count):
        base = index * CHAPTER_ENTRY_SIZE
        comp_len = struct.unpack_from("<H", table, base + 8)[0]
        struct.pack_into("<I", table, base + 4, running)
        running += comp_len

    total_size = data_off + len(compressed_blocks)

    header = bytearray(HEADER_SIZE)
    header[0:8] = MAGIC
    struct.pack_into("<HHHHIIIIIHHI", header, 8,
                     FORMAT_VERSION, 0, len(books), chapter_count, verse_count,
                     book_table_off, chap_table_off, num_table_off, data_off,
                     max_raw, max_comp, total_size)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as out:
        out.write(header)
        out.write(book_table)
        out.write(table)
        out.write(numbers_section)
        out.write(compressed_blocks)

    raw_total = sum(
        len("\n".join(chapter_lines(c)[0]).encode("utf-8"))
        for b in books for c in b["chapters"])

    print(f"origen      : {args.input.name} ({args.input.stat().st_size / 1048576:.1f} MB)")
    print(f"salida      : {args.output} ({total_size / 1048576:.2f} MB)")
    print(f"libros      : {len(books)}")
    print(f"capitulos   : {chapter_count}")
    print(f"versiculos  : {verse_count}")
    print(f"texto plano : {raw_total / 1048576:.2f} MB -> comprimido "
          f"{len(compressed_blocks) / 1048576:.2f} MB "
          f"({100 * len(compressed_blocks) / raw_total:.0f} %)")
    print(f"tablas      : libros {len(book_table)} B, capitulos {len(table)} B, "
          f"numeracion {len(numbers_section)} B")
    print(f"cap. mayor  : {max_raw_ref} = {max_raw} B descomprimido, {max_comp} B comprimido")
    if non_sequential:
        detalles = ", ".join(usfm for usfm, _ in non_sequential)
        print(f"numeracion  : {len(non_sequential)} capitulos con numeracion irregular "
              f"({detalles})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
