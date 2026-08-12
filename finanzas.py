"""
Motor de cálculo de rendimientos (TIR / XIRR) para EML Finance.

Idea general:
- Cada instrumento tiene una lista de "flujos de fondos" en el tiempo: compras
  (negativos), ventas / cupones / dividendos cobrados (positivos), y un flujo
  final positivo igual al valor de mercado actual (como si se vendiera hoy).
- La TIR anualizada (XIRR) es la tasa que hace que el valor presente de todos
  esos flujos sea cero. A diferencia del "% desde el precio de compra" que ya
  mostraba /cotizaciones, esto SÍ es comparable entre instrumentos comprados
  en fechas distintas y por partes (lotes).

Supuestos de origen de datos (documentados para poder ajustarlos si algo no
cierra al revisar los números reales):
- Los importes de "Cupones" y "Dividendos" del reporte de Balanz están en
  pesos (ARS), confirmado por Eduardo.
- Los "Gastos" (comisiones) se suman al costo de compra; no se restan de las
  ventas porque el reporte no separa un gasto propio de venta.
- Todo se calcula también en USD, convirtiendo cada flujo al dólar MEP vigente
  en la fecha de ESE flujo (dato que ya viene en el reporte de Balanz). Así, la
  TIR en USD ya neutraliza el efecto cambiario: si es positiva, la inversión le
  ganó a la estrategia de "comprar dólares y no hacer nada".
"""

from datetime import date, datetime


# ── Bucket / horizonte de inversión ───────────────────────────────────────────
# Según la estrategia documentada: Bucket 1 = liquidez en USD para anticipo de
# depto (~1 año). Bucket 2 = crecimiento patrimonial de largo plazo (3-5+ años).
# Todo lo que no está listado explícitamente cae en 'Sin clasificar' para que
# sea fácil de notar y reasignar acá mismo.
BUCKETS = {
    'AO27': 'Anticipo depto (corto plazo)',
    'FIMARFDA': 'Anticipo depto (corto plazo)',

    'GD35': 'Largo plazo',
    'VSCVO': 'Largo plazo',
    'TTCDO': 'Largo plazo',
    'YMCJO': 'Largo plazo',
    'AAPL': 'Largo plazo', 'AMD': 'Largo plazo', 'AMZN': 'Largo plazo',
    'DISN': 'Largo plazo', 'FDX': 'Largo plazo', 'KO': 'Largo plazo',
    'MELI': 'Largo plazo', 'META': 'Largo plazo', 'NVDA': 'Largo plazo',
    'PFE': 'Largo plazo', 'QQQ': 'Largo plazo', 'SMH': 'Largo plazo',
    'SPY': 'Largo plazo', 'TSLA': 'Largo plazo', 'XLE': 'Largo plazo',
    'BBAR': 'Largo plazo', 'BMA': 'Largo plazo', 'GGAL': 'Largo plazo',
    'PAMP': 'Largo plazo', 'TGSU2': 'Largo plazo', 'YPFD': 'Largo plazo',
}


def get_bucket(ticker):
    return BUCKETS.get(ticker, 'Sin clasificar')


# ── XIRR ──────────────────────────────────────────────────────────────────────
def _to_date(d):
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], '%Y-%m-%d').date()


def _npv(rate, flujos, t0):
    total = 0.0
    for d, monto in flujos:
        dias = (d - t0).days
        total += monto / ((1.0 + rate) ** (dias / 365.0))
    return total


