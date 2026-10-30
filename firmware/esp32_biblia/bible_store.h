// Acceso aleatorio a la base binaria de la Biblia (lectura y busqueda).
#pragma once

#include <Arduino.h>
#include <FS.h>

#include "biblia_config.h"

// Tipos de linea que puede contener un capitulo.
enum BibleLineKind : uint8_t {
  BIBLE_LINE_VERSE = 0,   // versiculo normal
  BIBLE_LINE_HEADING = 1, // titulo de seccion  ("# ...")
  BIBLE_LINE_LABEL = 2,   // etiqueta de salmo   ("~ ...")
};

struct BibleBook {
  char name[41];      // "Génesis"
  char usfm[9];       // "GEN"
  char shortName[13]; // "Gn"
  uint16_t chapters;
  uint16_t firstChapter;
};

struct BibleChapterRef {
  uint8_t book;       // indice de libro 0..65
  uint8_t number;     // numero de capitulo (1..150)
  uint16_t verses;
  uint32_t offset;    // posicion del bloque comprimido
  uint16_t compLen;
  uint16_t rawLen;
  uint32_t numOff;    // 0 = numeracion correlativa
};

class BibleStore {
 public:
  // Monta el archivo y carga las tablas en memoria.
  bool begin(fs::FS *fs, const char *path);

  bool ready() const { return ready_; }
  const char *error() const { return error_; }
  const char *source() const { return source_; }
  void setSource(const char *source) { source_ = source ? source : ""; }
  uint32_t fileSize() const { return header_.totalSize; }

  uint16_t bookCount() const { return header_.bookCount; }
  uint16_t chapterCount() const { return header_.chapterCount; }
  uint32_t verseCount() const { return header_.verseCount; }

  const BibleBook &book(uint16_t index) const { return books_[index]; }

  // Acepta numero (1..66), codigo USFM ("JHN") o nombre/abreviatura ("Jn",
  // "Juan", "juan"). Devuelve -1 si no lo encuentra.
  int findBook(const char *query) const;

  // Indice global de capitulo, o -1 si no existe.
  int chapterIndex(uint8_t book, uint16_t chapter) const;

  // Descomprime el capitulo en el buffer interno.
  bool openChapter(uint16_t chapterIndex);

  const BibleBook &currentBook() const { return books_[currentChapter_.book]; }
  uint16_t currentChapterIndex() const { return currentIndex_; }
  uint16_t currentChapterNumber() const { return currentChapter_.number; }
  uint16_t currentVerseCount() const { return currentChapter_.verses; }

  uint16_t currentLineCount() const { return lineCount_; }
  BibleLineKind currentLineKind(uint16_t line) const { return (BibleLineKind)kinds_[line]; }
  const char *currentLine(uint16_t line) const { return lines_[line]; }
  // Numero de versiculo de la linea, o 0 si es un titulo.
  uint16_t currentLineVerse(uint16_t line) const { return verses_[line]; }

  // Copia una referencia legible ("Jn 3:16") en 'out'.
  void formatRef(char *out, size_t size, uint16_t chapterIndex, uint16_t verse) const;

  // Busca 'query' (sin distinguir mayusculas ni tildes) desde el capitulo
  // 'from'. Devuelve cuantos resultados escribio en el callback.
  typedef void (*MatchCallback)(void *ctx, uint16_t chapterIndex, uint16_t verse,
                                const char *text);
  uint16_t search(const char *query, uint16_t from, uint16_t limit,
                  MatchCallback callback, void *ctx, uint16_t *nextChapter);

  // Estado de la carga del archivo por puerto serie.
  bool uploadBegin(uint32_t size);
  bool uploadChunk(uint32_t offset, const uint8_t *data, size_t len);
  bool uploadEnd(uint32_t size);
  uint32_t uploadWritten() const { return uploadWritten_; }

 private:
  bool readAt(uint32_t offset, uint8_t *buffer, size_t len);
  bool loadTables();
  void buildLineIndex();
  void fail(const char *message);

  fs::FS *fs_ = nullptr;
  File file_;
  const char *path_ = BIB_DATA_PATH;
  const char *source_ = "";
  const char *error_ = "";
  bool ready_ = false;

  struct Header {
    uint16_t version;
    uint16_t flags;
    uint16_t bookCount;
    uint16_t chapterCount;
    uint32_t verseCount;
    uint32_t bookTableOff;
    uint32_t chapTableOff;
    uint32_t numTableOff;
    uint32_t dataOff;
    uint16_t maxRawLen;
    uint16_t maxCompLen;
    uint32_t totalSize;
  } header_;

  BibleBook *books_ = nullptr;
  BibleChapterRef *chapters_ = nullptr;
  uint16_t maxLines_ = 0;

  int currentIndex_ = -1;
  BibleChapterRef currentChapter_ = {};

  uint8_t *compBuf_ = nullptr;
  char *rawBuf_ = nullptr;
  char *foldBuf_ = nullptr;
  const char **lines_ = nullptr;
  uint16_t *verses_ = nullptr;
  uint8_t *kinds_ = nullptr;
  uint16_t lineCount_ = 0;

  // Carga en curso.
  File uploadFile_;
  bool uploading_ = false;
  uint32_t uploadSize_ = 0;
  uint32_t uploadWritten_ = 0;

  void reset();
};

// Convierte UTF-8 a minusculas ASCII plegando las tildes del espanol
// ("Corazón" -> "corazon"). 'dst' debe tener espacio para len+1 bytes.
void bibleFoldUtf8(const char *src, size_t len, char *dst);
