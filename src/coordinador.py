import csv
import os
import time
from datetime import datetime

import cv2
import numpy as np
import snap7
from snap7.util import get_bool, set_bool
from pydobot import Dobot

# Registro para el experimento de OEE (Objetivo 3): un CSV por ciclo y otro
# por falla/paro, pensados para importarse despues a OEE_Dobot_Separating.xlsx
# (hojas OEE_P y OEE_F) - no se escribe el xlsx directo desde la Pi.
OEE_CICLOS_CSV = os.path.expanduser('~/oee_ciclos.csv')
OEE_FALLAS_CSV = os.path.expanduser('~/oee_fallas.csv')
OEE_DESCARTES_CSV = os.path.expanduser('~/oee_descartes.csv')

# Carpeta donde se guarda una copia de cada foto evaluada (aprobada o
# rechazada), para poder revisar despues por que se tomo cada decision -
# agregado el 2026-10-07 tras un rechazo que no se pudo diagnosticar por
# no tener la imagen original guardada.
FOTOS_DIR = os.path.expanduser('~/fotos_evaluadas')
os.makedirs(FOTOS_DIR, exist_ok=True)

PLC_IP = '192.168.10.31'
RACK = 0
SLOT = 2
# OJO: verificar este puerto al reconectar - el 2026-10-06 cambio de
# /dev/ttyACM0 a /dev/ttyACM1 tras un power-cycle del Dobot. Confirmar con
# "ls /dev/ttyACM*" en el Pi antes de arrancar, y ajustar si hace falta.
DOBOT_PORT = '/dev/ttyACM0'

# Byte/bit de la senal de modo (M136.2 "Modo_Dobot"): se pone en verdadero
# solo mientras dura un ciclo, y se vuelve a apagar en el finally pase lo
# que pase (incluso si pick_and_place lanza una excepcion), para que la
# ventana en la que el PLC "cree" que el Dobot esta a cargo sea lo mas
# corta posible.
MODO_DOBOT_BYTE = 136
MODO_DOBOT_BIT = 2

# Ruta completa: recogida -> 3 puntos intermedios (esquivan obstaculos) -> colocacion
# El regreso usa los mismos puntos intermedios en reversa.
PICK = (273.73, -123.32, -54.61, -24.25)
WAYPOINT_1 = (272.22, -135.50, 22.95, -26.46)
WAYPOINT_2 = (278.56, 15.04, 39.69, 3.09)
WAYPOINT_3 = (274.38, 90.45, -30.52, 18.24)
PLACE = (279.28, 154.99, -59.39, 29.03)
HOME = (268.64, -129.22, 108.19, -25.69)  # posicion de espera entre ciclos
DESCARTE = (219.98, 14.36, -51.34, 3.79)  # posicion de rechazo para piezas defectuosas
# ^ ajustado 2026-10-07: la Z original (-57.20) no era fisicamente
# alcanzable en este punto - el Dobot se detenia de forma consistente y
# estable ~5.9mm antes (en z=-51.34, medido en vivo con el diagnostico de
# pose), nunca lograba confirmar llegada dentro de ninguna tolerancia
# razonable. Se recalibro al punto real donde el brazo si se detiene.

RUTA_VUELTA = []  # vuelta directa: despegue -> HOME, sin waypoints intermedios, probado sin choques

# Despegue vertical tras soltar: sube derecho en Z antes de moverse en
# diagonal. En PLACE es para no arrastrar la pieza. En DESCARTE se habia
# confirmado innecesario para evitar choques (2026-10-06), pero se agrega
# de nuevo el 2026-10-07 como prueba: el salto directo DESCARTE->HOME es
# un movimiento mas brusco/diagonal que podria generar un pico de
# corriente que agrava el glitch de comunicacion serial, dado el problema
# de alimentacion ya conocido de la Pi.
DESPEGUE_PLACE = (PLACE[0], PLACE[1], WAYPOINT_3[2], PLACE[3])
DESPEGUE_DESCARTE = (DESCARTE[0], DESCARTE[1], WAYPOINT_3[2], DESCARTE[3])

