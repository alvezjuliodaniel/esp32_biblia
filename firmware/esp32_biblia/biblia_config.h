// Configuracion de la Biblia electronica RVR1960
#pragma once

// Velocidad del puerto serie (el monitor serie y el cliente deben usar la misma).
#define BIB_BAUD 115200

// Ruta del archivo de datos dentro del sistema de archivos.
#define BIB_DATA_PATH "/bible.dat"

// 1 = usar tarjeta SD, 0 = usar la memoria flash del ESP32 (LittleFS).
// Si vale 1 pero la SD no responde, se intenta LittleFS como respaldo.
#define BIB_USE_SD 0

// Pines SPI de la tarjeta SD (solo si BIB_USE_SD vale 1).
#define BIB_SD_CS 5
#define BIB_SD_SCK 18
#define BIB_SD_MISO 19
#define BIB_SD_MOSI 23

// Maximo de resultados que devuelve una busqueda en un solo lote.
#define BIB_FIND_LIMIT 40

// Longitud maxima de una linea de comando recibida por serie.
#define BIB_LINE_BUFFER 512

// Longitud maxima de una referencia preformateada ("1 Co 13:13").
#define BIB_REF_LENGTH 24
