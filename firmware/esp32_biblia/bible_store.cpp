// Implementacion del acceso a la base binaria de la Biblia.
#include "bible_store.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#if __has_include("rom/miniz.h")
#include "rom/miniz.h"
#elif __has_include("miniz.h")
#include "miniz.h"
#else
#error "No se encontro miniz (descompresor zlib) en el core de ESP32"
#endif

namespace {

const char kMagic[8] = {'R', 'V', 'R', '1', '9', '6', '0', '\0'};
const uint16_t kFormatVersion = 1;

const size_t kHeaderSize = 64;
const size_t kBookEntrySize = 64;
const size_t kChapterEntrySize = 16;

// Mapa de caracteres UTF-8 de dos bytes (0xC3 xx) del espanol a ASCII.
// La segunda lista contiene tildes y enes en mayuscula y minuscula.
struct FoldPair {
  uint8_t second;
  char ascii;
};

const FoldPair kFoldPairs[] = {
    {0x81, 'a'}, {0x89, 'e'}, {0x8D, 'i'}, {0x93, 'o'}, {0x9A, 'u'},
    {0x9C, 'u'}, {0x91, 'n'}, {0xA1, 'a'}, {0xA9, 'e'}, {0xAD, 'i'},
    {0xB3, 'o'}, {0xBA, 'u'}, {0xBC, 'u'}, {0xB1, 'n'}, {0x87, 'c'},
};

inline uint16_t readU16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

inline uint32_t readU32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

// Copia un campo de ancho fijo terminado en NUL.
void copyFixed(char *dst, size_t capacity, const uint8_t *src, size_t srcLen) {
  size_t len = 0;
  while (len + 1 < capacity && len < srcLen && src[len] != 0) {
    dst[len] = (char)src[len];
    ++len;
  }
  dst[len] = '\0';
}

// Busca 'needle' como palabra completa dentro de 'haystack'.
bool containsWord(const char *haystack, const char *needle) {
  const size_t len = strlen(needle);
  if (len == 0) {
    return false;
  }
  const char *p = haystack;
  while ((p = strstr(p, needle)) != nullptr) {
    const bool startOk = (p == haystack) || (p[-1] == ' ');
    const bool endOk = (p[len] == '\0') || (p[len] == ' ');
    if (startOk && endOk) {
      return true;
    }
    p += len;
  }
  return false;
}

}  // namespace

void bibleFoldUtf8(const char *src, size_t len, char *dst) {
  size_t in = 0;
  size_t out = 0;
  while (in < len) {
    uint8_t c = (uint8_t)src[in];
    if (c == 0xC3 && in + 1 < len) {
      uint8_t next = (uint8_t)src[in + 1];
      char mapped = 0;
      for (const FoldPair &pair : kFoldPairs) {
        if (pair.second == next) {
          mapped = pair.ascii;
          break;
        }
      }
      dst[out++] = mapped ? mapped : '?';
      in += 2;
      continue;
    }
    if (c < 0x80) {
      dst[out++] = (char)tolower(c);
    } else {
      // Otros caracteres multibyte se ignoran: no aportan a la comparacion.
      dst[out++] = '?';
    }
    ++in;
  }
  dst[out] = '\0';
}

bool BibleStore::begin(fs::FS *fs, const char *path) {
  reset();
  if (!fs) {
    fail("sistema de archivos no iniciado");
    return false;
  }
  fs_ = fs;
  if (path) {
    path_ = path;
  }
  if (!fs_->exists(path_)) {
    fail("no hay datos: falta " BIB_DATA_PATH);
    return false;
  }
  file_ = fs_->open(path_, FILE_READ);
  if (!file_) {
    fail("no se pudo abrir " BIB_DATA_PATH);
    return false;
  }

  uint8_t headerBytes[kHeaderSize];
  if (!readAt(0, headerBytes, kHeaderSize) ||
      memcmp(headerBytes, kMagic, sizeof(kMagic)) != 0) {
    fail("el archivo no es una base RVR1960 valida");
    return false;
  }

  header_.version = readU16(headerBytes + 8);
  header_.flags = readU16(headerBytes + 10);
  header_.bookCount = readU16(headerBytes + 12);
  header_.chapterCount = readU16(headerBytes + 14);
  header_.verseCount = readU32(headerBytes + 16);
  header_.bookTableOff = readU32(headerBytes + 20);
  header_.chapTableOff = readU32(headerBytes + 24);
  header_.numTableOff = readU32(headerBytes + 28);
  header_.dataOff = readU32(headerBytes + 32);
  header_.maxRawLen = readU16(headerBytes + 36);
  header_.maxCompLen = readU16(headerBytes + 38);
  header_.totalSize = readU32(headerBytes + 40);

  if (header_.version != kFormatVersion) {
    fail("version de formato no soportada");
    return false;
  }
  if (header_.bookCount == 0 || header_.chapterCount == 0 ||
      header_.maxRawLen == 0 || header_.maxCompLen == 0) {
    fail("cabecera incoherente");
    return false;
  }

  if (!loadTables()) {
    return false;
  }

  const size_t rawBytes = header_.maxRawLen + 1;
  compBuf_ = (uint8_t *)malloc(header_.maxCompLen);
  rawBuf_ = (char *)malloc(rawBytes);
  foldBuf_ = (char *)malloc(rawBytes);
  lines_ = (const char **)calloc(maxLines_, sizeof(const char *));
  verses_ = (uint16_t *)calloc(maxLines_, sizeof(uint16_t));
  kinds_ = (uint8_t *)calloc(maxLines_, sizeof(uint8_t));
  if (!compBuf_ || !rawBuf_ || !foldBuf_ || !lines_ || !verses_ || !kinds_) {
    fail("memoria insuficiente");
    return false;
  }

  ready_ = true;
  return true;
}

