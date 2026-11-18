"""
Lista activos de binarias/turbo (1 minuto) y su payout, o busca por palabra clave.
No usa get_all_open_time(), que falla en algunas versiones de iqoptionapi.

Uso:  py buscar_activo.py
  - Con KEYWORDS = [...]  -> muestra solo los activos que coincidan.
  - Con KEYWORDS = []     -> muestra todos los abiertos, ordenados por payout.
"""

import logging
import sys

from iqoptionapi.stable_api import IQ_Option

logging.disable(logging.CRITICAL)

EMAIL = "Gabrieldiazes10@gmail.com"
PASSWORD = "Gabriel331224."

# Palabras a buscar (sin importar mayúsculas). Déjala vacía [] para ver todos.
KEYWORDS = []


def cargar_datos(api):
    for metodo in ("get_all_init_v2", "get_all_init"):
        fn = getattr(api, metodo, None)
        if fn is None:
            continue
        try:
            data = fn()
            if data:
                return data
        except Exception:
            continue
    return None


def main():
    api = IQ_Option(EMAIL, PASSWORD)
    ok, reason = api.connect()
    if not ok:
        print(f"No se pudo conectar: {reason}")
        sys.exit(1)
    api.change_balance("PRACTICE")

    data = cargar_datos(api)
    if not data:
        print("No se pudieron cargar los datos de activos. Intenta de nuevo.")
        sys.exit(1)

    filas = []
    for categoria in ("turbo", "binary"):
        activos = data.get(categoria, {}).get("actives", {})
        for _, a in activos.items():
            nombre = str(a.get("name", "")).split(".", 1)[-1]
            if KEYWORDS and not any(k.upper() in nombre.upper() for k in KEYWORDS):
                continue
            abierto = bool(a.get("enabled")) and not a.get("is_suspended")
            if not KEYWORDS and not abierto:
                continue
            try:
                comision = a["option"]["profit"]["commission"]
                payout = (100.0 - comision) / 100.0
            except Exception:
                payout = None
            filas.append((categoria, nombre, abierto, payout))

    filas.sort(key=lambda f: (f[0], f[3] is None, -(f[3] or 0)))

    print(f"{'CATEGORÍA':<10}{'NOMBRE':<28}{'ESTADO':<10}{'PAYOUT'}")
    print("-" * 56)
    for categoria, nombre, abierto, payout in filas:
        estado = "ABIERTO" if abierto else "cerrado"
        txt = f"{payout * 100:.0f}%" if payout is not None else "-"
        print(f"{categoria:<10}{nombre:<28}{estado:<10}{txt}")

    if not filas:
        print("No se encontró ningún activo con esas palabras en tu cuenta.")
    print("\nturbo = 1 a 5 minutos | binary = expiraciones más largas")


if __name__ == "__main__":
    main()