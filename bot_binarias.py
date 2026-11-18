"""
Bot EMA 9/21 + RSI 14 para IQ Option - BINARIAS (expiración larga) - basado en v2

Diferencias con el bot turbo:
 - Velas de 5 minutos (TIMEFRAME = 300) y expiración de 15 minutos (EXPIRATION = 15).
   Con 5 minutos o menos la librería envía la orden como turbo; con más, como binaria.
 - Cada operación dura hasta ~30 minutos; el bot analiza una vez por vela de 5 min.
 - Mismos controles: monto progresivo solo al ganar, Stop Loss / Take Profit,
   protección de ganancias, pausa tras pérdidas, CSV (trades_binarias.csv).

Requisitos:  pip install pandas
             pip install git+https://github.com/iqoptionapi/iqoptionapi.git
"""

import csv
import logging
import os
import sys
import time
from datetime import datetime

import pandas as pd
from iqoptionapi.stable_api import IQ_Option

# ========================= CONFIGURACIÓN =========================
EMAIL = "Gabrieldiazes10@gmail.com"
PASSWORD = "Gabriel331224."
ACCOUNT_TYPE = "PRACTICE"     # "PRACTICE" (demo) o "REAL" (dinero real)

ASSET = "XAU/XAG"              # En fin de semana: "EURUSD-OTC"
TIMEFRAME = 300               # Velas de 5 minutos (segundos)
N_CANDLES = 100
EXPIRATION = 15               # minutos (más de 5 = binaria)

# --- Gestión de monto ---
BASE_RISK_PCT = 0.10          # Monto base = 10% del saldo al iniciar (mín. $1)
MIN_AMOUNT = 1.0
WIN_STEP_PCT = 0.25           # +25% del monto base por cada ganada seguida
MAX_STAKE_PCT = 0.20          # Tope: nunca más del 20% del saldo actual

# --- Límites de sesión ---
STOP_LOSS_PCT = 0.20
TAKE_PROFIT_PCT = 0.15
BREAKEVEN_TRIGGER = 0.08      # Al llegar a +8%, el Stop Loss sube al saldo inicial

# --- Control de pérdidas ---
MAX_CONSEC_LOSSES = 3
COOLDOWN_MIN = 30             # Pausa tras 3 pérdidas seguidas

# --- Estrategia ---
EMA_FAST, EMA_SLOW, RSI_PERIOD = 9, 21, 14
USE_TREND_FILTER = True
EMA_TREND = 50

MAX_CONSECUTIVE_ERRORS = 5
CSV_FILE = "trades_binarias.csv"
# =================================================================

logging.disable(logging.CRITICAL)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------- Conexión -------------------------
def connect() -> IQ_Option:
    api = IQ_Option(EMAIL, PASSWORD)
    ok, reason = api.connect()
    if not ok:
        log(f"❌ No se pudo conectar: {reason}")
        sys.exit(1)

    if ACCOUNT_TYPE not in ("PRACTICE", "REAL"):
        log("❌ ACCOUNT_TYPE debe ser 'PRACTICE' o 'REAL'.")
        sys.exit(1)

    api.change_balance(ACCOUNT_TYPE)

    try:
        mode = api.get_balance_mode()
        if mode and str(mode).upper() != ACCOUNT_TYPE:
            log(f"❌ La cuenta activa es '{mode}', no {ACCOUNT_TYPE}. Abortando.")
            sys.exit(1)
    except AttributeError:
        pass

    # Solo binarias/turbo (rápido). update_ACTIVES_OPCODE() completo se cuelga.
    try:
        api.get_ALL_Binary_ACTIVES_OPCODE()
    except Exception as e:
        log(f"⚠️ No se pudo actualizar la lista de activos: {type(e).__name__}: {e}")

    try:
        from iqoptionapi import constants
        if ASSET not in constants.ACTIVES:
            log(f"⚠️ El activo '{ASSET}' no está en la lista de la librería. "
                f"Revisa el nombre exacto con buscar_activo.py")
    except Exception:
        pass

    if EXPIRATION <= 5:
        log("⚠️ EXPIRATION <= 5: la orden se enviará como TURBO, no como binaria.")

    log(f"✅ Conectado | Cuenta {ACCOUNT_TYPE} | Saldo: ${api.get_balance():.2f}")
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


