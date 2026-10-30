// Implementacion del protocolo serie.
//
// Cada respuesta se compone de lineas de datos y termina con una linea de
// estado: "[OK]" o "[ERR] <codigo> <mensaje>".
//
// Lineas de datos:
//   i|clave|valor        informacion del dispositivo
//   b|n|usfm|corto|nombre|capitulos
//   h|texto              titulo de seccion
//   ~|texto              etiqueta (por ejemplo "Alef")
//   v|numero|texto       versiculo
//   m|referencia|texto   resultado de busqueda
#include "biblia_protocol.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "biblia_config.h"

namespace {

BibleStore *g_store = nullptr;

char g_line[BIB_LINE_BUFFER];
char g_rawLine[BIB_LINE_BUFFER];  // copia intacta para los argumentos de texto
size_t g_lineLen = 0;

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

void replyOk() { Serial.print(F("[OK]\n")); }

void replyOk(const char *detail) {
  Serial.print(F("[OK] "));
  Serial.print(detail);
  Serial.print('\n');
}

void replyOkNumber(long value) {
  Serial.print(F("[OK] "));
  Serial.println(value);
}

void replyError(const char *code, const char *message) {
  Serial.print(F("[ERR] "));
  Serial.print(code);
  Serial.print(' ');
  Serial.println(message);
}

void replyNotReady() {
  const char *message = g_store->error();
  replyError("notready", (message && *message) ? message : "sin datos cargados");
}

bool isNumber(const char *text) {
  if (!text || !*text) {
    return false;
  }
  for (const char *p = text; *p; ++p) {
    if (!isdigit((unsigned char)*p)) {
      return false;
    }
  }
  return true;
}

// Divide 'text' en tokens separados por espacios (modifica el buffer).
// Devuelve cuantos tokens se guardaron.
int tokenize(char *text, char **tokens, int maxTokens) {
  int count = 0;
  char *p = text;
  while (*p && count < maxTokens) {
    while (*p == ' ' || *p == '\t') {
      ++p;
    }
    if (!*p) {
      break;
    }
    tokens[count++] = p;
    while (*p && *p != ' ' && *p != '\t') {
      ++p;
    }
    if (*p) {
      *p++ = '\0';
    }
  }
  return count;
}

// Devuelve el puntero al inicio del token numero 'index' sin modificar nada.
const char *skipTokens(const char *text, int index) {
  const char *p = text;
  for (int i = 0; i < index && *p; ++i) {
    while (*p && *p != ' ' && *p != '\t') {
      ++p;
    }
    while (*p == ' ' || *p == '\t') {
      ++p;
    }
  }
  return p;
}

void printBookRef(uint16_t chapterIndex, uint16_t verse) {
  char ref[BIB_REF_LENGTH];
  g_store->formatRef(ref, sizeof(ref), chapterIndex, verse);
  Serial.print(ref);
}

// ---------------------------------------------------------------------------
// Comandos de lectura
// ---------------------------------------------------------------------------

void cmdInfo() {
  Serial.print(F("i|nombre|Biblia Reina-Valera 1960\n"));
  Serial.print(F("i|formato|RVR1960 v1\n"));
  Serial.print(F("i|libros|"));
  Serial.println(g_store->bookCount());
  Serial.print(F("i|capitulos|"));
  Serial.println(g_store->chapterCount());
  Serial.print(F("i|versiculos|"));
  Serial.println(g_store->verseCount());
  Serial.print(F("i|almacenamiento|"));
  Serial.println(g_store->source());
  Serial.print(F("i|archivo_bytes|"));
  Serial.println(g_store->fileSize());
  Serial.print(F("i|heap_libre|"));
  Serial.println((unsigned long)(ESP.getFreeHeap()));
  replyOk();
}

void cmdBooks() {
  for (uint16_t i = 0; i < g_store->bookCount(); ++i) {
    const BibleBook &book = g_store->book(i);
    Serial.print(F("b|"));
    Serial.print(i + 1);
    Serial.print('|');
    Serial.print(book.usfm);
    Serial.print('|');
    Serial.print(book.shortName);
    Serial.print('|');
    Serial.print(book.name);
    Serial.print('|');
    Serial.println(book.chapters);
  }
  replyOkNumber(g_store->bookCount());
}

// Imprime el capitulo ya descomprimido en el buffer interno.
void printCurrentChapter(bool onlyVerse, uint16_t verse) {
  bool found = false;
  for (uint16_t line = 0; line < g_store->currentLineCount(); ++line) {
    const BibleLineKind kind = g_store->currentLineKind(line);
    if (onlyVerse) {
      if (kind != BIBLE_LINE_VERSE || g_store->currentLineVerse(line) != verse) {
        continue;
      }
      found = true;
    }
    switch (kind) {
      case BIBLE_LINE_HEADING:
        Serial.print(F("h|"));
        break;
      case BIBLE_LINE_LABEL:
        Serial.print(F("~|"));
        break;
      default:
        Serial.print(F("v|"));
        Serial.print(g_store->currentLineVerse(line));
        Serial.print('|');
        break;
    }
    Serial.println(g_store->currentLine(line));
  }
  if (onlyVerse && !found) {
    replyError("noverse", "el versiculo no existe en ese capitulo");
    return;
  }
  char ref[BIB_REF_LENGTH];
  g_store->formatRef(ref, sizeof(ref), g_store->currentChapterIndex(),
                     onlyVerse ? verse : 0);
  replyOk(ref);
}

void handleChapterCommand(char **tokens, int count, bool verseMode) {
  if (!g_store->ready()) {
    replyNotReady();
    return;
  }
  if (count < 2) {
    replyError("sintaxis", verseMode ? "uso: verse <libro> <capitulo>:<versiculo>"
                                     : "uso: chap <libro> [capitulo]");
    return;
  }
  const int bookIndex = g_store->findBook(tokens[1]);
  if (bookIndex < 0) {
    replyError("nobook", "libro no reconocido");
    return;
  }

  uint16_t chapter = 1;
  uint16_t verse = 0;
  if (count >= 3) {
    // Acepta "3" o "3:16".
    char *colon = strchr(tokens[2], ':');
    if (colon) {
      *colon = '\0';
      verse = (uint16_t)atoi(colon + 1);
    }
    if (!isNumber(tokens[2]) || *tokens[2] == '\0') {
      replyError("sintaxis", "numero de capitulo invalido");
      return;
    }
    chapter = (uint16_t)atoi(tokens[2]);
    if (verseMode && count >= 4) {
      verse = (uint16_t)atoi(tokens[3]);
    }
  }
  if (verseMode && verse == 0) {
    replyError("sintaxis", "uso: verse <libro> <capitulo>:<versiculo>");
    return;
  }

  const int index = g_store->chapterIndex((uint8_t)bookIndex, chapter);
  if (index < 0) {
    replyError("nochap", "capitulo inexistente");
    return;
  }
  if (!g_store->openChapter((uint16_t)index)) {
    replyError("io", "no se pudo leer el capitulo");
    return;
  }
  printCurrentChapter(verseMode, verse);
}

void cmdRandom() {
  if (!g_store->ready()) {
    replyNotReady();
    return;
  }
  const uint16_t chapter = (uint16_t)random(0, g_store->chapterCount());
  if (!g_store->openChapter(chapter)) {
    replyError("io", "no se pudo leer el capitulo");
    return;
  }
  // Se busca hacia adelante el primer versiculo a partir de la linea elegida.
  const uint16_t start = (uint16_t)random(0, g_store->currentLineCount());
  int line = -1;
  for (uint16_t i = start; i < g_store->currentLineCount(); ++i) {
    if (g_store->currentLineKind(i) == BIBLE_LINE_VERSE) {
      line = (int)i;
      break;
    }
  }
  if (line < 0) {
    printCurrentChapter(false, 0);
    return;
  }
  Serial.print(F("v|"));
  Serial.print(g_store->currentLineVerse((uint16_t)line));
  Serial.print('|');
  Serial.println(g_store->currentLine((uint16_t)line));
  char ref[BIB_REF_LENGTH];
  g_store->formatRef(ref, sizeof(ref), chapter, g_store->currentLineVerse((uint16_t)line));
  replyOk(ref);
}

void searchCallback(void *ctx, uint16_t chapterIndex, uint16_t verse,
                    const char *text) {
  (void)ctx;
  Serial.print(F("m|"));
  printBookRef(chapterIndex, verse);
  Serial.print('|');
  Serial.println(text);
}

void cmdFind(char **tokens, int count, const char *rawLine) {
  if (!g_store->ready()) {
    replyNotReady();
    return;
  }
  if (count < 2) {
    replyError("sintaxis", "uso: find <texto>  |  find <desde> <limite> <texto>");
    return;
  }

  uint16_t from = 0;
  uint16_t limit = BIB_FIND_LIMIT;
  const char *query = skipTokens(rawLine, 1);

  if (isNumber(tokens[1])) {
    from = (uint16_t)atoi(tokens[1]);
    if (count >= 3 && isNumber(tokens[2])) {
      limit = (uint16_t)atoi(tokens[2]);
      query = skipTokens(rawLine, 3);
    } else {
      query = skipTokens(rawLine, 2);
    }
  }
  if (!query || !*query) {
    replyError("sintaxis", "falta el texto a buscar");
    return;
  }

  uint16_t next = g_store->chapterCount();
  const uint16_t matches =
      g_store->search(query, from, limit, searchCallback, nullptr, &next);
  Serial.print(F("[OK] "));
  Serial.print(next);
  Serial.print(' ');
  Serial.println(matches);
}

// ---------------------------------------------------------------------------
// Carga del archivo de datos por el mismo puerto serie
// ---------------------------------------------------------------------------

int hexValue(char c) {
  if (c >= '0' && c <= '9') {
    return c - '0';
  }
  if (c >= 'a' && c <= 'f') {
    return c - 'a' + 10;
  }
  if (c >= 'A' && c <= 'F') {
    return c - 'A' + 10;
  }
  return -1;
}

uint8_t g_chunk[256];

void cmdPutBegin(char **tokens, int count) {
  if (count < 2 || !isNumber(tokens[1])) {
    replyError("sintaxis", "uso: putbegin <bytes>");
    return;
  }
  const uint32_t size = (uint32_t)strtoul(tokens[1], nullptr, 10);
  if (!g_store->uploadBegin(size)) {
    replyNotReady();
    return;
  }
  replyOkNumber(size);
}

void cmdPut(char **tokens, int count) {
  if (count < 3 || !isNumber(tokens[1])) {
    replyError("sintaxis", "uso: put <offset> <hex>");
    return;
  }
  const uint32_t offset = (uint32_t)strtoul(tokens[1], nullptr, 10);
  const char *hex = tokens[2];
  const size_t hexLen = strlen(hex);
  if (hexLen == 0 || (hexLen & 1) != 0 || hexLen / 2 > sizeof(g_chunk)) {
    replyError("sintaxis", "datos hex invalidos");
    return;
  }
  const size_t len = hexLen / 2;
  for (size_t i = 0; i < len; ++i) {
    const int high = hexValue(hex[i * 2]);
    const int low = hexValue(hex[i * 2 + 1]);
    if (high < 0 || low < 0) {
      replyError("sintaxis", "datos hex invalidos");
      return;
    }
    g_chunk[i] = (uint8_t)((high << 4) | low);
  }
  if (!g_store->uploadChunk(offset, g_chunk, len)) {
    replyError("write", "no se pudo escribir el bloque");
    return;
  }
  replyOkNumber((long)g_store->uploadWritten());
}

void cmdPutEnd(char **tokens, int count) {
  const uint32_t size =
      (count >= 2 && isNumber(tokens[1])) ? (uint32_t)strtoul(tokens[1], nullptr, 10) : 0;
  if (!g_store->uploadEnd(size)) {
    replyNotReady();
    return;
  }
  replyOkNumber((long)g_store->fileSize());
}

void cmdHelp() {
  Serial.print(F(
      "i|comandos|ping info books chap verse find rand\n"
      "i|lectura|chap <libro> [capitulo]  |  verse <libro> <capitulo>:<versiculo>\n"
      "i|busqueda|find <texto>  |  find <desde> <limite> <texto>\n"
      "i|carga|putbegin <bytes> / put <offset> <hex> / putend <bytes>\n"
      "i|libro|acepta numero (1-66), USFM (JHN), abreviatura (Jn) o nombre (Juan)\n"
      "i|fin|cada respuesta termina en [OK] o [ERR]"));
  replyOk();
}

}  // namespace

