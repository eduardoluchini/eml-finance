import os, sqlite3, json, time
from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
from datetime import datetime
import requests as req

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
                        (item['ticker'], item['precio'], PORTFOLIO_INICIAL['fecha'], 'Precio PDF 26/06/2026')
                    )
        conn.commit()

def get_precios_iniciales():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM precios_iniciales').fetchall()
    return {r['ticker']: {'precio': r['precio'], 'fecha': r['fecha'], 'notas': r['notas']} for r in rows}

# ── Auth ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'usuario' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Datos cartera (posición 26/06/2026) ──────────────────────────────────────
PORTFOLIO_INICIAL = {
    'fecha': '26/06/2026',
    'total_ars': 41568639,
    'tc_mep': 1501.01,
    'tc_usd': 1543.38,
    'monedas': {
        'Pesos': 106127.31,
        'Dólares': 198.18,
        'USD Cable': 1.94,
    },
    'instrumentos': {
        'Acciones': [
            {'ticker': 'BMA',  'descripcion': 'Banco Macro S.A.',     'cantidad': 45,   'precio': 14110.00,  'valor': 634950},
            {'ticker': 'PAMP', 'descripcion': 'Pampa Energía',        'cantidad': 171,  'precio': 4972.50,   'valor': 850298},
            {'ticker': 'YPFD', 'descripcion': 'YPF S.A.',             'cantidad': 9,    'precio': 70050.00,  'valor': 630450},
        ],
        'Bonos': [
            {'ticker': 'AE38',  'descripcion': 'Bono Rep. Argentina USD Step Up 2038', 'cantidad': 137,   'precio': 1264.30,  'valor': 173209},
            {'ticker': 'AL29',  'descripcion': 'Bono Rep. Argentina USD 1% 2029',      'cantidad': 253,   'precio': 973.60,   'valor': 246321},
            {'ticker': 'AL30',  'descripcion': 'Bono Rep. Argentina USD Step Up 2030', 'cantidad': 1375,  'precio': 963.00,   'valor': 1324125},
            {'ticker': 'AL35',  'descripcion': 'Bono Rep. Argentina USD Step Up 2035', 'cantidad': 724,   'precio': 1228.00,  'valor': 889072},
            {'ticker': 'AL41',  'descripcion': 'Bono Rep. Argentina USD Step Up 2041', 'cantidad': 189,   'precio': 1150.00,  'valor': 217350},
            {'ticker': 'AO27',  'descripcion': 'Bono Tesoro Nacional 6% 29/10/27',     'cantidad': 518,   'precio': 1541.20,  'valor': 798342},
            {'ticker': 'AO28',  'descripcion': 'Bono Tesoro Nacional 6% 31/10/28',     'cantidad': 549,   'precio': 1469.90,  'valor': 806975},
            {'ticker': 'BPOD7', 'descripcion': 'Bopreal S.1-D Vto 31/10/27',          'cantidad': 632,   'precio': 1537.50,  'valor': 971700},
            {'ticker': 'GD30',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2030',     'cantidad': 21,    'precio': 987.70,   'valor': 20742},
            {'ticker': 'GD35',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2035',     'cantidad': 178,   'precio': 1267.00,  'valor': 225526},
        ],
        'CEDEARs': [
            {'ticker': 'AAPL', 'descripcion': 'Apple Inc.',                  'cantidad': 31,  'precio': 21880.00,  'valor': 678280},
            {'ticker': 'AMD',  'descripcion': 'Advanced Micro Devices',      'cantidad': 17,  'precio': 79550.00,  'valor': 1352350},
            {'ticker': 'AMZN', 'descripcion': 'Amazon.com Inc.',             'cantidad': 243, 'precio': 2478.00,   'valor': 602154},
            {'ticker': 'DISN', 'descripcion': 'The Walt Disney Company',     'cantidad': 60,  'precio': 12680.00,  'valor': 760800},
            {'ticker': 'FDX',  'descripcion': 'FedEx Corporation',           'cantidad': 11,  'precio': 48940.00,  'valor': 538340},
            {'ticker': 'MELI', 'descripcion': 'MercadoLibre Inc.',           'cantidad': 28,  'precio': 21510.00,  'valor': 602280},
            {'ticker': 'META', 'descripcion': 'Meta Platforms Inc.',         'cantidad': 23,  'precio': 35440.00,  'valor': 815120},
            {'ticker': 'NVDA', 'descripcion': 'NVIDIA Corporation',          'cantidad': 80,  'precio': 12450.00,  'valor': 996000},
            {'ticker': 'QQQ',  'descripcion': 'Invesco QQQ Trust (ETF)',     'cantidad': 14,  'precio': 54775.00,  'valor': 766850},
            {'ticker': 'SMH',  'descripcion': 'VanEck Semiconductor ETF',    'cantidad': 30,  'precio': 18880.00,  'valor': 566400},
            {'ticker': 'SPY',  'descripcion': 'SPDR S&P 500 ETF',           'cantidad': 189, 'precio': 18880.00,  'valor': 3568320},
            {'ticker': 'TSLA', 'descripcion': 'Tesla Inc.',                  'cantidad': 25,  'precio': 39180.00,  'valor': 979500},
            {'ticker': 'XLE',  'descripcion': 'Energy Select Sector SPDR',   'cantidad': 12,  'precio': 41500.00,  'valor': 498000},
        ],
        'Corporativos': [
            {'ticker': 'DNC3O', 'descripcion': 'ON Edenor Cl.3 Vto 22/11/26',      'cantidad': 336,   'precio': 1532.50,  'valor': 514920},
            {'ticker': 'LMS7O', 'descripcion': 'ON Aluar S.7 Vto 12/10/28',        'cantidad': 350,   'precio': 1312.00,  'valor': 459200},
            {'ticker': 'YMCJO', 'descripcion': 'ON YPF REGS 1.5% Vto 30/09/2033', 'cantidad': 1134,  'precio': 1595.60,  'valor': 1809410},
        ],
        'Fondos': [
            {'ticker': 'BRTA',     'descripcion': 'Renta Mixta Clase A (Balanz)',             'cantidad': 1227.43,      'precio': 728.16,       'valor': 893760,   'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'LECAPSA',  'descripcion': 'Lecaps Clase A (Balanz)',                  'cantidad': 2387292.45,   'precio': 2.03,         'valor': 4848612,  'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'BAHUSDA',  'descripcion': 'Corporativo Clase A (Balanz)',             'cantidad': 3529.11,      'precio': 1.42,         'valor': 4995,     'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'FIMAPREM', 'descripcion': 'Fima Premium Clase A (Galicia)',           'cantidad': 30889.58,     'precio': 81.818803,    'valor': 2527348,  'fuente': 'Galicia','moneda': 'ARS'},
            {'ticker': 'FIMARFDA', 'descripcion': 'Fima Renta Fija Dólares Clase A (Galicia)','cantidad': 914.53,      'precio': 1668.98,      'valor': 1527127,  'fuente': 'Galicia','moneda': 'USD', 'precio_usd': 1.112489, 'valor_usd': 1017.40},
        ],
        'Letras': [
            {'ticker': 'S31L6', 'descripcion': 'Letra Tesoro Nac. Capitalizable 31/07/26', 'cantidad': 495162, 'precio': 1.15, 'valor': 571318},
        ],
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
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        r.raise_for_status()
        return r.json()
    return _cached(f'rava_{ticker}', _fetch)

def fetch_precios_cartera(portfolio):
    """Devuelve dict {ticker: {ultimo, variacion, fuente}} para todos los instrumentos."""
    todos = []
    for items in portfolio['instrumentos'].values():
        todos.extend(items)

    precios = {}
    for item in todos:
        ticker = item['ticker']
        data = fetch_precio_rava(ticker)
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