# --- Vision: deteccion de pieza por forma (Hough) + color (HSV).
# Calibrado el 2026-10-06 con la PIEZA REAL de produccion (collar blanco +
# rosca de bronce, NO la tapa de plastico lisa usada en pruebas previas),
# con el brazo de Distributing presente en el encuadre (asi es siempre en
# produccion real). Si se mueve la camara, cambia la pieza, o cambia la
# iluminacion, hay que recalibrar todo esto con fotos nuevas - ver
# metodologia en la memoria del proyecto (monitor_fotos_pick.py + medir
# con el metodo de circulo, nunca con una caja de coordenadas fija).
RX, RY, RW, RH = 325, 300, 225, 200  # escalado x0.5 para resolucion 800x600 (ver main())
# Lista de referencias (no una sola): el balance de blancos de la camara
# cambia de una sesion a otra tras reiniciar la Pi (ej. 2026-10-06 vs
# 2026-10-07, mismo collar, tono H muy distinto: 65.9 vs 32.7). Se acepta
# si coincide con CUALQUIERA de las referencias conocidas.
COLOR_REFS_HSV = [
    np.array([65.9, 67.4, 155.4]),  # calibrado 2026-10-06
    np.array([32.7, 71.7, 139.0]),  # calibrado 2026-10-07
    np.array([50.0, 26.8, 232.4]),  # calibrado 2026-10-07, con reflejo del LED de B3 encendido
    np.array([78.5, 34.9, 220.5]),  # calibrado 2026-10-07, resolucion 800x600 + luz distinta
    np.array([78.3, 20.1, 210.6]),  # calibrado 2026-10-07 ~15:50, luz de atardecer (saturacion mas baja)
]
TOLERANCIA_HSV = np.array([20, 25, 40])
# Rango de azul tipico de marcador, usado para detectar marcas de defecto
# (grandes o chicas) que el promedio de color por si solo no distingue bien.
AZUL_LOWER = np.array([90, 60, 40])
AZUL_UPPER = np.array([140, 255, 255])
UMBRAL_PCT_AZUL = 2.0  # % de pixeles azules dentro del aro para rechazar


def get_pose(device):
    device.ser.reset_input_buffer()
    return device.pose()


def hay_paro(plc):
    """True si Em_Stop (I1.5) o el boton de Stop (I1.1) estan activos.
    Los dos son contactos NC en el PLC: 0-signal = presionado."""
    e = plc.eb_read(1, 1)
    em_stop_activo = not get_bool(e, 0, 5)
    stop_activo = not get_bool(e, 0, 1)
    return em_stop_activo or stop_activo


def move_safe(device, x, y, z, r, plc, timeout=30, tol=1.5):
    if hay_paro(plc):
        print("Paro activo, no se manda el movimiento.")
        return False
    try:
        device.ser.reset_input_buffer()
        device.move_to(x, y, z, r, wait=False)
        start = time.time()
        ultimo_print = 0
        while time.time() - start < timeout:
            if hay_paro(plc):
                print("Paro detectado a mitad de movimiento, se corta la secuencia.")
                return False
            pose = get_pose(device)
            if abs(pose[0] - x) < tol and abs(pose[1] - y) < tol and abs(pose[2] - z) < tol:
                return True
            # Diagnostico 2026-10-07: imprime la pose leida cada ~2s
            # mientras espera, para ver en el log si el Dobot ya llego
            # fisicamente (pose cercana al objetivo) o no se movio.
            transcurrido = time.time() - start
            if transcurrido - ultimo_print >= 2.0:
                ultimo_print = transcurrido
                print(f"    (esperando {transcurrido:.1f}s, pose actual: "
                      f"x={pose[0]:.2f} y={pose[1]:.2f} z={pose[2]:.2f}, "
                      f"objetivo: x={x:.2f} y={y:.2f} z={z:.2f})")
            time.sleep(0.3)
        return False
    except Exception as e:
        # Bug conocido de desincronizacion serial del Dobot: una lectura
        # corrupta puede tirar una excepcion en vez de solo timeout. Se
        # trata igual que un movimiento fallido, no se deja que tumbe todo
        # el script.
        print(f"Error de comunicacion con el Dobot durante el movimiento: {e}")
        return False