# ------------------------- Indicadores -------------------------
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


def get_signal(df: pd.DataFrame):
    """Devuelve (direccion | None, rsi, motivo)."""
    min_len = max(EMA_SLOW, RSI_PERIOD, EMA_TREND if USE_TREND_FILTER else 0) + 2
    if len(df) < min_len:
        return None, None, "pocas velas"

    close = df["close"]
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
    ema_trend = close.ewm(span=EMA_TREND, adjust=False).mean()
    rsi = float(compute_rsi(close, RSI_PERIOD).iloc[-1])

    cross_up = ema_fast.iloc[-2] <= ema_slow.iloc[-2] and ema_fast.iloc[-1] > ema_slow.iloc[-1]
    cross_down = ema_fast.iloc[-2] >= ema_slow.iloc[-2] and ema_fast.iloc[-1] < ema_slow.iloc[-1]
    price = close.iloc[-1]
    trend = ema_trend.iloc[-1]

    if cross_up and 50 <= rsi <= 70:
        if USE_TREND_FILTER and price < trend:
            return None, rsi, "cruce alcista pero precio bajo EMA 50"
        return "call", rsi, ""
    if cross_down and 30 <= rsi <= 50:
        if USE_TREND_FILTER and price > trend:
            return None, rsi, "cruce bajista pero precio sobre EMA 50"
        return "put", rsi, ""
    return None, rsi, ""


# ------------------------- Gestión de monto y riesgo -------------------------
def next_stake(base: float, consec_wins: int, balance: float) -> float:
    stake = base * (1 + WIN_STEP_PCT * consec_wins)
    stake = min(stake, balance * MAX_STAKE_PCT)
    stake = max(MIN_AMOUNT, round(stake, 2))
    return min(stake, round(balance, 2))


def check_limits(balance: float, stop_loss: float, take_profit: float) -> bool:
    if balance <= stop_loss:
        log(f"🛑 STOP LOSS alcanzado | Saldo: ${balance:.2f} (límite ${stop_loss:.2f})")
        return True
    if balance >= take_profit:
        log(f"🎯 TAKE PROFIT alcanzado | Saldo: ${balance:.2f} (objetivo ${take_profit:.2f})")
        return True
    return False


# ------------------------- Registro CSV -------------------------
def write_csv(direction, amount, rsi, outcome, profit, balance) -> None:
    new_file = not os.path.exists(CSV_FILE)
    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["fecha", "activo", "direccion", "monto", "rsi",
                            "resultado", "profit", "saldo"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ASSET, direction,
                        f"{amount:.2f}", f"{rsi:.1f}", outcome, f"{profit:.2f}", f"{balance:.2f}"])
    except Exception as e:
        log(f"⚠️ No se pudo escribir el CSV: {e}")


# ------------------------- Ejecución -------------------------
def expected_expiry(now: float, duration_min: int) -> float:
    """
    Estima cuándo vence la orden: primero 5 vencimientos por minuto y luego los
    múltiplos de 15 minutos (:00, :15, :30, :45) con más de 5 min restantes.
    Se elige el primero con al menos 'duration_min' minutos por delante.
    """
    start = int(now) // 60 * 60
    first = start + 60 if (start + 60 - now) > 30 else start + 120
    candidates = [first + 60 * i for i in range(5)]
    t = candidates[-1] + 60
    added = 0
    while added < 50:
        if t % 900 == 0 and t - now > 300:
            candidates.append(t)
            added += 1
        t += 60
    for t in candidates:
        if t - now >= duration_min * 60:
            return t
    return now + duration_min * 60 + 900


def closed_result(api: IQ_Option, order_id):
    """Lee el cierre que el servidor envía por websocket (no consulta get_betinfo)."""
    try:
        data = api.get_async_order(order_id)
        msg = data.get("option-closed", {}).get("msg", {})
        win = msg.get("win")
        if win:
            return str(win).lower()
    except Exception:
        pass
    return None


