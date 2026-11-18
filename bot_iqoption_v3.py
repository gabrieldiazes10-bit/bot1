"""
Bot EMA 9/21 + RSI 14 para IQ Option (pensado para cuenta PRACTICE) - VERSIÓN 3
Plan de crecimiento por FASES DE RIESGO PROGRESIVO

  FASE 1 - Supervivencia  ($0   - $25 ): stake FIJO $1.00, sin progresión.
  FASE 2                  ($25  - $75 ): 5.0% del saldo por operación.
  FASE 3                  ($75  - $200): 4.0% del saldo por operación.
  FASE 4                  ($200 - $363): 3.5% del saldo por operación.
  META                    (>= $363    ): el bot se detiene (cubre comisión de retiro).

Protecciones:
  * Stop Loss diario (-20% sobre el saldo con que empezó el DÍA) con candado:
    si se activa, el bot no vuelve a operar hasta el día siguiente.
  * Fase 1: 2 pérdidas seguidas -> pausa (y cierre si se repite).
  * Fases 2-4: 3 pérdidas seguidas -> pausa de enfriamiento.
  * Anti-martingala: el stake solo sube tras GANADAS, con tope por fase.
  * Filtro de payout mínimo (82%) validado antes de CADA operación.
  * Filtro de ruido (demasiados cruces EMA recientes = mercado lateral/errático).
  * Filtro de tendencia EMA 50, Take Profit, Breakeven a +8%, reconexión.

Requisitos:  pip install pandas
             pip install git+https://github.com/iqoptionapi/iqoptionapi.git
"""

import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime

import pandas as pd
from iqoptionapi.stable_api import IQ_Option

# ========================= CONFIGURACIÓN =========================
# Credenciales: variables de entorno IQ_EMAIL / IQ_PASSWORD (o se piden por consola)
ACCOUNT_TYPE = "REAL"      # "PRACTICE" (demo) o "REAL"

ASSET = "USDJPY-op"          # Entre semana puedes usar "EURUSD"
TIMEFRAME = 60
N_CANDLES = 100
EXPIRATION = 1                 # minutos

# --- Fases de capital: (nombre, saldo_min, saldo_max, % de riesgo) ---
# % de riesgo = None -> monto FIJO (fase de supervivencia)
PHASES = [
    ("F1 Supervivencia", 0.0,   25.0,  None),
    ("F2",               25.0,  75.0,  0.05),
    ("F3",               75.0,  200.0, 0.04),
    ("F4",               200.0, 363.0, 0.035),
]
FIXED_STAKE = 1.00             # Monto fijo de la fase 1 (mínimo de la plataforma)
MIN_AMOUNT = 1.00
GOAL_BALANCE = 363.0           # Meta: al alcanzarla el bot se detiene

# --- Anti-martingala (solo fases 2-4) ---
WIN_STEP_PCT = 0.25            # +25% del monto base por cada ganada seguida
MAX_WIN_STEPS = 3              # Máximo de escalones de subida
MAX_STAKE_MULT = 1.5           # Tope: stake <= 1.5 x (% de la fase x saldo)

# --- Límites de sesión/día ---
STOP_LOSS_PCT = 0.20           # Detener si cae 20% desde el saldo inicial del día
TAKE_PROFIT_PCT = 0.15         # Detener si sube 15%
BREAKEVEN_TRIGGER = 0.08       # Al llegar a +8%, el Stop Loss sube al saldo inicial

# --- Control de pérdidas seguidas ---
SURVIVAL_MAX_CONSEC_LOSSES = 2 # Fase 1: pausa tras 2 pérdidas seguidas
SURVIVAL_PAUSE_MIN = 30        # Minutos de pausa en fase 1
SURVIVAL_MAX_PAUSES = 1        # Si ocurre una vez más tras esta pausa -> cierre del día
MAX_CONSEC_LOSSES = 3          # Fases 2-4: pausa tras 3 pérdidas seguidas
COOLDOWN_MIN = 10

