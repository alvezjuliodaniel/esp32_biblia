// Protocolo de linea de comandos por puerto serie.
#pragma once

#include <Arduino.h>

#include "bible_store.h"

// Prepara el interprete de comandos.
void protocolBegin(BibleStore *store);

// Lee y ejecuta los comandos disponibles en el puerto serie. Debe llamarse
// continuamente desde loop().
void protocolPoll();
