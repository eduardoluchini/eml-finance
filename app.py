import os, sqlite3, json, time
from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
from datetime import datetime, date as _date
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests as req

import finanzas

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'eml-finance-secret-2026')

DB_PATH = os.environ.get('DB_PATH', 'finance.db')

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                username   TEXT PRIMARY KEY,
                password   TEXT NOT NULL,
                nombre     TEXT NOT NULL,
                must_change INTEGER DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha       TEXT NOT NULL,
                total_ars   REAL,
                tc_mep      REAL,
                tc_usd      REAL,
                data_json   TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        ''')
        # Usuario inicial
        exists = conn.execute("SELECT 1 FROM usuarios WHERE username='eduardo'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO usuarios VALUES (?,?,?,?)",
                ('eduardo', 'EML2026!', 'Eduardo Luchini', 0)
            )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS precios_iniciales (
                ticker      TEXT PRIMARY KEY,
                precio      REAL NOT NULL,
                fecha       TEXT,
                notas       TEXT
            )
        ''')
        # Seed precios iniciales desde el PDF si la tabla está vacía
        count = conn.execute('SELECT COUNT(*) FROM precios_iniciales').fetchone()[0]
        if count == 0:
            for items in PORTFOLIO_INICIAL['instrumentos'].values():
                for item in items:
                    conn.execute(
                        'INSERT OR IGNORE INTO precios_iniciales VALUES (?,?,?,?)',
                        (item['ticker'], item['precio'], PORTFOLIO_INICIAL['fecha'], 'Precio PDF 03/07/2026')
                    )

        conn.execute('''
            CREATE TABLE IF NOT EXISTS operaciones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                fecha       TEXT NOT NULL,
                tipo_mov    TEXT NOT NULL,   -- compra | venta | cupon | dividendo
                cantidad    REAL,
                precio      REAL,
                gastos      REAL,
                moneda      TEXT,
                mep         REAL,            -- dólar MEP vigente en la fecha de este flujo
                monto_ars   REAL,            -- solo para cupon/dividendo
                abierto     INTEGER DEFAULT 0,
                fuente      TEXT DEFAULT 'balanz_excel'
            )
        ''')
        # Seed histórico de operaciones (Balanz) desde data/operaciones_balanz.json,
        # una sola vez. Ver README / MEJORAS.md — sale del extracto "resultados por
        # info completa" descargado en Balanz > Resultados.
        # Upsert idempotente: cada vez que arranca la app revisa data/operaciones_balanz.json
        # y suma solo las filas que todavía no están en la tabla (por ticker+fecha+tipo_mov+
        # cantidad+precio). Así, cuando agregamos operaciones nuevas al JSON (ej. las compras
        # de un día), entran solas al próximo deploy sin necesidad de vaciar la tabla ni
        # duplicar lo que ya estaba cargado.
        seed_path = os.path.join(os.path.dirname(__file__), 'data', 'operaciones_balanz.json')
        if os.path.exists(seed_path):
            with open(seed_path, encoding='utf-8') as fh:
                seed_ops = json.load(fh)
            existentes = set(
                (r['ticker'], r['fecha'], r['tipo_mov'], r['cantidad'], r['precio'])
                for r in conn.execute('SELECT ticker, fecha, tipo_mov, cantidad, precio FROM operaciones').fetchall()
            )
            nuevas = 0
            for op in seed_ops:
                clave = (op['ticker'], op['fecha'], op['tipo_mov'], op.get('cantidad'), op.get('precio'))
                if clave in existentes:
                    continue
                conn.execute(
                    'INSERT INTO operaciones (ticker, fecha, tipo_mov, cantidad, precio, gastos, moneda, mep, monto_ars, abierto) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (
                        op['ticker'], op['fecha'], op['tipo_mov'],
                        op.get('cantidad'), op.get('precio'), op.get('gastos'),
                        op.get('moneda'), op.get('mep'), op.get('monto_ars'),
                        1 if op.get('abierto') else 0,
                    )
                )
                nuevas += 1
        conn.commit()

def get_precios_iniciales():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM precios_iniciales').fetchall()
    return {r['ticker']: {'precio': r['precio'], 'fecha': r['fecha'], 'notas': r['notas']} for r in rows}