void protocolBegin(BibleStore *store) {
  g_store = store;
  g_lineLen = 0;
}

void protocolPoll() {
  while (Serial.available() > 0) {
    const int value = Serial.read();
    if (value < 0) {
      break;
    }
    const char c = (char)value;
    if (c == '\r') {
      continue;
    }
    if (c != '\n') {
      if (g_lineLen + 1 < sizeof(g_line)) {
        g_line[g_lineLen++] = c;
      }
      continue;
    }
    g_line[g_lineLen] = '\0';
    g_lineLen = 0;
    if (!g_line[0]) {
      continue;
    }
    strcpy(g_rawLine, g_line);

    char *tokens[8];
    const int count = tokenize(g_line, tokens, 8);
    if (count == 0) {
      continue;
    }
    for (char *p = tokens[0]; *p; ++p) {
      *p = (char)tolower((unsigned char)*p);
    }
    const char *cmd = tokens[0];

    if (strcmp(cmd, "ping") == 0) {
      replyOk("pong");
    } else if (strcmp(cmd, "help") == 0) {
      cmdHelp();
    } else if (strcmp(cmd, "info") == 0) {
      cmdInfo();
    } else if (strcmp(cmd, "books") == 0) {
      cmdBooks();
    } else if (strcmp(cmd, "chap") == 0) {
      handleChapterCommand(tokens, count, false);
    } else if (strcmp(cmd, "verse") == 0) {
      handleChapterCommand(tokens, count, true);
    } else if (strcmp(cmd, "find") == 0) {
      cmdFind(tokens, count, g_rawLine);
    } else if (strcmp(cmd, "rand") == 0) {
      cmdRandom();
    } else if (strcmp(cmd, "putbegin") == 0) {
      cmdPutBegin(tokens, count);
    } else if (strcmp(cmd, "put") == 0) {
      cmdPut(tokens, count);
    } else if (strcmp(cmd, "putend") == 0) {
      cmdPutEnd(tokens, count);
    } else {
      replyError("cmd", "comando desconocido");
    }
  }
}