# --- Filtros ---
MIN_PAYOUT = 0.82              # No operar si el payout es menor al 82%
PAYOUT_STRICT = True           # True: si no se puede consultar el payout, NO opera
ALLOW_OTC = True               # False: rechaza cualquier activo terminado en -OTC
NOISE_LOOKBACK = 20            # Velas para medir ruido
NOISE_MAX_CROSSES = 4          # Más cruces EMA 9/21 que esto = mercado errático
USE_TREND_FILTER = True        # CALL solo sobre EMA 50, PUT solo bajo EMA 50

# --- Estrategia ---
EMA_FAST, EMA_SLOW, RSI_PERIOD, EMA_TREND = 9, 21, 14, 50

MAX_CONSECUTIVE_ERRORS = 5
CSV_FILE = "trades_log_v3.csv"
STATE_FILE = "daily_state.json"   # Candado diario (bórralo para reiniciar el día manualmente)
# =================================================================

logging.disable(logging.CRITICAL)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------- Conexión -------------------------
def get_balance_timeout(api: IQ_Option, timeout: float = 20.0):
    """
    api.get_balance() puede quedarse en un bucle infinito si el servidor no envía
    los saldos. Se ejecuta en un hilo con límite de tiempo para no colgar el bot.
    """
    result = {}

    def _worker():
        try:
            result["v"] = api.get_balance()
        except Exception as e:
            result["e"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("v")


def connect() -> IQ_Option:
    email = os.getenv("IQ_EMAIL") or input("Email IQ Option: ").strip()
    password = os.getenv("IQ_PASSWORD")
    if not password:
        # getpass en Windows no muestra nada y falla al pegar (Ctrl+V).
        # Por eso se pide visible; escríbela a mano o pégala con clic derecho.
        print("(La contraseña se verá en pantalla mientras la escribes)")
        password = input("Contraseña: ").strip()

    if ACCOUNT_TYPE not in ("PRACTICE", "REAL"):
        log("❌ ACCOUNT_TYPE debe ser 'PRACTICE' o 'REAL'.")
        sys.exit(1)
    if not ALLOW_OTC and ASSET.upper().endswith("-OTC"):
        log("❌ ALLOW_OTC=False y el activo configurado es OTC. Cambia ASSET.")
        sys.exit(1)

    api = IQ_Option(email, password)
    ok, reason = api.connect()
    if not ok:
        log(f"❌ No se pudo conectar: {reason}")
        sys.exit(1)

    api.change_balance(ACCOUNT_TYPE)
    try:
        mode = api.get_balance_mode()
        if mode and str(mode).upper() != ACCOUNT_TYPE:
            log(f"❌ La cuenta activa es '{mode}', no {ACCOUNT_TYPE}. Abortando.")
            sys.exit(1)
    except AttributeError:
        pass

    try:
        api.get_ALL_Binary_ACTIVES_OPCODE()
    except Exception as e:
        log(f"⚠️ No se pudo actualizar la lista de activos: {type(e).__name__}: {e}")

    try:
        from iqoptionapi import constants
        if ASSET not in constants.ACTIVES:
            log(f"⚠️ El activo '{ASSET}' no está en la lista de la librería.")
    except Exception:
        pass

    # Espera a que el servidor envíe los saldos (con reintentos y límite de tiempo)
    time.sleep(2)
    balance = None
    for attempt in range(1, 4):
        balance = get_balance_timeout(api)
        if balance is not None:
            break
        log(f"⚠️ El servidor no envió el saldo (intento {attempt}/3). Reintentando...")
        try:
            api.change_balance(ACCOUNT_TYPE)
        except Exception:
            pass
        time.sleep(3)
    if balance is None:
        log("❌ Conectó, pero IQ Option nunca entregó el saldo. Causas probables:")
        log("   1) Librería iqoptionapi desactualizada o carpeta local 'iqoptionapi' "
            "que tapa a la instalada con pip.")
        log("   2) Python demasiado nuevo (usa 3.11 o 3.12).")
        log("   3) Cuenta con verificación pendiente (2FA) o sesión abierta en otro lado.")
        sys.exit(1)

    log(f"✅ Conectado | Cuenta {ACCOUNT_TYPE} | Saldo: ${balance:.2f}")
    return api


def reconnect(api: IQ_Option) -> bool:
    for attempt in range(1, 4):
        log(f"🔄 Reconectando (intento {attempt}/3)...")
        try:
            ok, reason = api.connect()
            if ok:
                api.change_balance(ACCOUNT_TYPE)
                log("✅ Reconectado")
                return True
            log(f"   Falló: {reason}")
        except Exception as e:
            log(f"   Error al reconectar: {e}")
        time.sleep(5)
    return False


def ensure_connection(api: IQ_Option) -> None:
    try:
        alive = api.check_connect()
    except Exception:
        alive = False
    if not alive and not reconnect(api):
        log("❌ No se pudo restablecer la conexión. Cerrando.")
        sys.exit(1)


def safe_balance(api: IQ_Option, fallback=None):
    try:
        return api.get_balance()
    except Exception:
        if reconnect(api):
            try:
                return api.get_balance()
            except Exception:
                pass
    return fallback


# ------------------------- Estado diario (candado) -------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"⚠️ No se pudo guardar el estado diario: {e}")


# ------------------------- Indicadores y señal -------------------------
def get_closed_candles(api: IQ_Option) -> pd.DataFrame:
    candles = api.get_candles(ASSET, TIMEFRAME, N_CANDLES, time.time())
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles).sort_values("from").reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    return df.iloc[:-1]  # Descarta la vela en formación


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100)


