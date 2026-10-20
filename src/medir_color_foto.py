"""Mide el HSV de referencia de una o varias fotos guardadas, usando
exactamente el mismo metodo (deteccion de circulo por Hough + mascara
anular) que evaluar_pieza() usa en produccion dentro de coordinador.py.

No mueve el Dobot ni escribe nada al PLC ni a la camara: solo lee archivos
de imagen ya guardados (por ejemplo, los que coordinador.py deja en
~/fotos_evaluadas durante la operacion normal) y reporta que detecto.

Uso:
    python3 src/medir_color_foto.py foto1.jpg foto2.jpg ...
    python3 src/medir_color_foto.py ~/fotos_evaluadas/*.jpg
    python3 src/medir_color_foto.py --dir ~/fotos_evaluadas

Procedimiento de recalibracion (ver README, seccion "Calibracion de
vision"):
  1. Juntar fotos de piezas buenas y de cada tipo de defecto a detectar.
  2. Correr este script sobre todas para ver el HSV que mide cada una.
  3. Si las piezas buenas caen en un color distinto a las referencias
     existentes, agregar (no reemplazar) esa tupla a COLOR_REFS_HSV en
     coordinador.py.
  4. Validar contra TODAS las fotos conocidas (buenas y malas) antes de
     desplegar.
"""
import glob
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coordinador import evaluar_pieza  # noqa: E402


def medir_archivo(ruta):
    frame = cv2.imread(ruta)
    if frame is None:
        print(f"{os.path.basename(ruta)}: no se pudo abrir el archivo")
        return
    resultado = evaluar_pieza(frame)
    nombre = os.path.basename(ruta)
    if not resultado["hay_forma"]:
        print(f"{nombre}: sin circulo detectado ({resultado['motivo']})")
        return
    print(
        f"{nombre}: hsv={resultado['color_hsv']} "
        f"diferencia_a_ref_mas_cercana={resultado['diferencia']} "
        f"pct_azul={resultado['pct_azul']} "
        f"-> {'aprobada' if resultado['aprobada'] else 'rechazada'} ({resultado['motivo']})"
    )


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    rutas = []
    if args[0] == "--dir":
        if len(args) < 2:
            print("Falta la carpeta despues de --dir")
            return
        rutas = sorted(glob.glob(os.path.join(args[1], "*.jpg"))) + \
            sorted(glob.glob(os.path.join(args[1], "*.png")))
    else:
        for patron in args:
            coincidencias = sorted(glob.glob(patron))
            rutas.extend(coincidencias if coincidencias else [patron])

    if not rutas:
        print("No se encontraron fotos para medir.")
        return

    for ruta in rutas:
        medir_archivo(ruta)


if __name__ == "__main__":
    main()