def mover_a_home_seguro(device, plc, intentos=3):
    """Lleva el Dobot a HOME al arrancar el script, para garantizar que
    cada ciclo siempre parte de una posicion conocida y segura - nunca de
    donde haya quedado un ciclo anterior interrumpido."""
    for intento in range(1, intentos + 1):
        print(f"Llevando el Dobot a HOME (intento {intento}/{intentos})...")
        if move_safe(device, *HOME, plc=plc):
            print("Dobot en HOME.")
            return True
        time.sleep(1)
    return False


def set_modo_dobot(plc, activo):
    m = plc.mb_read(MODO_DOBOT_BYTE, 1)
    set_bool(m, 0, MODO_DOBOT_BIT, activo)
    plc.mb_write(MODO_DOBOT_BYTE, 1, m)


# Banda de descarte (Q0.1): identificada 2026-10-07 observando un ciclo
# real de rechazo neumatico - se prende junto con el resto de la secuencia
# de rechazo y se queda activa durante todo el tramo, incluido el empuje de
# B4, hasta que el PLC cierra el ciclo. En reposo nadie mas la toca, asi
# que es seguro escribirla directo desde aca sin pelear con el programa.
BANDA_DESCARTE_BYTE = 0
BANDA_DESCARTE_BIT = 1


def activar_banda_descarte(plc):
    """Prende Q0.1 justo cuando el brazo empieza a despegar del punto de
    DESCARTE - no bloquea esperando, se apaga mas adelante en
    desactivar_banda_descarte(). Asi el tiempo que la banda esta activa se
    solapa con el despegue+vuelta a HOME en vez de sumarse al ciclo."""
    try:
        q = plc.ab_read(BANDA_DESCARTE_BYTE, 1)
        set_bool(q, 0, BANDA_DESCARTE_BIT, True)
        plc.ab_write(BANDA_DESCARTE_BYTE, q)
    except Exception as e:
        print(f"No se pudo activar la banda de descarte: {e}")


def desactivar_banda_descarte(plc):
    try:
        q = plc.ab_read(BANDA_DESCARTE_BYTE, 1)
        set_bool(q, 0, BANDA_DESCARTE_BIT, False)
        plc.ab_write(BANDA_DESCARTE_BYTE, q)
    except Exception as e:
        print(f"No se pudo desactivar la banda de descarte: {e}")


def confirmar_ciclo_plc(plc):
    """Avisa al PLC que el Dobot ya termino con la pieza (pulso M136.1 +
    limpia Q136.2) para que libere el ciclo. Separada de main() el
    2026-10-07 para poder mandarla justo al despegar en la ruta PLACE, sin
    esperar a que el brazo vuelva a HOME - la banda principal (Q0.0, la
    controla el PLC solo, no este script) usa esta confirmacion para saber
    cuando avanzar, asi que mandarla antes adelanta tambien a la banda."""
    m = plc.mb_read(136, 1)
    set_bool(m, 0, 1, True)
    plc.mb_write(136, 1, m)
    time.sleep(0.3)

    m = plc.mb_read(136, 1)
    set_bool(m, 0, 1, False)
    plc.mb_write(136, 1, m)

    q = plc.ab_read(136, 1)
    set_bool(q, 0, 2, False)
    plc.ab_write(136, q)


