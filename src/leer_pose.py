"""Utilidad de calibracion: imprime la pose actual del Dobot en bucle.

No escribe nada al PLC ni mueve el brazo por si solo - se usa junto con los
botones de jog de DobotStudio, o empujando el brazo a mano, para anotar las
coordenadas (x, y, z, r) de cada punto que coordinador.py necesita (PICK,
PLACE, WAYPOINT_1-3, HOME, DESCARTE). Ver la seccion "Calibracion de las
posiciones del Dobot" del README para el procedimiento completo.

Uso:
    python3 leer_pose.py
    (Ctrl+C para salir una vez anotado el punto que se necesitaba)
"""
import time

from pydobot import Dobot

DOBOT_PORT = '/dev/dobot_magician'

if __name__ == '__main__':
    device = Dobot(port=DOBOT_PORT)
    print("Leyendo pose cada 0.5s. Mueve el brazo a la posicion deseada y "
          "anota los valores. Ctrl+C para salir.\n")
    try:
        while True:
            x, y, z, r, *_ = device.pose()
            print(f"x={x:8.2f}  y={y:8.2f}  z={z:8.2f}  r={r:8.2f}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        device.close()