def get_operaciones():
    """Devuelve todas las operaciones agrupadas por ticker."""
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM operaciones ORDER BY fecha ASC').fetchall()
    por_ticker = {}
    for r in rows:
        por_ticker.setdefault(r['ticker'], []).append(dict(r))
    return por_ticker

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'usuario' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Datos cartera (posición 25/09/2026 — reconciliación completa) ──────────────
PORTFOLIO_INICIAL = {
    # 25/09: reconciliación COMPLETA con el resumen de cuenta consolidado de Balanz
    # (ResumenDeCuenta_20260925.pdf, "FULL INVESTMENT HOUSE" — incluye Balanz +
    # fondos Galicia en el total) y la pantalla de posición de Fondos Fima de
    # Galicia. A diferencia de las dos actualizaciones anteriores (10/09 y 11/09,
    # parciales), acá se repricearon TODOS los instrumentos al valor del resumen.
    #
    # Se detectaron 2 cambios de CANTIDAD que no coinciden con lo que tenía cargado
    # (todo lo demás fue únicamente reprecificación, sin cambio de cantidad):
    #  - SMH: 30 -> 35. Esto RESUELVE la ambigüedad de la orden del 10/09 (Precio/
    #    Cantidad Operada en -1 en ordenesdeldia.xlsx, sin confirmar en su momento):
    #    sí se ejecutó. Se cargó la fila de compra en el ledger con fecha 2026-09-10
    #    al precio que ya se había estimado entonces (~$17.950) — pendiente confirmar
    #    el precio real con Eduardo.
    #  - PFE: 34 -> 35. Esta SÍ es nueva — no hay registro de que Eduardo haya
    #    comprado PFE. Puede ser un dividendo reinvertido (DRIP) o una compra que no
    #    se reportó. Se cargó una fila de compra estimada (fecha y precio de hoy)
    #    para no perder la cantidad real, PENDIENTE DE CONFIRMAR con Eduardo.
    #
    # tc_mep $1.549,02 es el que figura en el resumen — dato real, no aproximado
    # (a diferencia de los $1.515,10 usados para las operaciones del 10/09 y 11/09,
    # que siguen siendo una aproximación por no haber podido reconfirmarlos).
    #
    # 'disponibilidad': se actualizó Balanz Pesos/USD/Cable con los datos reales del
    # resumen ("Monedas"). Galicia Pesos/USD y MercadoPago NO se reconfirmaron esta
    # ronda (no vinieron en las capturas) — se mantienen los últimos valores
    # conocidos, así que 'total_ars' de abajo (tomado directo del resumen de Balanz,
    # que sí consolida todo) no va a coincidir exactamente con
    # suma(instrumentos)+disponibilidad — la diferencia es el efectivo de Galicia/
    # MercadoPago no reconfirmado. Mismo patrón que el gap histórico ya documentado.
    #
    # Historial de actualizaciones parciales anteriores (10/09 y 11/09), para
    # referencia:
    # OJO: esto fue una actualización PARCIAL. El 10/09 Eduardo pasó $5.670.636,05
    # nuevos a Balanz y ejecutó las compras de ordenesdeldia.xlsx (ver detalle en
    # data/operaciones_balanz.json, filas del 2026-09-10). Se actualizaron cantidad/
    # precio/valor SOLO de los tickers efectivamente operados ese día (AO27, GD35,
    # PFE, FDX, XLE, AMZN, MELI, AAPL, DISN, KO, TSLA, META, QQQ, AMD, SPY, y las
    # posiciones nuevas BRKB/GOOGL/MSFT). El resto de los instrumentos (Acciones,
    # Bonos que no sean AO27/GD35, Corporativos, Fondos, y también NVDA y SMH entre
    # los Cedears) sigue con precios del 04/09 — no hay un resumen nuevo completo.
    # SMH quedó afuera: la orden figura "Ejecutada" pero con Precio/Cantidad Operada
    # en -1 en el Excel; Eduardo no pudo confirmar si se ejecutó o no, así que no se
    # cargó. Si se confirma, hay que sumarla (5 nominales, ~$17.950 c/u).
    #
    # 11/09: segunda actualización parcial. Eduardo recibió un depósito de $3.000.000
    # y, siguiendo la estrategia acordada (~80% reforzar el bucket de mediano plazo,
    # ~20% reforzar el momentum de largo plazo), ejecutó en Balanz:
    #   AO27:  +1.067 nominales @ $1.574,00  = $1.679.458,00 (orden 107469352, 11:50)
    #   QQQ:   +11 nominales    @ $57.125,00 = $628.375,00   (orden 107467209, 11:43)
    #   BPOD7: +517 nominales   @ $1.592,80  = $823.477,60   (orden 107472243, 12:01)
    # Por separado, en Galicia hubo movimientos (no relacionados con este depósito)
    # sobre FIMAPREM: retiros el 07/09 ($1.736.000) y 09/09 ($1.500.000), altas el
    # 10/09 ($3.200.000) y 11/09 ($1.953.000) — neto +$1.917.000, cantidad pasó de
    # 49.663,34 a 72.282,38 cuotapartes. No se identificó de qué cuenta salió/entró
    # esa plata (no es el depósito de Balanz), así que 'disponibilidad' y 'total_ars'
    # de abajo NO reflejan ese movimiento — están desactualizados en esa parte hasta
    # confirmar el origen/destino. FIMARFDA y FIMARPLUS solo tuvieron revalorización
    # de precio, sin cambio de cantidad (confirmado con la pantalla de posición
    # consolidada de Galicia).
    # MEP usado para las 7 operaciones de hoy (11/09): $1.515,10 — es el último MEP
    # confirmado que tengo (el de la última actualización), NO uno reconfirmado hoy
    # (la búsqueda web y el acceso automatizado a Balanz/Galicia no funcionaron en el
    # momento de cargar esto). Si el MEP real de hoy fue distinto, el tir_usd de estas
    # 7 filas en operaciones_balanz.json va a tener un pequeño margen de error.
    'fecha': '25/09/2026',
    # 25/09: total tomado directo del encabezado del resumen consolidado de Balanz
    # ("FULL INVESTMENT HOUSE"), que ya incluye Balanz + los 3 fondos de Galicia.
    # Ya no es una suma manual — es el número que Balanz calcula. Ver nota de más
    # arriba sobre el gap con disponibilidad (Galicia efectivo/MercadoPago sin
    # reconfirmar).
    'total_ars': 82394211,
    'tc_mep': 1549.02,
    'tc_usd': 1620.36,
    'monedas': {
        'Pesos': -3747.08,
        'Dólares': 0.80,
        'USD Cable': 8.53,
    },
    'disponibilidad': {
        # 25/09: datos reales del resumen de Balanz ("Monedas"). El saldo en pesos
        # está en negativo ($-3.747,08) — probablemente un ajuste/gasto pendiente de
        # liquidación, no un error de carga; así lo muestra el resumen.
        'Balanz Pesos':  {'ars': -3747.08,    'usd': None},
        'Balanz USD':    {'ars': None,        'usd': 0.80},
        'Balanz Cable':  {'ars': None,        'usd': 8.53},
        # No reconfirmado esta ronda (no vino en las capturas) — último valor conocido.
        'Galicia Pesos': {'ars': 612.62,      'usd': None},
        'Galicia USD':   {'ars': None,        'usd': 107.64},
        # No reconfirmado en esta actualización (no vino en las capturas de esta
        # ronda) — se mantiene el último saldo de MercadoPago conocido (12/08/2026).
        'MercadoPago':   {'ars': 1001968.00,  'usd': None},
    },
    'instrumentos': {
        'Acciones': [
            {'ticker': 'BBAR',  'descripcion': 'Banco Frances Escriturales',    'cantidad': 58,  'precio': 7175.00,  'valor': 416150},
            {'ticker': 'BMA',   'descripcion': 'Banco Macro S.A.',              'cantidad': 45,  'precio': 11150.00, 'valor': 501750},
            {'ticker': 'GGAL',  'descripcion': 'Grupo Financiero Galicia',      'cantidad': 402, 'precio': 6335.00,  'valor': 2546670},
            {'ticker': 'PAMP',  'descripcion': 'Pampa Energia',                 'cantidad': 524, 'precio': 5055.00,  'valor': 2648820},
            {'ticker': 'TGSU2', 'descripcion': 'Transportadora de Gas del Sur', 'cantidad': 83,  'precio': 8595.00,  'valor': 713385},
            {'ticker': 'YPFD',  'descripcion': 'YPF S.A.',                     'cantidad': 90,  'precio': 8450.00,  'valor': 760500},
        ],
        'Bonos': [
            {'ticker': 'AE38',  'descripcion': 'Bono Rep. Argentina USD Step Up 2038', 'cantidad': 137,  'precio': 1125.00, 'valor': 154125},
            {'ticker': 'AL29',  'descripcion': 'Bono Rep. Argentina USD 1% 2029',      'cantidad': 253,  'precio': 826.60,  'valor': 209130},
            {'ticker': 'AL30',  'descripcion': 'Bono Rep. Argentina USD Step Up 2030', 'cantidad': 1375, 'precio': 837.40,  'valor': 1151425},
            {'ticker': 'AL35',  'descripcion': 'Bono Rep. Argentina USD Step Up 2035', 'cantidad': 724,  'precio': 1079.40, 'valor': 781486},
            {'ticker': 'AL41',  'descripcion': 'Bono Rep. Argentina USD Step Up 2041', 'cantidad': 1137, 'precio': 1000.50, 'valor': 1137569},
            {'ticker': 'AO27',  'descripcion': 'Bono Tesoro Nacional 6% 29/10/27',     'cantidad': 6171, 'precio': 1589.00, 'valor': 9805719},
            {'ticker': 'AO28',  'descripcion': 'Bono Tesoro Nacional 6% 31/10/28',     'cantidad': 1124, 'precio': 1421.20, 'valor': 1597429},
            {'ticker': 'BPOD7', 'descripcion': 'Bopreal S.1-D Vto 31/10/27',           'cantidad': 1149, 'precio': 1615.70, 'valor': 1856439},
            {'ticker': 'GD30',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2030',     'cantidad': 21,   'precio': 874.00,  'valor': 18354},
            # RESUELTO: la baja de 9.783 (registrada el 12/08) a 8.737 nominales NO fue una
            # venta. El 12/08 se había cargado mal la compra del 12/08 (append aproximado,
            # sin el detalle real de Balanz) y quedó de más 1.046 nominales que nunca
            # existieron. Con el Excel de operaciones que pasó Eduardo (lotes iniciales
            # 31/07 vs. finales 04/09) se confirmó que la cantidad real siempre fue 8.737 y
            # se corrigió data/operaciones_balanz.json en consecuencia.
            {'ticker': 'GD35',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2035',     'cantidad': 9557, 'precio': 1154.50, 'valor': 11033557},
        ],
        'CEDEARs': [
            {'ticker': 'AAPL', 'descripcion': 'Apple Inc.',                  'cantidad': 36,  'precio': 27560.00, 'valor': 992160},
            {'ticker': 'AMD',  'descripcion': 'Advanced Micro Devices',      'cantidad': 19,  'precio': 102150.00,'valor': 1940850},
            {'ticker': 'AMZN', 'descripcion': 'Amazon.com Inc.',             'cantidad': 286, 'precio': 2827.50,  'valor': 808665},
            {'ticker': 'BRKB', 'descripcion': 'Berkshire Hathaway Inc. B',   'cantidad': 6,   'precio': 37280.00, 'valor': 223680},
            {'ticker': 'DISN', 'descripcion': 'The Walt Disney Company',     'cantidad': 70,  'precio': 14270.00, 'valor': 998900},
            {'ticker': 'FDX',  'descripcion': 'FedEx Corporation',           'cantidad': 12,  'precio': 46100.00, 'valor': 553200},
            {'ticker': 'GOOGL','descripcion': 'Alphabet Inc. Class A',       'cantidad': 32,  'precio': 9645.00,  'valor': 308640},
            {'ticker': 'KO',   'descripcion': 'Coca-Cola Company',           'cantidad': 38,  'precio': 28580.00, 'valor': 1086040},
            {'ticker': 'MELI', 'descripcion': 'MercadoLibre Inc.',           'cantidad': 33,  'precio': 23680.00, 'valor': 781440},
            {'ticker': 'META', 'descripcion': 'Meta Platforms Inc.',         'cantidad': 26,  'precio': 50675.00, 'valor': 1317550},
            {'ticker': 'MSFT', 'descripcion': 'Microsoft Corporation',       'cantidad': 11,  'precio': 27860.00, 'valor': 306460},
            {'ticker': 'NVDA', 'descripcion': 'NVIDIA Corporation',          'cantidad': 80,  'precio': 15220.00, 'valor': 1217600},
            # OJO: cantidad subió de 34 a 35 en el resumen de Balanz del 25/09 sin que
            # Eduardo me haya contado una compra nueva de PFE. Puede ser un dividendo
            # reinvertido (DRIP) o una compra que no me pasó. Se dejó la cantidad real
            # (35) para que la posición sea correcta, pero la fila de ledger que se
            # agregó para la unidad nueva usa fecha/precio de HOY como aproximación —
            # falta confirmar con Eduardo la fecha y el precio real de esa operación.
            {'ticker': 'PFE',  'descripcion': 'Pfizer Inc.',                 'cantidad': 35,  'precio': 11470.00, 'valor': 401450},
            {'ticker': 'QQQ',  'descripcion': 'Invesco QQQ Trust (ETF)',     'cantidad': 33,  'precio': 60450.00, 'valor': 1994850},
            # RESUELTO: el resumen del 25/09 confirma 35 nominales (30 + 5), o sea que
            # la orden ambigua del 10/09 (Precio/Cantidad Operada en -1 en el Excel) SÍ
            # se ejecutó. Se cargó la fila de compra en el ledger con fecha 2026-09-10
            # (misma tanda que el resto de esa orden) al precio estimado que ya se
            # había anotado en su momento (~$17.950) — falta confirmar el precio real.
            {'ticker': 'SMH',  'descripcion': 'VanEck Semiconductor ETF',    'cantidad': 35,  'precio': 19720.00, 'valor': 690200},
            {'ticker': 'SPY',  'descripcion': 'SPDR S&P 500 ETF',            'cantidad': 262, 'precio': 20860.00, 'valor': 5465320},
            {'ticker': 'TSLA', 'descripcion': 'Tesla Inc.',                  'cantidad': 29,  'precio': 40320.00, 'valor': 1169280},
            {'ticker': 'XLE',  'descripcion': 'Energy Select Sector SPDR',   'cantidad': 14,  'precio': 50050.00, 'valor': 700700},
        ],
        'Corporativos': [
            {'ticker': 'DNC3O', 'descripcion': 'ON Edenor Cl.3 Vto 22/11/26',           'cantidad': 336,  'precio': 1618.50, 'valor': 543816},
            {'ticker': 'LMS7O', 'descripcion': 'ON Aluar S.7 Vto 12/10/28',             'cantidad': 350,  'precio': 1200.50, 'valor': 420175},
            {'ticker': 'TTCDO', 'descripcion': 'ON Tecpetrol 7.625% Vto 11/2030 USD',   'cantidad': 1000, 'precio': 1684.00, 'valor': 1684000},
            {'ticker': 'VSCVO', 'descripcion': 'ON Vista Energy 8.5% Vto 06/2033 USD',  'cantidad': 2000, 'precio': 1740.20, 'valor': 3480400},
            {'ticker': 'YMCJO', 'descripcion': 'ON YPF REGS 1.5% Vto 30/09/2033',      'cantidad': 2537, 'precio': 1624.90, 'valor': 4122371},
        ],
        'Fondos': [
            {'ticker': 'BRTA',     'descripcion': 'Renta Mixta Clase A (Balanz)',               'cantidad': 1227.43,    'precio': 744.00,      'valor': 913207,  'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'LECAPSA',  'descripcion': 'Lecaps Clase A (Balanz)',                    'cantidad': 2387292.45, 'precio': 2.14,        'valor': 5113017, 'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'BAHUSDA',  'descripcion': 'Corporativo Clase A (Balanz)',               'cantidad': 4445.58,    'precio': 1.43,        'valor': 6337,    'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'FIMAPREM', 'descripcion': 'Fima Premium Clase A (Galicia)',           'cantidad': 72282.38,   'precio': 85.168189,   'valor': 6156159, 'fuente': 'Galicia', 'moneda': 'ARS'},
            {'ticker': 'FIMARPLUS','descripcion': 'Fima Renta Plus Clase A (Galicia)',         'cantidad': 1068.94,    'precio': 959.952521,  'valor': 1026132, 'fuente': 'Galicia', 'moneda': 'ARS'},
            {'ticker': 'FIMARFDA', 'descripcion': 'Fima Renta Fija Dolares Clase A (Galicia)','cantidad': 914.53,     'precio': 1734.49,     'valor': 1586243, 'fuente': 'Galicia', 'moneda': 'USD', 'precio_usd': 1.119731, 'valor_usd': 1024.03},
        ],
        'Letras': [],
    }
}

# ── Precios en tiempo real ────────────────────────────────────────────────────
_cache = {}
CACHE_TTL = 300  # 5 minutos

def _cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key]['ts'] < CACHE_TTL:
        return _cache[key]['data']
    try:
        data = fn()
        _cache[key] = {'data': data, 'ts': now}
        return data
    except Exception:
        return _cache.get(key, {}).get('data')

def fetch_dolares():
    def _fetch():
        r = req.get('https://dolarapi.com/v1/dolares', timeout=8)
        r.raise_for_status()
        return r.json()
    return _cached('dolares', _fetch) or []

def fetch_precio_rava(ticker):
    """Precio de un instrumento listado en BYMA via Rava."""
    def _fetch():
        r = req.get(
            f'https://www.rava.com/empresas/cotizacion.php?e={ticker}&t=json',
            timeout=5,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        r.raise_for_status()
        return r.json()
    return _cached(f'rava_{ticker}', _fetch)

def fetch_precios_cartera(portfolio):
    """Devuelve dict {ticker: {ultimo, variacion, fuente}} para todos los instrumentos, en paralelo."""
    todos = []
    for items in portfolio['instrumentos'].values():
        todos.extend(items)
    tickers = [item['ticker'] for item in todos]

    precios = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_precio_rava, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                data = future.result()
            except Exception:
                continue
            if data:
                ultimo = data.get('Ultimo') or data.get('ultimo') or data.get('UltimoPrecio')
                var    = data.get('Variacion') or data.get('variacion') or 0
                if ultimo:
                    precios[ticker] = {
                        'ultimo':    float(ultimo),
                        'variacion': float(var),
                        'fuente':    'Rava/BYMA',
                    }
    return precios

COLORES = {
    'Acciones':     '#3b82f6',
    'Bonos':        '#10b981',
    'CEDEARs':      '#f59e0b',
    'Corporativos': '#8b5cf6',
    'Fondos':       '#ec4899',
    'Letras':       '#06b6d4',
}

def get_portfolio():
    """Devuelve el snapshot más reciente de DB, o el inicial del PDF."""
    try:
        with get_db() as conn:
            row = conn.execute(
                'SELECT * FROM snapshots ORDER BY fecha DESC, id DESC LIMIT 1'
            ).fetchone()
        if row:
            data = json.loads(row['data_json'])
            data['total_ars'] = row['total_ars']
            data['tc_mep']    = row['tc_mep']
            data['tc_usd']    = row['tc_usd']
            data['fecha']     = row['fecha']
            return data
    except Exception:
        pass
    return PORTFOLIO_INICIAL

# ── Notificaciones ───────────────────────────────────────────────────────────
VENCIMIENTOS_CONOCIDOS = {
    'S31L6': _date(2026, 7, 31),
    'S29L6': _date(2026, 6, 29),
    'DNC3O': _date(2026, 11, 22),
}

def get_notificaciones(portfolio):
    hoy = _date.today()
    notifs = []

    for tipo, items in portfolio['instrumentos'].items():
        for item in items:
            t = item['ticker']
            if t not in VENCIMIENTOS_CONOCIDOS:
                continue
            fecha_vto = VENCIMIENTOS_CONOCIDOS[t]
            dias = (fecha_vto - hoy).days
            if dias > 90 or dias < -15:
                continue
            if dias < 0:
                nivel, icono = 'danger', '🔴'
                msg = f'venció hace {-dias} día{"s" if -dias != 1 else ""}'
            elif dias == 0:
                nivel, icono = 'danger', '🔴'
                msg = 'vence HOY'
            elif dias <= 7:
                nivel, icono = 'danger', '⚠️'
                msg = f'vence en {dias} día{"s" if dias != 1 else ""}'
            elif dias <= 30:
                nivel, icono = 'warning', '⏰'
                msg = f'vence en {dias} días ({fecha_vto.strftime("%d/%m")})'
            else:
                nivel, icono = 'info', '📋'
                msg = f'vence el {fecha_vto.strftime("%d/%m/%Y")}'
            notifs.append({
                'nivel': nivel,
                'icono': icono,
                'titulo': f'{t} – {msg}',
                'cuerpo': f'${item["valor"]:,.0f} ARS disponibles para reinvertir',
            })

    # Resumen estratégico
    fimarfda_usd = 0
    for item in portfolio['instrumentos'].get('Fondos', []):
        if item['ticker'] == 'FIMARFDA':
            fimarfda_usd = item.get('valor_usd', 0)
    if fimarfda_usd:
        notifs.append({
            'nivel': 'info',
            'icono': '🏠',
            'titulo': 'Bucket anticipo depto',
            'cuerpo': f'FIMARFDA: USD {fimarfda_usd:,.2f} — capital líquido en dólares',
        })

    gd35_u = next((i['cantidad'] for i in portfolio['instrumentos'].get('Bonos', []) if i['ticker'] == 'GD35'), 0)
    if gd35_u:
        notifs.append({
            'nivel': 'neutral',
            'icono': '📈',
            'titulo': 'Bucket largo plazo',
            'cuerpo': f'GD35: {int(gd35_u):,} nominales · ONs: TTCDO + VSCVO + YMCJO',
        })

    notifs.append({
        'nivel': 'neutral',
        'icono': '💱',
        'titulo': f'TC MEP al {portfolio["fecha"]}',
        'cuerpo': f'${portfolio["tc_mep"]:,.2f} ARS/USD',
    })

    return notifs

@app.context_processor
def inject_notificaciones():
    p = get_portfolio()
    notifs = get_notificaciones(p)
    urgentes = sum(1 for n in notifs if n['nivel'] in ('danger', 'warning'))
    return {'notificaciones': notifs, 'notif_count': urgentes}

# ── Rutas ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'usuario' in session else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        with get_db() as conn:
            user = conn.execute(
                'SELECT * FROM usuarios WHERE username=?', (username,)
            ).fetchone()
        if user and user['password'] == password:
            session['usuario'] = username
            session['nombre']  = user['nombre']
            return redirect(url_for('dashboard'))
        flash('Usuario o contraseña incorrectos.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    p = get_portfolio()
    totales_tipo = {
        tipo: sum(item['valor'] for item in items)
        for tipo, items in p['instrumentos'].items()
    }
    total = p['total_ars']
    pcts  = {tipo: round(v / total * 100, 1) for tipo, v in totales_tipo.items()}
    return render_template('dashboard.html',
        portfolio=p,
        totales_tipo=totales_tipo,
        pcts=pcts,
        colores=COLORES,
    )

@app.route('/cartera/<tipo>')
@login_required
def cartera_tipo(tipo):
    p = get_portfolio()
    items = p['instrumentos'].get(tipo, [])
    total_tipo = sum(i['valor'] for i in items)
    return render_template('cartera_tipo.html',
        tipo=tipo,
        items=items,
        total_tipo=total_tipo,
        portfolio=p,
        color=COLORES.get(tipo, '#3b82f6'),
    )

@app.route('/cotizaciones')
@login_required
def cotizaciones():
    dolares = fetch_dolares()
    # Ordenar por nombre conocido
    orden = ['oficial', 'blue', 'bolsa', 'contadoconliqui', 'tarjeta', 'mayorista', 'cripto']
    nombres_es = {
        'oficial':          'Oficial',
        'blue':             'Blue',
        'bolsa':            'MEP / Bolsa',
        'contadoconliqui':  'Contado con Liqui',
        'tarjeta':          'Tarjeta / Turista',
        'mayorista':        'Mayorista',
        'cripto':           'Cripto',
    }
    dolares_sorted = sorted(
        dolares,
        key=lambda d: orden.index(d.get('casa','').lower()) if d.get('casa','').lower() in orden else 99
    )
    for d in dolares_sorted:
        casa = d.get('casa', '').lower()
        d['nombre_es'] = nombres_es.get(casa, d.get('nombre', casa.title()))

    p = get_portfolio()
    precios      = fetch_precios_cartera(p)
    precios_ini  = get_precios_iniciales()

    # Construir tabla de instrumentos con precio live
    filas = []
    for tipo, items in p['instrumentos'].items():
        for item in items:
            ticker        = item['ticker']
            pr            = precios.get(ticker)
            pi            = precios_ini.get(ticker)
            precio_actual = pr['ultimo']   if pr else None
            variacion     = pr['variacion'] if pr else None
            precio_ini    = pi['precio']   if pi else item['precio']
            valor_actual  = precio_actual * item['cantidad'] if precio_actual else None
            valor_orig    = item['valor']
            valor_ini     = precio_ini * item['cantidad']
            diff_pct_live = ((valor_actual - valor_ini) / valor_ini * 100) if valor_actual else None
            filas.append({
                'tipo':          tipo,
                'ticker':        ticker,
                'descripcion':   item['descripcion'],
                'cantidad':      item['cantidad'],
                'precio_ini':    precio_ini,
                'precio_actual': precio_actual,
                'variacion':     variacion,
                'valor_ini':     valor_ini,
                'valor_actual':  valor_actual,
                'diff_pct':      diff_pct_live,
                'fuente_live':   pr['fuente'] if pr else None,
                'broker':        item.get('fuente', 'Balanz'),
                'moneda':        item.get('moneda', 'ARS'),
                'valor_usd':     item.get('valor_usd'),
                'pi_fecha':      pi['fecha']  if pi else None,
                'pi_notas':      pi['notas']  if pi else None,
            })

    total_ini    = sum(f['valor_ini']    for f in filas)
    total_actual = sum(f['valor_actual'] for f in filas if f['valor_actual'])
    con_precio   = sum(1 for f in filas if f['precio_actual'])

    return render_template('cotizaciones.html',
        dolares=dolares_sorted,
        filas=filas,
        total_ini=total_ini,
        total_actual=total_actual,
        con_precio=con_precio,
        total_filas=len(filas),
        portfolio=p,
        colores=COLORES,
    )

@app.route('/rendimientos')
@login_required
def rendimientos():
    p = get_portfolio()
    precios_live = fetch_precios_cartera(p)
    precios_ini  = get_precios_iniciales()
    operaciones  = get_operaciones()
    tc_mep       = p['tc_mep']
    hoy          = _date.today()

    filas = []
    for tipo, items in p['instrumentos'].items():
        for item in items:
            ticker = item['ticker']
            ops_ticker = operaciones.get(ticker, [])

            pr = precios_live.get(ticker)
            if pr:
                precio_actual = pr['ultimo']
                precio_es_live = True
            else:
                # Sin cotización viva (típico de Fondos): usamos el último precio
                # conocido (precio_iniciales o el que figura en la posición) como
                # aproximación, dejándolo marcado como "estimado" en la UI.
                pi = precios_ini.get(ticker)
                precio_actual = pi['precio'] if pi else item['precio']
                precio_es_live = False

            cantidad_actual = item['cantidad']
            valor_actual_ars = precio_actual * cantidad_actual
            valor_actual_usd = item.get('valor_usd') if item.get('moneda') == 'USD' else (
                valor_actual_ars / tc_mep if tc_mep else None
            )

            if not ops_ticker:
                # Todavía no tenemos historial de compra real para este ticker
                # (ej. algo de Galicia). Mostramos la fila igual, sin TIR.
                filas.append({
                    'tipo': tipo, 'ticker': ticker, 'descripcion': item['descripcion'],
                    'bucket': finanzas.get_bucket(ticker),
                    'cantidad': cantidad_actual,
                    'invertido_ars': None, 'rentas_cobradas_ars': None,
                    'valor_actual_ars': valor_actual_ars,
                    'invertido_usd': None, 'rentas_cobradas_usd': None,
                    'valor_actual_usd': valor_actual_usd,
                    'tir_ars': None, 'tir_usd': None,
                    'precio_es_live': precio_es_live,
                    'sin_historial': True,
                    'ops': [],
                })
                continue

            r = finanzas.resumen_ticker(ticker, ops_ticker, valor_actual_ars, valor_actual_usd, hoy)
            r['tipo'] = tipo
            r['descripcion'] = item['descripcion']
            r['cantidad'] = cantidad_actual
            r['precio_es_live'] = precio_es_live
            r['sin_historial'] = False
            r['ops'] = [
                {
                    'fecha':    op['fecha'],
                    'tipo_mov': op['tipo_mov'],
                    'cantidad': op.get('cantidad'),
                    'precio':   op.get('precio'),
                    'gastos':   op.get('gastos'),
                    'moneda':   op.get('moneda'),
                    'mep':      op.get('mep'),
                    'monto_ars':op.get('monto_ars'),
                    'abierto':  bool(op.get('abierto')),
                }
                for op in ops_ticker
            ]
            filas.append(r)

    # Agregados por tipo y por bucket (solo con lo que tiene TIR calculable)
    def agregar(rows, key):
        grupos = {}
        for f in rows:
            if f['tir_usd'] is None or f.get('sin_historial'):
                continue
            g = grupos.setdefault(f[key], {'invertido_ars': 0, 'valor_actual_ars': 0, 'tirs_usd': []})
            g['invertido_ars'] += f.get('invertido_ars') or 0
            g['valor_actual_ars'] += f.get('valor_actual_ars') or 0
            g['tirs_usd'].append(f['tir_usd'])
        for g in grupos.values():
            g['tir_usd_prom'] = sum(g['tirs_usd']) / len(g['tirs_usd']) if g['tirs_usd'] else None
        return grupos

    por_tipo   = agregar(filas, 'tipo')
    por_bucket = agregar(filas, 'bucket')

    con_tir = sum(1 for f in filas if f['tir_usd'] is not None)

    return render_template('rendimientos.html',
        filas=sorted(filas, key=lambda f: (f['tir_usd'] is None, f['tir_usd'] if f['tir_usd'] is not None else 0)),
        por_tipo=por_tipo,
        por_bucket=por_bucket,
        con_tir=con_tir,
        total_filas=len(filas),
        portfolio=p,
        colores=COLORES,
    )

@app.route('/precios-iniciales', methods=['GET', 'POST'])
@login_required
def precios_iniciales():
    p = get_portfolio()
    if request.method == 'POST':
        ticker  = request.form.get('ticker', '').strip().upper()
        precio  = request.form.get('precio', '').strip()
        fecha   = request.form.get('fecha', '').strip()
        notas   = request.form.get('notas', '').strip()
        try:
            precio_f = float(precio.replace(',', '.'))
            with get_db() as conn:
                conn.execute(
                    'INSERT INTO precios_iniciales VALUES (?,?,?,?) '
                    'ON CONFLICT(ticker) DO UPDATE SET precio=excluded.precio, fecha=excluded.fecha, notas=excluded.notas',
                    (ticker, precio_f, fecha or None, notas or None)
                )
                conn.commit()
            flash(f'Precio inicial de {ticker} actualizado.', 'success')
        except Exception as e:
            flash(f'Error: {e}', 'error')
        return redirect(url_for('precios_iniciales'))

    precios_ini = get_precios_iniciales()
    # Armar lista con todos los instrumentos del portfolio
    instrumentos = []
    for tipo, items in p['instrumentos'].items():
        for item in items:
            pi = precios_ini.get(item['ticker'])
            instrumentos.append({
                'tipo':        tipo,
                'ticker':      item['ticker'],
                'descripcion': item['descripcion'],
                'cantidad':    item['cantidad'],
                'precio_ini':  pi['precio'] if pi else item['precio'],
                'fecha':       pi['fecha']  if pi else p['fecha'],
                'notas':       pi['notas']  if pi else '',
                'color':       COLORES.get(tipo, '#64748b'),
            })

    return render_template('precios_iniciales.html',
        instrumentos=instrumentos,
        colores=COLORES,
    )

@app.route('/cambiar-password', methods=['GET', 'POST'])
@login_required
def cambiar_password():
    error = None
    if request.method == 'POST':
        actual   = request.form.get('actual', '').strip()
        nueva    = request.form.get('nueva', '').strip()
        confirma = request.form.get('confirma', '').strip()
        username = session['usuario']
        with get_db() as conn:
            user = conn.execute('SELECT * FROM usuarios WHERE username=?', (username,)).fetchone()
        if not user or user['password'] != actual:
            error = 'La contraseña actual es incorrecta.'
        elif len(nueva) < 6:
            error = 'La nueva contraseña debe tener al menos 6 caracteres.'
        elif nueva != confirma:
            error = 'Las contraseñas no coinciden.'
        else:
            with get_db() as conn:
                conn.execute('UPDATE usuarios SET password=?, must_change=0 WHERE username=?', (nueva, username))
                conn.commit()
            flash('Contraseña actualizada.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('cambiar_password.html', error=error)


with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    app.run(host='0.0.0.0', port=port, debug=False)