def log_ciclo(inicio, fin, resultado):
    nuevo = not os.path.exists(OEE_CICLOS_CSV)
    with open(OEE_CICLOS_CSV, 'a', newline='') as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(['Fecha', 'HoraInicio', 'HoraFin', 'DuracionS', 'Resultado'])
        w.writerow([inicio.strftime('%Y-%m-%d'), inicio.strftime('%H:%M:%S'),
                    fin.strftime('%H:%M:%S'), f'{(fin - inicio).total_seconds():.2f}',
                    resultado])


def log_falla(inicio, fin, tipo):
    nuevo = not os.path.exists(OEE_FALLAS_CSV)
    with open(OEE_FALLAS_CSV, 'a', newline='') as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(['Fecha', 'HoraInicioFalla', 'HoraFinFalla', 'Tipo'])
        w.writerow([inicio.strftime('%Y-%m-%d'), inicio.strftime('%H:%M:%S'),
                    fin.strftime('%H:%M:%S'), tipo])


def log_descarte(momento, resultado_vision):
    nuevo = not os.path.exists(OEE_DESCARTES_CSV)
    with open(OEE_DESCARTES_CSV, 'a', newline='') as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(['Fecha', 'Hora', 'HayForma', 'ColorHSV', 'DiferenciaHSV', 'PctAzul'])
        w.writerow([momento.strftime('%Y-%m-%d'), momento.strftime('%H:%M:%S'),
                    resultado_vision.get('hay_forma'),
                    resultado_vision.get('color_hsv'),
                    resultado_vision.get('diferencia'),
                    resultado_vision.get('pct_azul')])


def tomar_foto(cap):
    ret, frame = cap.read()
    if not ret:
        return None
    return frame


def evaluar_pieza(frame):
    """Evalua una foto de la zona de PICK: busca la forma circular del
    collar (Hough) y, si la encuentra, la rechaza si el color promedio del
    aro se aleja de la referencia O si hay demasiado pixeles azules
    (marcas de defecto) dentro del aro. Calibrado 2026-10-06 - ver
    comentario junto a las constantes arriba."""
    roi = frame[RY:RY + RH, RX:RX + RW]
    gray = cv2.medianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), 5)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=100,
        param1=80, param2=40, minRadius=30, maxRadius=68  # escalado x0.5
    )
    if circles is None:
        return {"hay_forma": False, "aprobada": False, "motivo": "sin circulo"}

    x, y, r = np.round(circles[0][0]).astype(int)
    mask = np.zeros(roi.shape[:2], dtype=np.uint8)
    cv2.circle(mask, (x, y), int(r * 0.9), 255, -1)
    cv2.circle(mask, (x, y), int(r * 0.45), 0, -1)

    hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    color_promedio = np.array(cv2.mean(hsv_roi, mask=mask)[:3])
    diferencias = [np.abs(color_promedio - ref) for ref in COLOR_REFS_HSV]
    color_ok = any(bool(np.all(d <= TOLERANCIA_HSV)) for d in diferencias)
    diferencia = min(diferencias, key=lambda d: np.sum(d))

    mask_azul = cv2.inRange(hsv_roi, AZUL_LOWER, AZUL_UPPER)
    mask_azul_en_aro = cv2.bitwise_and(mask_azul, mask)
    pix_aro = cv2.countNonZero(mask)
    pix_azul = cv2.countNonZero(mask_azul_en_aro)
    pct_azul = 100 * pix_azul / pix_aro if pix_aro > 0 else 0
    azul_ok = pct_azul <= UMBRAL_PCT_AZUL

    aprobada = color_ok and azul_ok
    motivo = []
    if not color_ok:
        motivo.append("color fuera de rango")
    if not azul_ok:
        motivo.append("demasiado azul (marca de defecto)")

    return {
        "hay_forma": True,
        "aprobada": aprobada,
        "motivo": "; ".join(motivo) if motivo else "ok",
        "color_hsv": [round(float(v), 1) for v in color_promedio],
        "diferencia": [round(float(v), 1) for v in diferencia],
        "pct_azul": round(pct_azul, 2),
    }