bool BibleStore::loadTables() {
  books_ = (BibleBook *)calloc(header_.bookCount, sizeof(BibleBook));
  chapters_ = (BibleChapterRef *)calloc(header_.chapterCount, sizeof(BibleChapterRef));
  if (!books_ || !chapters_) {
    fail("memoria insuficiente para el indice");
    return false;
  }

  uint8_t entry[kBookEntrySize];
  for (uint16_t i = 0; i < header_.bookCount; ++i) {
    if (!readAt(header_.bookTableOff + (uint32_t)i * kBookEntrySize, entry, kBookEntrySize)) {
      fail("error leyendo la tabla de libros");
      return false;
    }
    BibleBook &book = books_[i];
    copyFixed(book.name, sizeof(book.name), entry, 40);
    copyFixed(book.usfm, sizeof(book.usfm), entry + 40, 8);
    copyFixed(book.shortName, sizeof(book.shortName), entry + 48, 12);
    book.chapters = readU16(entry + 60);
    book.firstChapter = readU16(entry + 62);
  }

  uint8_t chapterEntry[kChapterEntrySize];
  uint16_t lines = 0;
  for (uint16_t i = 0; i < header_.chapterCount; ++i) {
    if (!readAt(header_.chapTableOff + (uint32_t)i * kChapterEntrySize, chapterEntry,
                kChapterEntrySize)) {
      fail("error leyendo la tabla de capitulos");
      return false;
    }
    BibleChapterRef &ref = chapters_[i];
    ref.book = chapterEntry[0];
    ref.number = chapterEntry[1];
    ref.verses = readU16(chapterEntry + 2);
    ref.offset = readU32(chapterEntry + 4);
    ref.compLen = readU16(chapterEntry + 8);
    ref.rawLen = readU16(chapterEntry + 10);
    ref.numOff = readU32(chapterEntry + 12);
    // Cada versiculo es al menos una linea; los titulos agregan unas pocas mas.
    if ((uint16_t)(ref.verses + 64) > lines) {
      lines = (uint16_t)(ref.verses + 64);
    }
  }
  maxLines_ = lines;
  return true;
}

void BibleStore::reset() {
  if (file_) {
    file_.close();
  }
  if (uploadFile_) {
    uploadFile_.close();
  }
  free(books_);
  free(chapters_);
  free(compBuf_);
  free(rawBuf_);
  free(foldBuf_);
  free(lines_);
  free(verses_);
  free(kinds_);
  books_ = nullptr;
  chapters_ = nullptr;
  compBuf_ = nullptr;
  rawBuf_ = nullptr;
  foldBuf_ = nullptr;
  lines_ = nullptr;
  verses_ = nullptr;
  kinds_ = nullptr;
  lineCount_ = 0;
  maxLines_ = 0;
  currentIndex_ = -1;
  ready_ = false;
  uploading_ = false;
  uploadSize_ = 0;
  uploadWritten_ = 0;
  header_ = Header();
}

void BibleStore::fail(const char *message) {
  error_ = message;
  ready_ = false;
}

bool BibleStore::readAt(uint32_t offset, uint8_t *buffer, size_t len) {
  if (!file_) {
    return false;
  }
  if (!file_.seek(offset)) {
    return false;
  }
  return file_.read(buffer, len) == len;
}