def wait_result(api: IQ_Option, balance_before: float, order_id):
    """
    Espera el resultado sin usar check_win (get_betinfo da timeout).
    Ganada/empate: el saldo sube al vencer. Perdida: no cambia hasta el vencimiento.
    """
    now = time.time()
    deadline = expected_expiry(now, EXPIRATION) + 20
    log(f"⏳ Vence aprox. a las {datetime.fromtimestamp(deadline - 20):%H:%M:%S}")

    time.sleep(2)
    after_buy = safe_balance(api, balance_before - MIN_AMOUNT)

    while time.time() < deadline:
        time.sleep(5)
        if closed_result(api, order_id):
            break
        bal = safe_balance(api, after_buy)
        if abs(bal - after_buy) > 0.005:
            break
        try:
            ensure_connection(api)
        except SystemExit:
            raise

    time.sleep(2)
    profit = safe_balance(api, after_buy) - balance_before
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
    log(f"📤 {direction.upper()} {ASSET} | ${amount:.2f} | {EXPIRATION} min | ID {order_id} "
        f"| esperando resultado...")
    return wait_result(api, balance_before, order_id)


def wait_next_candle() -> None:
    time.sleep(TIMEFRAME - (time.time() % TIMEFRAME) + 2)


def main() -> None:
    api = connect()

    if ACCOUNT_TYPE == "REAL":
        print("\n⚠️  ESTÁS A PUNTO DE OPERAR CON DINERO REAL ⚠️")
        print(f"   Saldo real: ${api.get_balance():.2f}")
        if input("   Escribe SI para continuar: ").strip().upper() != "SI":
            log("Cancelado por el usuario.")
            sys.exit(0)

    start_balance = api.get_balance()
    base_amount = max(MIN_AMOUNT, round(start_balance * BASE_RISK_PCT, 2))
    stop_loss = start_balance * (1 - STOP_LOSS_PCT)
    take_profit = start_balance * (1 + TAKE_PROFIT_PCT)
    breakeven_level = start_balance * (1 + BREAKEVEN_TRIGGER)
    breakeven_locked = False
    last_balance = start_balance

    log("=" * 60)
    log(f"BINARIAS | {ASSET} | velas {TIMEFRAME // 60} min | expiración {EXPIRATION} min")
    log(f"Saldo inicial: ${start_balance:.2f} | Monto base: ${base_amount:.2f}")
    log(f"Stop Loss: ${stop_loss:.2f} | Take Profit: ${take_profit:.2f}")
    log(f"Protección: al llegar a ${breakeven_level:.2f} el Stop Loss sube a ${start_balance:.2f}")
    log(f"Filtro de tendencia EMA {EMA_TREND}: {'ACTIVO' if USE_TREND_FILTER else 'apagado'}")
    log("=" * 60)

    if base_amount > start_balance:
        log("❌ Saldo insuficiente para el monto mínimo.")
        sys.exit(1)

    wins = losses = draws = 0
    consec_wins = consec_losses = 0
    errors = 0

    try:
        while True:
            try:
                ensure_connection(api)
                wait_next_candle()

                balance = safe_balance(api, last_balance)
                last_balance = balance
                if check_limits(balance, stop_loss, take_profit):
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

                amount = next_stake(base_amount, consec_wins, balance)
                log(f"📈 Señal {direction.upper()} | RSI {rsi:.1f} | "
                    f"Racha: {consec_wins}G | Monto: ${amount:.2f}")

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
                    consec_wins = 0
                    consec_losses += 1
                    log(f"❌ PERDIDA {profit:+.2f} (monto vuelve al base)")
                else:
                    draws += 1
                    log("➖ EMPATE")

                balance = safe_balance(api, last_balance)
                last_balance = balance
                log(f"💰 Saldo actualizado: ${balance:.2f} | W:{wins} L:{losses} E:{draws}")
                write_csv(direction, amount, rsi, outcome, profit, balance)
                errors = 0

                if not breakeven_locked and balance >= breakeven_level:
                    stop_loss = max(stop_loss, start_balance)
                    breakeven_locked = True
                    log(f"🔒 Ganancia protegida: Stop Loss sube a ${stop_loss:.2f}")

                if check_limits(balance, stop_loss, take_profit):
                    break

                if consec_losses >= MAX_CONSEC_LOSSES:
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
    log(f"Sesión terminada | Inicio ${start_balance:.2f} → Final ${final:.2f} "
        f"({(final / start_balance - 1) * 100:+.1f}%)")
    log(f"Operaciones: W:{wins} L:{losses} E:{draws} | Tasa de acierto: {rate:.1f}%")
    log(f"Registro guardado en: {os.path.abspath(CSV_FILE)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