def guardar_foto_evaluada(frame, resultado):
    """Guarda una copia de la foto evaluada (aprobada o rechazada) para
    poder revisar despues por que se tomo cada decision - sin esto, una
    foto que causo un rechazo raro no se puede volver a ver."""
    if frame is None:
        return
    estado = "aprobada" if resultado.get("aprobada") else "rechazada"
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    ruta = os.path.join(FOTOS_DIR, f"{ts}_{estado}.jpg")
    try:
        cv2.imwrite(ruta, frame)
    except Exception as e:
        print(f"No se pudo guardar la foto evaluada: {e}")


def evaluar_pieza_con_reintento(cap, intentos=3, espera=0.3):
    """Si no se detecta ninguna forma circular, puede ser que la pieza
    todavia no haya asentado del todo en PICK - se reintenta un par de
    veces antes de tratarlo como descarte real (forma invalida)."""
    ultimo = {"hay_forma": False, "aprobada": False, "motivo": "sin circulo"}
    ultimo_frame = None
    for intento in range(intentos):
        frame = tomar_foto(cap)
        if frame is not None:
            ultimo_frame = frame
            ultimo = evaluar_pieza(frame)
            if ultimo["hay_forma"]:
                guardar_foto_evaluada(frame, ultimo)
                return ultimo
        time.sleep(espera)
    guardar_foto_evaluada(ultimo_frame, ultimo)
    return ultimo


def pick_and_place(device, plc, cap):
    _t = datetime.now()

    def marca(nombre):
        nonlocal _t
        ahora = datetime.now()
        print(f"  [{nombre}] {(ahora - _t).total_seconds():.2f}s")
        _t = ahora

    # Vision: se evalua con el brazo todavia en HOME, antes de moverse, para
    # no estorbar la vista de la camara sobre la zona de PICK. Pequeña
    # espera para que la pieza/brazo de Distributing terminen de asentarse
    # antes de capturar - sin esto, el desenfoque por movimiento distorsiona
    # el color medido (hallazgo 2026-10-06).
    time.sleep(0.6)
    resultado_vision = evaluar_pieza_con_reintento(cap)
    print(f"  [Vision] {resultado_vision}")
    marca("vision")

    # Pausa entre el I/O de la camara (USB) y el primer comando serial al
    # Dobot (USB tambien) - prueba 2026-10-07 para ver si separar ambas
    # operaciones en el tiempo reduce los errores de comunicacion serial
    # que no aparecian ayer cuando camara y Dobot corrian en procesos
    # separados, y que sí aparecen hoy con todo en el mismo proceso.
    time.sleep(0.5)

    # Ida: recogida -> waypoints
    if not move_safe(device, *PICK, plc=plc):
        return False, False
    marca("HOME->PICK")
    device.grip(True)
    t_agarre = datetime.now()
    time.sleep(0.5)
    marca("grip cerrar + pausa")

    for nombre, punto in zip(["WP1", "WP2"], [WAYPOINT_1, WAYPOINT_2]):
        if not move_safe(device, *punto, plc=plc):
            return False, False
        marca(nombre)

    if resultado_vision["aprobada"]:
        # Ruta normal: WAYPOINT_2 -> PLACE directo, probado sin choques
        if not move_safe(device, *PLACE, plc=plc):
            return False, False
        marca("PLACE")

        device.grip(False)
        t_suelta = datetime.now()
        print(f"Tiempo agarre->suelta: {(t_suelta - t_agarre).total_seconds():.2f}s")
        time.sleep(0.5)
        marca("grip abrir + pausa")

        if not move_safe(device, *DESPEGUE_PLACE, plc=plc):
            return False, False
        marca("despegue")

        confirmar_ciclo_plc(plc)
        marca("confirmacion PLC (adelantada)")

        for i, punto in enumerate(RUTA_VUELTA):
            if not move_safe(device, *punto, plc=plc):
                return False, True
            marca(f"vuelta_{i}")
    else:
        # Ruta de descarte: pieza mal puesta/irreconocible, se deja en un
        # punto aparte en vez de colocarla en la linea.
        print(f"  [Vision] PIEZA RECHAZADA -> ruta de descarte ({resultado_vision.get('motivo')})")
        log_descarte(datetime.now(), resultado_vision)

        # Tolerancia un poco mas floja (3mm) que el default (1.5mm), como
        # margen de seguridad - la coordenada de DESCARTE ya fue
        # recalibrada arriba al punto real donde el brazo se detiene, asi
        # que no deberia necesitar mucho margen extra.
        if not move_safe(device, *DESCARTE, plc=plc, tol=3.0):
            return False, False
        marca("DESCARTE")

        device.grip(False)
        time.sleep(0.5)
        marca("grip abrir + pausa (descarte)")

        activar_banda_descarte(plc)
        if not move_safe(device, *DESPEGUE_DESCARTE, plc=plc):
            return False, False
        marca("despegue (descarte) + banda activada")

    # Posicion de espera hasta el siguiente ciclo. En la ruta PLACE la
    # confirmacion al PLC ya se mando antes (justo al despegar, ver arriba)
    # para no retrasar la banda principal; en la ruta DESCARTE todavia no
    # se ha mandado y la manda main() como antes.
    move_safe(device, *HOME, plc=plc)
    marca("->HOME")

    if not resultado_vision["aprobada"]:
        desactivar_banda_descarte(plc)
        marca("banda descarte desactivada")

    return True, resultado_vision["aprobada"]