def count_recent_crosses(ema_fast: pd.Series, ema_slow: pd.Series, lookback: int) -> int:
    """Cuenta cuántas veces se cruzaron las EMA en las últimas `lookback` velas."""
    sign = (ema_fast - ema_slow).iloc[-lookback:].apply(lambda x: 1 if x > 0 else -1)
    return int((sign != sign.shift()).iloc[1:].sum())


def get_signal(df: pd.DataFrame):
    """Devuelve (direccion | None, rsi, motivo)."""
    min_len = max(EMA_SLOW, RSI_PERIOD, EMA_TREND if USE_TREND_FILTER else 0,
                  NOISE_LOOKBACK) + 2
    if len(df) < min_len:
        return None, None, "pocas velas"

    close = df["close"]
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
    ema_trend = close.ewm(span=EMA_TREND, adjust=False).mean()
    rsi = float(compute_rsi(close, RSI_PERIOD).iloc[-1])

    cross_up = ema_fast.iloc[-2] <= ema_slow.iloc[-2] and ema_fast.iloc[-1] > ema_slow.iloc[-1]
    cross_down = ema_fast.iloc[-2] >= ema_slow.iloc[-2] and ema_fast.iloc[-1] < ema_slow.iloc[-1]
    price, trend = close.iloc[-1], ema_trend.iloc[-1]

    direction = None
    if cross_up and 50 <= rsi <= 70:
        direction = "call"
        if USE_TREND_FILTER and price < trend:
            return None, rsi, "cruce alcista pero precio bajo EMA 50"
    elif cross_down and 30 <= rsi <= 50:
        direction = "put"
        if USE_TREND_FILTER and price > trend:
            return None, rsi, "cruce bajista pero precio sobre EMA 50"
    if direction is None:
        return None, rsi, ""

    # Filtro de ruido: demasiados cruces recientes = mercado lateral/errático
    crosses = count_recent_crosses(ema_fast, ema_slow, NOISE_LOOKBACK)
    if crosses > NOISE_MAX_CROSSES:
        return None, rsi, f"mercado ruidoso ({crosses} cruces en {NOISE_LOOKBACK} velas)"
    return direction, rsi, ""


