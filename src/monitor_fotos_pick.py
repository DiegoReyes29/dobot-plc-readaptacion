"""Captura fotos de referencia para calibrar la vision, sin necesitar el
Dobot ni correr el ciclo completo de produccion.

Se conecta solo al PLC (para leer el sensor Part_AV / I0.0, "pieza presente
en el punto de recogida") y a la camara. Cada vez que detecta que llega una
pieza nueva a PICK, espera a que se asiente y guarda una foto. Para calibrar
un tipo de defecto que todavia no pasa por produccion real, basta con poner
piezas (buenas y con cada defecto a probar) a mano en el punto de PICK con
este script corriendo.

Uso:
    python3 src/monitor_fotos_pick.py
    (Ctrl+C para detener)

Las fotos quedan en ~/fotos_pick/tapa_NNN_timestamp.jpg, listas para medir
con:
    python3 src/medir_color_foto.py --dir ~/fotos_pick
"""
import os
import time
from datetime import datetime

import cv2
import snap7
from snap7.util import get_bool

PLC_IP = '192.168.10.31'
RACK = 0
SLOT = 2
CARPETA = os.path.expanduser('~/fotos_pick')
ESPERA_ASENTADO_S = 0.6  # tiempo para que la pieza deje de moverse antes de fotografiarla

os.makedirs(CARPETA, exist_ok=True)


def main():
    plc = snap7.client.Client()
    plc.connect(PLC_IP, RACK, SLOT)

    # Mismo symlink fijo y mismos ajustes que usa coordinador.py en
    # produccion (ver main() ahi): backend V4L2 explicito y buffer de 1
    # frame para que cada lectura de siempre el frame mas reciente.
    cap = cv2.VideoCapture('/dev/camara_dobot', cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 800)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 600)
    if not cap.isOpened():
        print("No se pudo abrir la camara (/dev/camara_dobot).")
        return
    cap.read()  # frame de calentamiento

    print(f"Camara lista. Monitoreando Part_AV (I0.0). Fotos en {CARPETA}")
    print("Poné una pieza en el punto de PICK para cada foto. Ctrl+C para detener.")

    part_av_anterior = False
    contador = 0
    try:
        while True:
            pa_actual = get_bool(plc.eb_read(0, 1), 0, 0)
            if pa_actual and not part_av_anterior:
                contador += 1
                time.sleep(ESPERA_ASENTADO_S)
                cap.read()  # descarta un frame viejo del buffer
                ret, frame = cap.read()
                if ret:
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    ruta = os.path.join(CARPETA, f"tapa_{contador:03d}_{ts}.jpg")
                    cv2.imwrite(ruta, frame)
                    print(f"Foto guardada: {ruta}")
                else:
                    print("Part_AV detectado pero no se pudo leer un frame de la camara.")
            part_av_anterior = pa_actual
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
    finally:
        cap.release()
        plc.disconnect()


if __name__ == "__main__":
    main()