def main():
    plc = snap7.client.Client()
    plc.connect(PLC_IP, RACK, SLOT)
    device = Dobot(port=DOBOT_PORT)
    device.speed(velocity=380, acceleration=380)

    # La camara se abre UNA SOLA VEZ aqui y se mantiene abierta toda la
    # sesion: abrir cv2.VideoCapture desde cero tarda ~3.2s (overhead de
    # inicializacion), mientras que leer un frame con la camara ya abierta
    # tarda ~0.2s. Reabrirla en cada ciclo agregaria ~30% al tiempo de ciclo
    # sin necesidad. Si la camara no abre, el sistema sigue funcionando SIN
    # vision (todas las piezas se tratan como aprobadas) en vez de parar
    # toda la produccion por un problema de la camara.
    # Se usa el symlink fijo /dev/camara_dobot (regla de udev en
    # /etc/udev/rules.d/99-camara-dobot.rules, agregada 2026-10-07) en vez
    # de un indice numerico (/dev/video0, /dev/video1...) que puede cambiar
    # cada vez que se desconecta/reconecta la camara fisicamente. La regla
    # identifica la camara por su idVendor:idProduct (058f:3841) y por
    # ID_V4L_CAPABILITIES=capture (para evitar el nodo de metadata).
    cap = cv2.VideoCapture('/dev/camara_dobot')
    # Resolucion reducida (de 1600x1200 a 800x600) para bajar la demanda de
    # ancho de banda/energia USB de la camara justo en el momento de la
    # captura - prueba 2026-10-07 para ver si alivia el pico de consumo que
    # coincide con los errores de comunicacion serial del Dobot. Si se
    # cambia esto, hay que re-escalar RX/RY/RW/RH y minRadius/maxRadius
    # (estan calibrados para 1600x1200 - ver constantes arriba).
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 800)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 600)
    vision_disponible = cap.isOpened()
    if vision_disponible:
        tomar_foto(cap)  # primer frame de calentamiento, se descarta
        print("Camara de vision inicializada correctamente.")
    else:
        print("ADVERTENCIA: no se pudo abrir la camara. El sistema seguira "
              "funcionando SIN deteccion de defectos (todas las piezas se "
              "trataran como aprobadas) hasta que se resuelva.")

    # Modo_Dobot se activa UNA SOLA VEZ al arrancar el script, y se mantiene
    # activo toda la sesion (no por cada ciclo) - es lo que le permite a
    # FC130 arrancar en primer lugar. Si el reset del panel lo tumba,
    # systemd (Restart=always) reinicia el script solo en ~5s y lo vuelve
    # a activar - no hace falta reintento dentro del script (confirmado
    # 2026-10-06, revertido un intento de reintento interno por pedido del
    # usuario: "mas facil reemplazar el codigo de antes").
    set_modo_dobot(plc, True)

    if not mover_a_home_seguro(device, plc):
        print("No se pudo llevar el Dobot a HOME. Revisar manualmente la posicion "
              "del brazo antes de reintentar - no se va a arrancar el ciclo.")
        set_modo_dobot(plc, False)
        device.close()
        if vision_disponible:
            cap.release()
        plc.disconnect()
        return

    # Aseguramos que la pinza arranque abierta - si quedo cerrada de una
    # sesion anterior, el primer ciclo "recogeria" sin tener nada sujeto.
    device.grip(False)
    time.sleep(0.5)

    print("Modo_Dobot activado. Esperando senal Q136.0... (Ctrl+C para salir)")
    falla_inicio = None
    part_av_anterior = False
    t_llegada_tapa = None
    try:
        while True:
            # Medicion de diagnostico: cuanto tarda desde que llega la pieza
            # a Part_AV hasta que el PLC manda la senal Q136.0 (espera a
            # Handling + latencia de FC130).
            pa_actual = get_bool(plc.eb_read(0, 1), 0, 0)
            if pa_actual and not part_av_anterior:
                t_llegada_tapa = datetime.now()
            part_av_anterior = pa_actual

            # Registro de OEE_F: cualquier ventana con Stop/Em_Stop activo
            # cuenta como una falla, independiente de si hay un ciclo en
            # curso o no.
            paro_ahora = hay_paro(plc)
            if paro_ahora and falla_inicio is None:
                falla_inicio = datetime.now()
            elif not paro_ahora and falla_inicio is not None:
                log_falla(falla_inicio, datetime.now(), 'Stop/Em_Stop')
                falla_inicio = None

            # Si algo externo (Stop, Em_Stop, Reset) apago Modo_Dobot, el
            # PLC ya libero todo solo (ese es su trabajo) - pero avisamos
            # claro en vez de quedarnos esperando en silencio para siempre.
            m_actual = plc.mb_read(MODO_DOBOT_BYTE, 1)
            if not get_bool(m_actual, 0, MODO_DOBOT_BIT):
                print("Modo_Dobot fue desactivado externamente (Stop/Reset/Em_Stop). "
                      "Deteniendo el script - reiniciar manualmente cuando este resuelto.")
                break

            data = plc.ab_read(136, 1)
            if get_bool(data, 0, 0):
                t_inicio = datetime.now()
                if t_llegada_tapa is not None:
                    print(f"  [Part_AV->Q136.0] {(t_inicio - t_llegada_tapa).total_seconds():.2f}s")
                print("Senal detectada, ejecutando movimiento...")
                completo, ya_confirmado = pick_and_place(device, plc, cap)
                log_ciclo(t_inicio, datetime.now(), 'Completo' if completo else 'Interrumpido')

                if completo:
                    if not ya_confirmado:
                        confirmar_ciclo_plc(plc)
                    print("Ciclo completo.")
                else:
                    print("Ciclo interrumpido por Stop/paro de emergencia. "
                          "No se confirma tarea; el watchdog del PLC libera todo solo.")

            time.sleep(0.2)
    except KeyboardInterrupt:
        print("Detenido por el usuario.")
    finally:
        if falla_inicio is not None:
            log_falla(falla_inicio, datetime.now(), 'Stop/Em_Stop (al cerrar el script)')
        set_modo_dobot(plc, False)
        device.close()
        if vision_disponible:
            cap.release()
        plc.disconnect()


if __name__ == '__main__':
    main()