# ------------------------- Filtro de payout -------------------------
def get_payout(api: IQ_Option, asset: str):
    """Payout (0-1) del activo para la expiración configurada, o None si no se pudo leer."""
    try:
        profits = api.get_all_profit()
        info = profits.get(asset)
        if not info:
            return None
        key = "turbo" if EXPIRATION <= 5 else "binary"
        val = info.get(key) or info.get("binary")
        if val is None:
            return None
        val = float(val)
        return val / 100 if val > 1 else val
    except Exception as e:
        log(f"⚠️ No se pudo consultar el payout: {type(e).__name__}: {e}")
        return None


def payout_ok(api: IQ_Option):
    """Devuelve (permitido, payout)."""
    if not ALLOW_OTC and ASSET.upper().endswith("-OTC"):
        return False, None
    payout = get_payout(api, ASSET)
    if payout is None:
        return (not PAYOUT_STRICT), None
    return payout >= MIN_PAYOUT, payout


# ------------------------- Fases, monto y límites -------------------------
def get_phase(balance: float):
    """Devuelve (nombre, pct_riesgo | None) según el saldo actual."""
    for name, lo, hi, pct in PHASES:
        if lo <= balance < hi:
            return name, pct
    return PHASES[-1][0], PHASES[-1][3]  # Por encima de la última fase


def next_stake(balance: float, consec_wins: int) -> float:
    """
    Fase 1: monto fijo, sin progresión.
    Fases 2-4: % del saldo ACTUAL; sube solo tras ganadas (anti-martingala) con tope.
    """
    _, pct = get_phase(balance)
    if pct is None:
        return round(min(FIXED_STAKE, balance), 2)

    base = balance * pct
    stake = base * (1 + WIN_STEP_PCT * min(consec_wins, MAX_WIN_STEPS))
    stake = min(stake, base * MAX_STAKE_MULT)          # tope por fase
    stake = max(MIN_AMOUNT, round(stake, 2))
    return round(min(stake, balance), 2)


def check_limits(balance, stop_loss, take_profit):
    """Devuelve None, 'SL', 'TP' o 'GOAL'."""
    if balance <= stop_loss + 1e-9:
        log(f"🛑 STOP LOSS | Saldo: ${balance:.2f} (límite ${stop_loss:.2f})")
        return "SL"
    if balance >= GOAL_BALANCE:
        log(f"🏆 META ALCANZADA | Saldo: ${balance:.2f} (meta ${GOAL_BALANCE:.2f})")
        return "GOAL"
    if balance >= take_profit:
        log(f"🎯 TAKE PROFIT | Saldo: ${balance:.2f} (objetivo ${take_profit:.2f})")
        return "TP"
    return None


# ------------------------- Registro CSV -------------------------
def write_csv(phase, direction, amount, rsi, payout, outcome, profit, balance) -> None:
    new_file = not os.path.exists(CSV_FILE)
    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["fecha", "activo", "fase", "direccion", "monto", "rsi",
                            "payout", "resultado", "profit", "saldo"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ASSET, phase,
                        direction, f"{amount:.2f}", f"{rsi:.1f}",
                        "" if payout is None else f"{payout:.2f}",
                        outcome, f"{profit:.2f}", f"{balance:.2f}"])
    except Exception as e:
        log(f"⚠️ No se pudo escribir el CSV: {e}")