int BibleStore::findBook(const char *query) const {
  if (!query || !*query || !books_) {
    return -1;
  }

  // 1) Numero de libro (1..66).
  bool numeric = true;
  for (const char *p = query; *p; ++p) {
    if (!isdigit((unsigned char)*p)) {
      numeric = false;
      break;
    }
  }
  if (numeric) {
    long value = atol(query);
    if (value >= 1 && value <= header_.bookCount) {
      return (int)(value - 1);
    }
    return -1;
  }

  // 2) Comparacion plegada contra USFM, abreviatura y nombre.
  char needle[48];
  const size_t queryLen = strnlen(query, sizeof(needle) - 1);
  bibleFoldUtf8(query, queryLen, needle);
  size_t needleLen = strlen(needle);

  // Se ignoran espacios para aceptar "1co", "1 co", "1 Co".
  char compact[48];
  size_t compactLen = 0;
  for (size_t i = 0; i < needleLen && compactLen + 1 < sizeof(compact); ++i) {
    if (needle[i] != ' ') {
      compact[compactLen++] = needle[i];
    }
  }
  compact[compactLen] = '\0';

  int prefixMatch = -1;
  int wordMatch = -1;
  for (uint16_t i = 0; i < header_.bookCount; ++i) {
    const BibleBook &book = books_[i];
    char folded[48];

    bibleFoldUtf8(book.usfm, strlen(book.usfm), folded);
    if (strcmp(folded, compact) == 0) {
      return i;
    }
    bibleFoldUtf8(book.shortName, strlen(book.shortName), folded);
    size_t shortLen = 0;
    for (size_t k = 0; folded[k]; ++k) {
      if (folded[k] != ' ') {
        folded[shortLen++] = folded[k];
      }
    }
    folded[shortLen] = '\0';
    if (strcmp(folded, compact) == 0) {
      return i;
    }
    bibleFoldUtf8(book.name, strlen(book.name), folded);
    if (strcmp(folded, needle) == 0) {
      return i;
    }
    if (prefixMatch < 0 && needleLen >= 2 && strncmp(folded, needle, needleLen) == 0) {
      prefixMatch = i;
    }
    if (wordMatch < 0 && needleLen >= 3 && containsWord(folded, needle)) {
      wordMatch = i;
    }
  }
  return prefixMatch >= 0 ? prefixMatch : wordMatch;
}

int BibleStore::chapterIndex(uint8_t book, uint16_t chapter) const {
  if (book >= header_.bookCount || chapter < 1) {
    return -1;
  }
  const BibleBook &info = books_[book];
  if (chapter > info.chapters) {
    return -1;
  }
  return (int)info.firstChapter + (chapter - 1);
}