def xirr(flujos, guess=0.15):
    """
    flujos: lista de (fecha, monto) — monto negativo = salida de plata,
    positivo = entrada. Devuelve la tasa anualizada (0.15 = 15%) o None si no
    se puede calcular (muy pocos flujos, o no converge).
    """
    flujos = [(_to_date(d), float(m)) for d, m in flujos if m is not None]
    if len(flujos) < 2:
        return None
    flujos.sort(key=lambda x: x[0])
    if flujos[0][1] >= 0 or flujos[-1][1] <= 0:
        # Sin al menos una salida y una entrada de fondos no hay TIR posible.
        # (puede pasar en posiciones muy chicas con solo cupones, por ejemplo)
        pass

    t0 = flujos[0][0]
    rate = guess
    for _ in range(200):
        npv = _npv(rate, flujos, t0)
        eps = 1e-6
        npv2 = _npv(rate + eps, flujos, t0)
        deriv = (npv2 - npv) / eps
        if abs(deriv) < 1e-12:
            break
        nuevo = rate - npv / deriv
        if nuevo <= -0.999999:
            nuevo = (rate - 0.999999) / 2
        if abs(nuevo - rate) < 1e-9:
            rate = nuevo
            break
        rate = nuevo

    npv_final = _npv(rate, flujos, t0)
    if abs(npv_final) > max(1.0, abs(flujos[0][1]) * 0.01):
        # Newton no convergió de forma confiable: probamos bisección en un
        # rango amplio como red de seguridad.
        lo, hi = -0.99, 10.0
        npv_lo, npv_hi = _npv(lo, flujos, t0), _npv(hi, flujos, t0)
        if npv_lo * npv_hi > 0:
            return None  # no hay cambio de signo, no se puede acotar la raíz
        for _ in range(200):
            mid = (lo + hi) / 2
            npv_mid = _npv(mid, flujos, t0)
            if abs(npv_mid) < 1e-6:
                return mid
            if npv_lo * npv_mid < 0:
                hi = mid
            else:
                lo, npv_lo = mid, npv_mid
        return (lo + hi) / 2

    if -0.999 < rate < 50:
        return rate
    return None


# ── Construcción de flujos por ticker a partir de la tabla `operaciones` ─────
def flujos_por_ticker(operaciones_ticker, valor_actual_ars, valor_actual_usd,
                       fecha_valuacion):
    """
    operaciones_ticker: filas de la tabla `operaciones` para un ticker
    (dicts con: tipo_mov, fecha, cantidad, precio, gastos, moneda, mep,
    monto_ars).
    Devuelve (flujos_ars, flujos_usd) listos para pasarle a xirr().
    """
    flujos_ars, flujos_usd = [], []

    for op in operaciones_ticker:
        f = _to_date(op['fecha'])
        mep = op.get('mep')

        if op['tipo_mov'] == 'compra':
            cantidad = op['cantidad'] or 0
            precio = op['precio'] or 0
            gastos = op.get('gastos') or 0
            if op.get('moneda') == 'Dólares':
                monto_usd = -(cantidad * precio + (gastos / mep if mep else 0))
                monto_ars = monto_usd * mep if mep else None
            else:
                monto_ars = -(cantidad * precio + gastos)
                monto_usd = monto_ars / mep if mep else None

        elif op['tipo_mov'] == 'venta':
            cantidad = op['cantidad'] or 0
            precio = op['precio'] or 0
            if op.get('moneda') == 'Dólares':
                monto_usd = cantidad * precio
                monto_ars = monto_usd * mep if mep else None
            else:
                monto_ars = cantidad * precio
                monto_usd = monto_ars / mep if mep else None

        else:  # cupon / dividendo — reportados en ARS
            monto_ars = op.get('monto_ars') or 0
            monto_usd = monto_ars / mep if mep else None

        if monto_ars is not None:
            flujos_ars.append((f, monto_ars))
        if monto_usd is not None:
            flujos_usd.append((f, monto_usd))

    if valor_actual_ars:
        flujos_ars.append((fecha_valuacion, valor_actual_ars))
    if valor_actual_usd:
        flujos_usd.append((fecha_valuacion, valor_actual_usd))

    return flujos_ars, flujos_usd


def resumen_ticker(ticker, operaciones_ticker, valor_actual_ars,
                    valor_actual_usd, fecha_valuacion):
    flujos_ars, flujos_usd = flujos_por_ticker(
        operaciones_ticker, valor_actual_ars, valor_actual_usd, fecha_valuacion
    )
    invertido_ars = -sum(m for _, m in flujos_ars if m < 0)
    rentas_ars = sum(m for f, m in flujos_ars if m > 0 and f != fecha_valuacion)
    invertido_usd = -sum(m for _, m in flujos_usd if m < 0)
    rentas_usd = sum(m for f, m in flujos_usd if m > 0 and f != fecha_valuacion)

    return {
        'ticker': ticker,
        'bucket': get_bucket(ticker),
        'invertido_ars': invertido_ars,
        'invertido_usd': invertido_usd,
        'rentas_cobradas_ars': rentas_ars,
        'rentas_cobradas_usd': rentas_usd,
        'valor_actual_ars': valor_actual_ars,
        'valor_actual_usd': valor_actual_usd,
        'tir_ars': xirr(flujos_ars),
        'tir_usd': xirr(flujos_usd),
        'n_flujos': len(flujos_ars),
    }