# ------------------------- Ejecución -------------------------
def wait_result(api: IQ_Option, balance_before: float):
    """Detecta el resultado por variación de saldo (check_win suele dar timeout)."""
    now = time.time()
    expiry = int(now // 60) * 60 + EXPIRATION * 60
    if expiry - now < 30:
        expiry += 60
    deadline = expiry + 10

    time.sleep(2)
    after_buy = api.get_balance()
    while time.time() < deadline:
        time.sleep(3)
        if abs(api.get_balance() - after_buy) > 0.005:
            break

    time.sleep(1)
    profit = api.get_balance() - balance_before
    if profit > 0.005:
        return "win", profit
    if profit < -0.005:
        return "loose", profit
    return "equal", 0.0


def place_trade(api: IQ_Option, direction: str, amount: float):
    balance_before = api.get_balance()
    status, order_id = api.buy(amount, ASSET, direction, EXPIRATION)
    if not status:
        log(f"⚠️ Orden rechazada: {order_id}")
        return None
    log(f"📤 {direction.upper()} {ASSET} | ${amount:.2f} | ID {order_id} | esperando resultado...")
    return wait_result(api, balance_before)


def wait_next_candle() -> None:
    time.sleep(60 - (time.time() % 60) + 1)


def main() -> None:
    api = connect()

    if ACCOUNT_TYPE == "REAL":
        print("\n⚠️  ESTÁS A PUNTO DE OPERAR CON DINERO REAL ⚠️")
        print(f"   Saldo real: ${api.get_balance():.2f}")
        if input("   Escribe SI para continuar: ").strip().upper() != "SI":
            log("Cancelado por el usuario.")
            sys.exit(0)

    # ---- Estado diario: el Stop Loss se mide contra el saldo con que empezó el DÍA ----
    today = date.today().isoformat()
    state = load_state()
    current = api.get_balance()
    if state and state.get("date") == today:
        if state.get("locked"):
            log(f"🔒 Candado diario activo ({state.get('reason')}). "
                f"No se opera más hoy. (Borra {STATE_FILE} para forzar reinicio.)")
            sys.exit(0)
        start_balance = float(state["start_balance"])
        breakeven_locked = bool(state.get("breakeven_locked", False))
        log(f"ℹ️ Retomando el día con saldo inicial ${start_balance:.2f}")
    else:
        start_balance = current
        breakeven_locked = False
        state = {"date": today, "start_balance": start_balance,
                 "breakeven_locked": False, "locked": False, "reason": ""}
        save_state(state)

    stop_loss = start_balance * (1 - STOP_LOSS_PCT)
    take_profit = start_balance * (1 + TAKE_PROFIT_PCT)
    breakeven_level = start_balance * (1 + BREAKEVEN_TRIGGER)
    if breakeven_locked:
        stop_loss = max(stop_loss, start_balance)
    last_balance = current

    phase_name, _ = get_phase(current)
    log("=" * 60)
    log(f"Saldo inicial del día: ${start_balance:.2f} | Saldo actual: ${current:.2f} | {phase_name}")
    log(f"Stop Loss: ${stop_loss:.2f} | Take Profit: ${take_profit:.2f} | Meta: ${GOAL_BALANCE:.2f}")
    log(f"Protección: al llegar a ${breakeven_level:.2f} el Stop Loss sube a ${start_balance:.2f}")
    log(f"Payout mínimo: {MIN_PAYOUT:.0%} | Filtro tendencia EMA {EMA_TREND}: "
        f"{'ACTIVO' if USE_TREND_FILTER else 'apagado'} | OTC permitido: {ALLOW_OTC}")
    log("=" * 60)

    if current < MIN_AMOUNT:
        log("❌ Saldo insuficiente para el monto mínimo.")
        sys.exit(1)

    wins = losses = draws = 0
    consec_wins = consec_losses = 0
    survival_pauses = 0
    errors = 0
    stop_reason = None

    def lock_day(reason: str) -> None:
        state.update({"locked": True, "reason": reason})
        save_state(state)

    try:
        while True:
            try:
                ensure_connection(api)
                wait_next_candle()

                balance = safe_balance(api, last_balance)
                last_balance = balance
                stop_reason = check_limits(balance, stop_loss, take_profit)
                if stop_reason:
                    if stop_reason in ("SL", "GOAL"):
                        lock_day(stop_reason)
                    break

                df = get_closed_candles(api)
                if df.empty:
                    log("⚠️ No se recibieron velas (¿activo cerrado?). Reintentando...")
                    continue

                direction, rsi, reason = get_signal(df)
                if direction is None:
                    rsi_txt = f"{rsi:.1f}" if rsi is not None else "N/A"
                    extra = f" | {reason}" if reason else ""
                    log(f"Sin señal | RSI {rsi_txt} | Saldo ${balance:.2f}{extra}")
                    errors = 0
                    continue

                # Filtro de payout validado justo antes de operar
                allowed, payout = payout_ok(api)
                if not allowed:
                    p_txt = f"{payout:.0%}" if payout is not None else "desconocido"
                    log(f"⛔ Señal {direction.upper()} descartada | payout {p_txt} "
                        f"(mínimo {MIN_PAYOUT:.0%}) o activo no permitido")
                    continue

                phase_name, pct = get_phase(balance)
                amount = next_stake(balance, consec_wins)
                p_txt = f"{payout:.0%}" if payout is not None else "N/D"
                log(f"📈 Señal {direction.upper()} | RSI {rsi:.1f} | {phase_name} | "
                    f"Payout {p_txt} | Racha: {consec_wins}G | Monto: ${amount:.2f}")

                res = place_trade(api, direction, amount)
                if res is None:
                    continue

                outcome, profit = res
                if outcome == "win":
                    wins += 1
                    consec_wins += 1
                    consec_losses = 0
                    log(f"✅ GANADA  +${profit:.2f}")
                elif outcome == "loose":
                    losses += 1
                    consec_wins = 0           # vuelve al monto base
                    consec_losses += 1
                    log(f"❌ PERDIDA {profit:+.2f} (racha de pérdidas: {consec_losses})")
                else:
                    draws += 1
                    log("➖ EMPATE")

                balance = safe_balance(api, last_balance)
                last_balance = balance
                log(f"💰 Saldo actualizado: ${balance:.2f} | W:{wins} L:{losses} E:{draws}")
                write_csv(phase_name, direction, amount, rsi, payout, outcome, profit, balance)
                errors = 0

                # Breakeven: protege ganancias
                if not breakeven_locked and balance >= breakeven_level:
                    stop_loss = max(stop_loss, start_balance)
                    breakeven_locked = True
                    state["breakeven_locked"] = True
                    save_state(state)
                    log(f"🔒 Ganancia protegida: Stop Loss sube a ${stop_loss:.2f}")

                stop_reason = check_limits(balance, stop_loss, take_profit)
                if stop_reason:
                    if stop_reason in ("SL", "GOAL"):
                        lock_day(stop_reason)
                    break

                # ---- Control de pérdidas seguidas (según la fase en que se operó) ----
                in_survival = pct is None
                if in_survival and consec_losses >= SURVIVAL_MAX_CONSEC_LOSSES:
                    survival_pauses += 1
                    if survival_pauses > SURVIVAL_MAX_PAUSES:
                        log("🛑 Pérdidas seguidas repetidas en fase de supervivencia. "
                            "Cierre de seguridad por hoy.")
                        lock_day("SURVIVAL_LOSSES")
                        stop_reason = "SURVIVAL"
                        break
                    log(f"⏸️ {consec_losses} pérdidas seguidas (supervivencia). "
                        f"Pausa de {SURVIVAL_PAUSE_MIN} min...")
                    time.sleep(SURVIVAL_PAUSE_MIN * 60)
                    consec_losses = 0
                elif not in_survival and consec_losses >= MAX_CONSEC_LOSSES:
                    log(f"⏸️ {consec_losses} pérdidas seguidas. Pausa de {COOLDOWN_MIN} min...")
                    time.sleep(COOLDOWN_MIN * 60)
                    consec_losses = 0

            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                errors += 1
                log(f"⚠️ Error ({errors}/{MAX_CONSECUTIVE_ERRORS}): {type(e).__name__}: {e}")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log("❌ Demasiados errores seguidos. Cerrando.")
                    break
                reconnect(api)

    except KeyboardInterrupt:
        log("⏹️ Detenido manualmente.")

    final = safe_balance(api, last_balance)
    total = wins + losses
    rate = (wins / total * 100) if total else 0.0
    log("=" * 60)
    log(f"Sesión terminada | Inicio del día ${start_balance:.2f} → Final ${final:.2f} "
        f"({(final / start_balance - 1) * 100:+.1f}%)")
    log(f"Operaciones: W:{wins} L:{losses} E:{draws} | Tasa de acierto: {rate:.1f}%")
    log(f"Registro guardado en: {os.path.abspath(CSV_FILE)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
