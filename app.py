import os, sqlite3, json, time
from flask import Flask, render_template, request, redirect, url_for, session, flash
from functools import wraps
from datetime import datetime, date as _date
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                        (item['ticker'], item['precio'], PORTFOLIO_INICIAL['fecha'], 'Precio PDF 03/07/2026')
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

# ── Datos cartera (posición 03/07/2026) ──────────────────────────────────────
PORTFOLIO_INICIAL = {
    'fecha': '31/07/2026',
    'total_ars': 63193000,
    'tc_mep': 1517.87,
    'tc_usd': 1583.98,
    'monedas': {
        'Pesos': 31721.73,
        'Dólares': 21.48,
        'USD Cable': 2.42,
    },
    'disponibilidad': {
        'Balanz Pesos':  {'ars': 31721.73,     'usd': None},
        'Balanz USD':    {'ars': None,          'usd': 21.48},
        'Balanz Cable':  {'ars': None,          'usd': 2.42},
        'Galicia Pesos': {'ars': 701988.97,     'usd': None},
        'Galicia USD':   {'ars': None,          'usd': 66.62},
        'MercadoPago':   {'ars': 1495471.34,    'usd': None},
    },
    'instrumentos': {
        'Acciones': [
            {'ticker': 'BBAR',  'descripcion': 'Banco Frances Escriturales',    'cantidad': 58,  'precio': 10050.00, 'valor': 582900},
            {'ticker': 'BMA',   'descripcion': 'Banco Macro S.A.',              'cantidad': 45,  'precio': 14550.00, 'valor': 654750},
            {'ticker': 'GGAL',  'descripcion': 'Grupo Financiero Galicia',      'cantidad': 360, 'precio': 7945.00,  'valor': 2860200},
            {'ticker': 'PAMP',  'descripcion': 'Pampa Energia',                 'cantidad': 171, 'precio': 5630.00,  'valor': 962730},
            {'ticker': 'TGSU2', 'descripcion': 'Transportadora de Gas del Sur', 'cantidad': 83,  'precio': 9995.00,  'valor': 829585},
            {'ticker': 'YPFD',  'descripcion': 'YPF S.A.',                     'cantidad': 9,   'precio': 82475.00, 'valor': 742275},
        ],
        'Bonos': [
            {'ticker': 'AE38',  'descripcion': 'Bono Rep. Argentina USD Step Up 2038', 'cantidad': 137,  'precio': 1262.60, 'valor': 172976},
            {'ticker': 'AL29',  'descripcion': 'Bono Rep. Argentina USD 1% 2029',      'cantidad': 253,  'precio': 837.00,  'valor': 211761},
            {'ticker': 'AL30',  'descripcion': 'Bono Rep. Argentina USD Step Up 2030', 'cantidad': 1375, 'precio': 862.10,  'valor': 1185388},
            {'ticker': 'AL35',  'descripcion': 'Bono Rep. Argentina USD Step Up 2035', 'cantidad': 724,  'precio': 1221.80, 'valor': 884583},
            {'ticker': 'AL41',  'descripcion': 'Bono Rep. Argentina USD Step Up 2041', 'cantidad': 1137, 'precio': 1152.80, 'valor': 1310734},
            {'ticker': 'AO27',  'descripcion': 'Bono Tesoro Nacional 6% 29/10/27',     'cantidad': 2868, 'precio': 1551.30, 'valor': 4447127},
            {'ticker': 'AO28',  'descripcion': 'Bono Tesoro Nacional 6% 31/10/28',     'cantidad': 1124, 'precio': 1492.00, 'valor': 1677008},
            {'ticker': 'BPOD7', 'descripcion': 'Bopreal S.1-D Vto 31/10/27',           'cantidad': 632,  'precio': 1566.60, 'valor': 990091},
            {'ticker': 'GD30',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2030',     'cantidad': 21,   'precio': 884.00,  'valor': 18564},
            {'ticker': 'GD35',  'descripcion': 'Bonos Rep. Arg. USD Step Up 2035',     'cantidad': 6108, 'precio': 1244.00, 'valor': 7598352},
        ],
        'CEDEARs': [
            {'ticker': 'AAPL', 'descripcion': 'Apple Inc.',                  'cantidad': 31,  'precio': 25480.00, 'valor': 789880},
            {'ticker': 'AMD',  'descripcion': 'Advanced Micro Devices',      'cantidad': 17,  'precio': 85600.00, 'valor': 1455200},
            {'ticker': 'AMZN', 'descripcion': 'Amazon.com Inc.',             'cantidad': 243, 'precio': 2577.50,  'valor': 626333},
            {'ticker': 'DISN', 'descripcion': 'The Walt Disney Company',     'cantidad': 60,  'precio': 12300.00, 'valor': 738000},
            {'ticker': 'FDX',  'descripcion': 'FedEx Corporation',           'cantidad': 11,  'precio': 49900.00, 'valor': 548900},
            {'ticker': 'KO',   'descripcion': 'Coca-Cola Company',           'cantidad': 33,  'precio': 25760.00, 'valor': 850080},
            {'ticker': 'MELI', 'descripcion': 'MercadoLibre Inc.',           'cantidad': 28,  'precio': 23760.00, 'valor': 665280},
            {'ticker': 'META', 'descripcion': 'Meta Platforms Inc.',         'cantidad': 23,  'precio': 40140.00, 'valor': 923220},
            {'ticker': 'NVDA', 'descripcion': 'NVIDIA Corporation',          'cantidad': 80,  'precio': 13720.00, 'valor': 1097600},
            {'ticker': 'PFE',  'descripcion': 'Pfizer Inc.',                 'cantidad': 32,  'precio': 9920.00,  'valor': 317440},
            {'ticker': 'QQQ',  'descripcion': 'Invesco QQQ Trust (ETF)',     'cantidad': 14,  'precio': 54850.00, 'valor': 767900},
            {'ticker': 'SMH',  'descripcion': 'VanEck Semiconductor ETF',    'cantidad': 30,  'precio': 18290.00, 'valor': 548700},
            {'ticker': 'SPY',  'descripcion': 'SPDR S&P 500 ETF',            'cantidad': 189, 'precio': 19500.00, 'valor': 3685500},
            {'ticker': 'TSLA', 'descripcion': 'Tesla Inc.',                  'cantidad': 25,  'precio': 33840.00, 'valor': 846000},
            {'ticker': 'XLE',  'descripcion': 'Energy Select Sector SPDR',   'cantidad': 12,  'precio': 47080.00, 'valor': 564960},
        ],
        'Corporativos': [
            {'ticker': 'DNC3O', 'descripcion': 'ON Edenor Cl.3 Vto 22/11/26',           'cantidad': 336,  'precio': 1567.00, 'valor': 526512},
            {'ticker': 'LMS7O', 'descripcion': 'ON Aluar S.7 Vto 12/10/28',             'cantidad': 350,  'precio': 1184.80, 'valor': 414680},
            {'ticker': 'TTCDO', 'descripcion': 'ON Tecpetrol 7.625% Vto 11/2030 USD',   'cantidad': 1000, 'precio': 1665.40, 'valor': 1665400},
            {'ticker': 'VSCVO', 'descripcion': 'ON Vista Energy 8.5% Vto 06/2033 USD',  'cantidad': 1000, 'precio': 1705.50, 'valor': 1705500},
            {'ticker': 'YMCJO', 'descripcion': 'ON YPF REGS 1.5% Vto 30/09/2033',      'cantidad': 2131, 'precio': 1654.00, 'valor': 3524674},
        ],
        'Fondos': [
            {'ticker': 'BRTA',     'descripcion': 'Renta Mixta Clase A (Balanz)',               'cantidad': 1227.43,    'precio': 748.10,      'valor': 918234,  'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'LECAPSA',  'descripcion': 'Lecaps Clase A (Balanz)',                    'cantidad': 2387292.45, 'precio': 2.06,        'valor': 4927539, 'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'BAHUSDA',  'descripcion': 'Corporativo Clase A (Balanz)',               'cantidad': 4445.58,    'precio': 1.43,        'valor': 6364,    'fuente': 'Balanz', 'moneda': 'ARS'},
            {'ticker': 'FIMAPREM', 'descripcion': 'Fima Premium Clase A (Galicia)',             'cantidad': 85006.35,   'precio': 82.670567,   'valor': 7027523, 'fuente': 'Galicia','moneda': 'ARS'},
            {'ticker': 'FIMARFDA', 'descripcion': 'Fima Renta Fija Dolares Clase A (Galicia)', 'cantidad': 914.53,     'precio': 1702.91,     'valor': 1557155, 'fuente': 'Galicia','moneda': 'USD', 'precio_usd': 1.121462, 'valor_usd': 1025.61},
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
