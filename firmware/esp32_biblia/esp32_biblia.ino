// ============================================================================
//  Biblia electronica Reina-Valera 1960 sobre ESP32
//  El texto viaja por el puerto serie; el cliente de PC esta en tools/biblia.py
// ============================================================================
#include <FS.h>
#include <LittleFS.h>

#include "biblia_config.h"
#include "biblia_protocol.h"
#include "bible_store.h"

#if BIB_USE_SD
#include <SD.h>
#include <SPI.h>
#endif

// El descompresor tinfl de la ROM reserva varios KB de pila dentro de
// tinfl_decompress_mem_to_mem(); el loop de Arduino solo tiene 8 KB.
SET_LOOP_TASK_STACK_SIZE(32 * 1024);

BibleStore bible;
static fs::FS *activeFs = nullptr;

// Monta el sistema de archivos y carga el indice de la Biblia.
static void mountStorage() {
  if (!activeFs) {
#if BIB_USE_SD
    SPI.begin(BIB_SD_SCK, BIB_SD_MISO, BIB_SD_MOSI, BIB_SD_CS);
    if (SD.begin(BIB_SD_CS, SPI, 20000000)) {
      activeFs = &SD;
      Serial.println(F("[BOOT] tarjeta SD montada"));
    } else {
      Serial.println(F("[BOOT] sin tarjeta SD, se usa la memoria interna"));
    }
#endif
    if (!activeFs) {
      if (LittleFS.begin(true)) {
        activeFs = &LittleFS;
        Serial.println(F("[BOOT] almacenamiento interno montado"));
      } else {
        Serial.println(F("[BOOT] error: no se pudo montar LittleFS"));
        return;
      }
    }
  }

  const char *label = activeFs == (fs::FS *)&LittleFS ? "littlefs" : "sd";
  bible.setSource(label);
  if (bible.begin(activeFs, BIB_DATA_PATH)) {
    Serial.print(F("[BOOT] datos cargados: "));
    Serial.print(activeFs->exists(BIB_DATA_PATH) ? "si" : "no");
    Serial.print(F(" | libros="));
    Serial.print(bible.bookCount());
    Serial.print(F(" capitulos="));
    Serial.print(bible.chapterCount());
    Serial.print(F(" versiculos="));
    Serial.println(bible.verseCount());
  } else {
    Serial.print(F("[BOOT] sin datos utilizables: "));
    Serial.println(bible.error());
    Serial.println(F("[BOOT] envia data/bible.dat con: python tools/biblia.py upload"));
  }
}

void setup() {
  Serial.begin(BIB_BAUD);
  Serial.setTimeout(2000);
  randomSeed(esp_random());
  delay(300);
  Serial.println();
  Serial.println(F("[BOOT] =============================="));
  Serial.println(F("[BOOT]  Biblia RVR1960 - ESP32"));
  Serial.println(F("[BOOT] =============================="));
  Serial.print(F("[BOOT] velocidad serie: "));
  Serial.println(BIB_BAUD);

  mountStorage();
  protocolBegin(&bible);

  Serial.print(F("[RDY] "));
  Serial.println(bible.ready() ? "listo" : "sin datos");
}

void loop() {
  protocolPoll();
  delay(1);
}
