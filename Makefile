# Atajos para construir, grabar y usar la Biblia RVR1960 en la ESP32.
# Ejemplo:  make flash PORT=/dev/ttyUSB0 && make upload PORT=/dev/ttyUSB0 && make read

PY      ?= python3
PORT    ?= /dev/ttyUSB0
BAUD    ?= 115200
FQBN    ?= esp32:esp32:esp32
SKETCH  ?= firmware/esp32_biblia

.PHONY: help data compile flash upload read gui sim test clean

help:
	@echo "make data     genera data/bible.dat desde el JSON"
	@echo "make compile  compila el firmware"
	@echo "make flash    graba el firmware (PORT=$(PORT))"
	@echo "make upload   envia los datos por el puerto serie"
	@echo "make read     abre el lector interactivo"
	@echo "make gui      abre la interfaz grafica"
	@echo "make sim      simulador del dispositivo (sin placa)"
	@echo "make test     comprobaciones del archivo binario"

data:
	$(PY) tools/build_bible.py

compile:
	arduino-cli compile --fqbn $(FQBN) $(SKETCH)

flash:
	arduino-cli upload -p $(PORT) --fqbn $(FQBN) $(SKETCH)

upload:
	$(PY) tools/biblia.py --port $(PORT) --baud $(BAUD) upload

read:
	$(PY) tools/biblia.py --port $(PORT) --baud $(BAUD)

gui:
	$(PY) tools/biblia_gui.py --port $(PORT) --baud $(BAUD)

sim:
	$(PY) tools/sim_esp32.py

test:
	$(PY) tools/sim_esp32.py --selftest

clean:
	rm -rf data/bible.dat build