bool BibleStore::openChapter(uint16_t chapterIndex) {
  if (!ready_ || chapterIndex >= header_.chapterCount) {
    return false;
  }
  if ((int)chapterIndex == currentIndex_) {
    return true;
  }

  const BibleChapterRef &ref = chapters_[chapterIndex];
  if (ref.compLen > header_.maxCompLen || ref.rawLen > header_.maxRawLen) {
    return false;
  }
  if (!readAt(ref.offset, compBuf_, ref.compLen)) {
    return false;
  }

  size_t produced = tinfl_decompress_mem_to_mem(
      rawBuf_, header_.maxRawLen, compBuf_, ref.compLen,
      TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
  if (produced == TINFL_DECOMPRESS_MEM_TO_MEM_FAILED || produced != ref.rawLen) {
    currentIndex_ = -1;
    return false;
  }
  rawBuf_[ref.rawLen] = '\0';

  currentIndex_ = (int)chapterIndex;
  currentChapter_ = ref;
  buildLineIndex();
  return true;
}

void BibleStore::buildLineIndex() {
  lineCount_ = 0;
  uint16_t verseNumber = 0;

  // Numeros reales de versiculo (solo en capitulos con numeracion irregular).
  uint8_t numbers[256];
  bool hasNumbers = currentChapter_.numOff != 0;
  if (hasNumbers) {
    if (!readAt(header_.numTableOff + currentChapter_.numOff, numbers,
                currentChapter_.verses)) {
      hasNumbers = false;
    }
  }

  const uint16_t len = currentChapter_.rawLen;
  uint16_t start = 0;
  while (start < len && lineCount_ < maxLines_) {
    uint16_t end = start;
    while (end < len && rawBuf_[end] != '\n') {
      ++end;
    }
    rawBuf_[end] = '\0';

    char *line = rawBuf_ + start;
    BibleLineKind kind = BIBLE_LINE_VERSE;
    if (line[0] == '#' && line[1] == ' ') {
      kind = BIBLE_LINE_HEADING;
      line += 2;
    } else if (line[0] == '~' && line[1] == ' ') {
      kind = BIBLE_LINE_LABEL;
      line += 2;
    }

    uint16_t number = 0;
    if (kind == BIBLE_LINE_VERSE) {
      if (hasNumbers && verseNumber < currentChapter_.verses) {
        number = numbers[verseNumber];
      } else {
        number = (uint16_t)(verseNumber + 1);
      }
      ++verseNumber;
    }

    const uint16_t slot = lineCount_++;
    lines_[slot] = line;
    verses_[slot] = number;
    kinds_[slot] = (uint8_t)kind;

    start = (uint16_t)(end + 1);
  }
}

void BibleStore::formatRef(char *out, size_t size, uint16_t chapterIndex,
                           uint16_t verse) const {
  if (chapterIndex >= header_.chapterCount) {
    snprintf(out, size, "?");
    return;
  }
  const BibleChapterRef &ref = chapters_[chapterIndex];
  if (verse) {
    snprintf(out, size, "%s %u:%u", books_[ref.book].shortName, ref.number, verse);
  } else {
    snprintf(out, size, "%s %u", books_[ref.book].shortName, ref.number);
  }
}

uint16_t BibleStore::search(const char *query, uint16_t from, uint16_t limit,
                            MatchCallback callback, void *ctx,
                            uint16_t *nextChapter) {
  *nextChapter = header_.chapterCount;
  if (!ready_ || !query || !*query) {
    return 0;
  }

  char needle[96];
  bibleFoldUtf8(query, strnlen(query, sizeof(needle) - 1), needle);
  const size_t needleLen = strlen(needle);
  if (needleLen == 0) {
    return 0;
  }
  if (limit == 0) {
    limit = BIB_FIND_LIMIT;
  }

  uint16_t matches = 0;
  if (from >= header_.chapterCount) {
    return 0;
  }

  for (uint16_t chapter = from; chapter < header_.chapterCount; ++chapter) {
    if (!openChapter(chapter)) {
      continue;
    }
    for (uint16_t line = 0; line < lineCount_; ++line) {
      if (kinds_[line] != BIBLE_LINE_VERSE) {
        continue;
      }
      const char *text = lines_[line];
      size_t textLen = strlen(text);
      if (textLen < needleLen) {
        continue;
      }
      bibleFoldUtf8(text, textLen, foldBuf_);
      if (!strstr(foldBuf_, needle)) {
        continue;
      }
      if (callback) {
        callback(ctx, chapter, verses_[line], text);
      }
      if (++matches >= limit) {
        *nextChapter = (uint16_t)(chapter + 1);
        return matches;
      }
    }
  }
  return matches;
}

bool BibleStore::uploadBegin(uint32_t size) {
  if (!fs_) {
    fail("sistema de archivos no iniciado");
    return false;
  }
  if (uploading_) {
    uploadFile_.close();
    uploading_ = false;
  }
  if (file_) {
    file_.close();
  }
  ready_ = false;
  currentIndex_ = -1;

  // Si no cabe, la escritura fallara y el cliente vera el error de espacio.
  uploadFile_ = fs_->open(path_, "w");
  if (!uploadFile_) {
    fail("no se pudo crear " BIB_DATA_PATH);
    return false;
  }
  uploading_ = true;
  uploadSize_ = size;
  uploadWritten_ = 0;
  return true;
}

bool BibleStore::uploadChunk(uint32_t offset, const uint8_t *data, size_t len) {
  if (!uploading_ || !uploadFile_) {
    return false;
  }
  if (offset != uploadWritten_) {
    return false;
  }
  if (uploadSize_ && uploadWritten_ + len > uploadSize_) {
    return false;
  }
  if (uploadFile_.write(data, len) != len) {
    return false;
  }
  uploadWritten_ += len;
  return true;
}

bool BibleStore::uploadEnd(uint32_t size) {
  if (!uploading_) {
    return false;
  }
  uploadFile_.flush();
  uploadFile_.close();
  uploading_ = false;

  const uint32_t written = uploadWritten_;
  const uint32_t expected = size ? size : uploadSize_;
  uploadSize_ = 0;
  uploadWritten_ = 0;

  if (expected && written != expected) {
    fail("la carga quedo incompleta");
    return false;
  }
  if (!begin(fs_, path_)) {
    return false;
  }
  return true;
}
