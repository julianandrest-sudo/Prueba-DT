import os, json, base64, sqlite3, urllib.request, urllib.error, csv, io, html, time, uuid, mimetypes, re, hmac, hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response, redirect, send_file
from pathlib import Path
from urllib.parse import urlsplit
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

app = Flask(__name__)
# Meta webhook payloads are small; the larger cap is for dashboard audio uploads.
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN','')
META_APP_SECRET = os.environ.get('META_APP_SECRET','').strip()
META_ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN','')
PHONE_NUMBER_ID = os.environ.get('PHONE_NUMBER_ID','1190081650863434')
ADMIN_PHONE = os.environ.get('ADMIN_PHONE','573012108712')
GRAPH_API_VERSION = os.environ.get('GRAPH_API_VERSION','v23.0')
DB_PATH = os.environ.get('DB_PATH','conversations.db')
MEDIA_DIR = Path(os.environ.get('MEDIA_DIR','media_store'))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get('DATABASE_URL','').strip()
DASHBOARD_USER = os.environ.get('DASHBOARD_USER','admin')
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD','')
APPS_SCRIPT_WEBHOOK_URL = os.environ.get('APPS_SCRIPT_WEBHOOK_URL','').strip()
APPS_SCRIPT_SYNC_TOKEN = os.environ.get('APPS_SCRIPT_SYNC_TOKEN','').strip()
# Keep inbound admin callers stable while the outbound Apps Script token rotates.
MARKETING_SYNC_TOKEN = os.environ.get('MARKETING_SYNC_TOKEN','').strip()
conversations = {}
LOCAL_TZ = ZoneInfo('America/Bogota')

def format_dt(value):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        dt=dt.astimezone(LOCAL_TZ)
        suffix='a. m.' if dt.hour < 12 else 'p. m.'
        hour=dt.hour % 12 or 12
        return f'{dt.day:02d}/{dt.month:02d}/{dt.year} · {hour:02d}:{dt.minute:02d}:{dt.second:02d} {suffix}'
    except Exception:
        return str(value)

FOLLOW_UP_DELAYS = {
    'Urgente': timedelta(days=0),
    'Alta': timedelta(hours=2),
    'Media': timedelta(days=1),
    'Baja': timedelta(days=3),
}
TERMINAL_PROSPECT_STATUSES = {'Ganado', 'Perdido'}


def calculate_follow_up_at(priority, reference=None):
    """Return the UTC ISO timestamp for the default follow-up deadline."""
    reference = reference or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delay = FOLLOW_UP_DELAYS.get(priority, FOLLOW_UP_DELAYS['Media'])
    return (reference + delay).astimezone(timezone.utc).isoformat()


def follow_up_state(value, status='Nuevo', now=None):
    """Classify a follow-up for display; this never sends a customer message."""
    if not value or status in TERMINAL_PROSPECT_STATUSES:
        return 'Sin seguimiento pendiente'
    try:
        due = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if due <= current:
            return 'Vencido'
        return 'Próximo'
    except (TypeError, ValueError):
        return 'Fecha inválida'


def is_follow_up_pending(row, now=None):
    follow_up_at = row.get('follow_up_at') if hasattr(row, 'get') else row['follow_up_at']
    status = row.get('status') if hasattr(row, 'get') else row['status']
    return bool(follow_up_at) and status not in TERMINAL_PROSPECT_STATUSES


def _backfill_follow_ups(connection):
    """Populate deadlines for legacy rows without changing existing data."""
    rows = connection.execute(
        'SELECT id,priority,created_at FROM prospects WHERE follow_up_at IS NULL'
    ).fetchall()
    for row in rows:
        created = row['created_at']
        try:
            reference = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            reference = datetime.now(timezone.utc)
        connection.execute(
            'UPDATE prospects SET follow_up_at=? WHERE id=?',
            (calculate_follow_up_at(row['priority'], reference), row['id'])
        )

class PGConnection:
    def __init__(self, url):
        self.conn=psycopg2.connect(url, connect_timeout=10)
        self.cur=self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def execute(self, query, params=()):
        query=query.replace('?', '%s')
        if params:
            self.cur.execute(query, params)
        else:
            # Passing an empty tuple makes psycopg2 treat literal % in LIKE patterns as placeholders.
            self.cur.execute(query)
        return self.cur
    def commit(self): self.conn.commit()
    def close(self): self.cur.close(); self.conn.close()
WELCOME = ('¡Hola! Soy el asistente de DT Grúas y Montacargas 🚜🏗️\n\n'
           '¿En qué podemos ayudarte?\n'
           '1️⃣ Alquiler de montacargas\n'
           '2️⃣ Alquiler de grúa tipo planchón\n'
           '3️⃣ Mantenimiento o reparación\n'
           '4️⃣ Venta de equipos o repuestos\n'
           '5️⃣ Solicitar visita técnica\n\n'
           'En cualquier momento escribe MENU para volver al inicio.')

def db():
    if DATABASE_URL and psycopg2:
        c=PGConnection(DATABASE_URL)
        c.execute("CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, sender TEXT NOT NULL, direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'recorded', delivery_error TEXT NOT NULL DEFAULT '')")
        c.execute('CREATE TABLE IF NOT EXISTS processed_webhook_messages (message_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'recorded'")
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_error TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS conversation_meta (sender TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'Nuevo')")
        c.execute('CREATE TABLE IF NOT EXISTS conversation_state (sender TEXT PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS admin_alerts (id TEXT PRIMARY KEY, sender TEXT, body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, sent_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, sender TEXT NOT NULL, media_id TEXT, type TEXT NOT NULL, filename TEXT, local_path TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)')
        c.execute("CREATE TABLE IF NOT EXISTS prospects (id TEXT PRIMARY KEY, sender TEXT NOT NULL, contact TEXT, service TEXT, campaign TEXT, source TEXT, municipality TEXT, origin TEXT, destination TEXT, weight TEXT, dimensions TEXT, service_date TEXT, duration TEXT, operator TEXT, quoted_value TEXT, next_action TEXT, outcome TEXT, priority TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Nuevo', follow_up_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS sheets_sync_queue (sync_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, synced_at TEXT)")
        c.execute('CREATE INDEX IF NOT EXISTS idx_sheets_sync_queue_due ON sheets_sync_queue(status,next_attempt_at)')
        c.execute("CREATE TABLE IF NOT EXISTS content_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, \"copy\" TEXT NOT NULL, channel TEXT NOT NULL, campaign TEXT, status TEXT NOT NULL DEFAULT 'Borrador', scheduled_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        for col in ('municipality','origin','destination','weight','dimensions','service_date','duration','operator','quoted_value','next_action','outcome','follow_up_at'):
            c.execute(f'ALTER TABLE prospects ADD COLUMN IF NOT EXISTS {col} TEXT')
        _backfill_follow_ups(c)
        c.commit(); return c
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, direction TEXT, body TEXT, created_at TEXT, delivery_status TEXT NOT NULL DEFAULT 'recorded', delivery_error TEXT NOT NULL DEFAULT '')")
    for col,definition in (('delivery_status', "TEXT NOT NULL DEFAULT 'recorded'"), ('delivery_error', "TEXT NOT NULL DEFAULT ''")):
        try: c.execute(f'ALTER TABLE messages ADD COLUMN {col} {definition}')
        except sqlite3.OperationalError: pass
    c.execute('CREATE TABLE IF NOT EXISTS processed_webhook_messages (message_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    c.execute("CREATE TABLE IF NOT EXISTS conversation_meta (sender TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'Nuevo')")
    c.execute('CREATE TABLE IF NOT EXISTS conversation_state (sender TEXT PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS admin_alerts (id TEXT PRIMARY KEY, sender TEXT, body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, sent_at TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, sender TEXT NOT NULL, media_id TEXT, type TEXT NOT NULL, filename TEXT, local_path TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)')
    c.execute("CREATE TABLE IF NOT EXISTS prospects (id TEXT PRIMARY KEY, sender TEXT NOT NULL, contact TEXT, service TEXT, campaign TEXT, source TEXT, municipality TEXT, origin TEXT, destination TEXT, weight TEXT, dimensions TEXT, service_date TEXT, duration TEXT, operator TEXT, quoted_value TEXT, next_action TEXT, outcome TEXT, priority TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Nuevo', follow_up_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS sheets_sync_queue (sync_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, synced_at TEXT)")
    c.execute('CREATE INDEX IF NOT EXISTS idx_sheets_sync_queue_due ON sheets_sync_queue(status,next_attempt_at)')
    c.execute("CREATE TABLE IF NOT EXISTS content_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, \"copy\" TEXT NOT NULL, channel TEXT NOT NULL, campaign TEXT, status TEXT NOT NULL DEFAULT 'Borrador', scheduled_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    for col in ('municipality','origin','destination','weight','dimensions','service_date','duration','operator','quoted_value','next_action','outcome','follow_up_at'):
        try: c.execute(f'ALTER TABLE prospects ADD COLUMN {col} TEXT')
        except sqlite3.OperationalError: pass
    _backfill_follow_ups(c)
    c.commit(); return c

def load_conversation_state(sender):
    """Load a sender's state from durable storage, falling back to the menu."""
    try:
        c=db(); row=c.execute('SELECT step,data FROM conversation_state WHERE sender=?',(sender,)).fetchone(); c.close()
        if row:
            data=json.loads(row['data'])
            if isinstance(data,dict) and isinstance(row['step'],str):
                return {'step': row['step'], 'data': data}
    except Exception as e:
        print('Error cargando estado conversacional:',e,flush=True)
    return {'step':'menu','data':{}}

def save_conversation_state(sender, state):
    """Upsert the small JSON state independently of messages/prospects."""
    try:
        step=state.get('step','menu')
        data=json.dumps(state.get('data') or {}, ensure_ascii=False)
        now=datetime.now(timezone.utc).isoformat()
        c=db(); row=c.execute('SELECT sender FROM conversation_state WHERE sender=?',(sender,)).fetchone()
        if row:
            c.execute('UPDATE conversation_state SET step=?,data=?,updated_at=? WHERE sender=?',(step,data,now,sender))
        else:
            c.execute('INSERT INTO conversation_state(sender,step,data,updated_at) VALUES(?,?,?,?)',(sender,step,data,now))
        c.commit(); c.close()
    except Exception as e:
        # State persistence must never break the existing WhatsApp flow.
        print('Error guardando estado conversacional:',e,flush=True)

def clear_conversation_state(sender):
    state={'step':'menu','data':{}}
    conversations[sender]=state
    save_conversation_state(sender,state)
    return state

def save(sender,direction,body,delivery_status=None,delivery_error=''):
    # Persist a message with an explicit transport state; never imply delivery.
    status = delivery_status or ('received' if direction == 'in' else 'pending')
    created = datetime.now(timezone.utc).isoformat()
    c=db()
    if isinstance(c, PGConnection):
        row=c.execute('INSERT INTO messages(sender,direction,body,created_at,delivery_status,delivery_error) VALUES(?,?,?,?,?,?) RETURNING id',
                      (sender,direction,body,created,status,delivery_error)).fetchone()
        message_id=row['id'] if row else None
    else:
        cursor=c.execute('INSERT INTO messages(sender,direction,body,created_at,delivery_status,delivery_error) VALUES(?,?,?,?,?,?)',
                         (sender,direction,body,created,status,delivery_error))
        message_id=cursor.lastrowid
    c.commit(); c.close()
    return message_id


def update_message_delivery(message_id,status,error=''):
    if message_id is None: return
    c=db(); c.execute('UPDATE messages SET delivery_status=?,delivery_error=? WHERE id=?',(status,(error or '')[:1000],message_id)); c.commit(); c.close()


def claim_webhook_message(message_id):
    # Atomically claim an inbound Meta message once; failed processing may retry.
    now=datetime.now(timezone.utc).isoformat()
    c=db()
    cur=c.execute('INSERT INTO processed_webhook_messages(message_id,status,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(message_id) DO NOTHING',
                  (message_id,'processing',now,now))
    claimed=cur.rowcount == 1
    if not claimed:
        stale_before=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        cur=c.execute('UPDATE processed_webhook_messages SET status=?,updated_at=? WHERE message_id=? AND (status=? OR (status=? AND updated_at<?))',
                      ('processing',now,message_id,'failed','processing',stale_before))
        claimed=cur.rowcount == 1
    c.commit(); c.close()
    return claimed


def mark_webhook_message(message_id,status):
    c=db(); c.execute('UPDATE processed_webhook_messages SET status=?,updated_at=? WHERE message_id=?',
                      (status,datetime.now(timezone.utc).isoformat(),message_id)); c.commit(); c.close()


def valid_meta_signature(raw_body, signature):
    if not META_APP_SECRET or not signature:
        return False
    expected='sha256='+hmac.new(META_APP_SECRET.encode('utf-8'),raw_body,hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected,signature.strip())


def same_origin_dashboard_post(req):
    # Reject cross-site form submissions to Basic-authenticated dashboard actions.
    source=req.headers.get('Origin') or req.headers.get('Referer','')
    if not source:
        return False
    try:
        parsed=urlsplit(source)
        return parsed.scheme == 'https' and parsed.netloc.lower() == req.host.lower()
    except Exception:
        return False

def get_status(sender):
    try:
        c=db(); r=c.execute('SELECT status FROM conversation_meta WHERE sender=?',(sender,)).fetchone()
        if not r:
            c.execute('INSERT INTO conversation_meta(sender,status) VALUES(?,?)',(sender,'Nuevo')); c.commit(); status='Nuevo'
        else: status=r['status']
        c.close(); return status
    except Exception: return 'Nuevo'

def set_status(sender,status):
    c=db(); r=c.execute('SELECT sender FROM conversation_meta WHERE sender=?',(sender,)).fetchone()
    if r: c.execute('UPDATE conversation_meta SET status=? WHERE sender=?',(status,sender))
    else: c.execute('INSERT INTO conversation_meta(sender,status) VALUES(?,?)',(sender,status))
    c.commit(); c.close()

QUICK_REPLIES = [
    ('📄 Solicitar datos', 'Hola, gracias por contactarnos. Para preparar tu cotización, por favor envíanos nombre, empresa, teléfono y los datos del servicio que necesitas.'),
    ('✅ Confirmar recepción', 'Hemos recibido tu información correctamente. Nuestro equipo la revisará y un asesor se pondrá en contacto contigo.'),
    ('⏱️ Tiempo de respuesta', 'Gracias por escribirnos. Estamos revisando tu solicitud y te responderemos lo antes posible dentro de nuestro horario de atención.'),
    ('📍 Pedir ubicación', 'Por favor compártenos la ciudad y dirección exacta donde se realizará el servicio.'),
    ('📞 Pedir llamada', '¿En qué horario te podemos llamar para ampliar la información de tu solicitud?'),
]

def authorized(req):
    h=req.headers.get('Authorization','')
    if not DASHBOARD_PASSWORD or not h.startswith('Basic '): return False
    try:
        u,p=base64.b64decode(h[6:]).decode().split(':',1); return hmac.compare_digest(u,DASHBOARD_USER) and hmac.compare_digest(p,DASHBOARD_PASSWORD)
    except Exception: return False

def login(): return Response('Autenticación requerida',401,{'WWW-Authenticate':'Basic realm="DT Gruas"'})

def log_outgoing_attachment(sender, kind, filename, local_path='', media_id='', status='sent', error=''):
    """Register an attachment sent from the dashboard without contacting a client."""
    try:
        c=db(); c.execute('INSERT INTO attachments(id,sender,media_id,type,filename,local_path,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (uuid.uuid4().hex, sender, media_id, kind, filename, local_path, status, error[:500], datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
    except Exception as e:
        print('Error registrando adjunto saliente:', e, flush=True)

def upload_media(filepath, content_type):
    """Upload a local file to WhatsApp Cloud API and return its media ID."""
    if not META_ACCESS_TOKEN: raise RuntimeError('Falta META_ACCESS_TOKEN')
    boundary='----DTGruasBoundary'+uuid.uuid4().hex
    filename=os.path.basename(filepath)
    data=Path(filepath).read_bytes()
    parts=[]
    parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n').encode())
    parts.append(('--'+boundary+f'\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode()+data+b'\r\n')
    parts.append(('--'+boundary+'--\r\n').encode())
    req=urllib.request.Request(f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/media', data=b''.join(parts),
        headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':f'multipart/form-data; boundary={boundary}'}, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        result=json.loads(r.read().decode())
    media_id=result.get('id')
    if not media_id: raise RuntimeError('Meta no devolvió ID multimedia')
    return media_id

def send_audio(to, filepath, filename):
    """Upload and send an audio file, logging both the message and attachment."""
    content_type=mimetypes.guess_type(filename)[0] or 'audio/ogg'
    if not content_type.startswith('audio/'): raise RuntimeError('El archivo debe ser un audio')
    if os.path.getsize(filepath)>16*1024*1024: raise RuntimeError('El audio excede el límite de 16 MB')
    try:
        media_id=upload_media(filepath, content_type)
        url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
        payload=json.dumps({'messaging_product':'whatsapp','to':to,'type':'audio','audio':{'id':media_id}}).encode()
        req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req, timeout=15) as r: response=r.read().decode()
        save(to,'out',f'🎧 Audio enviado: {filename}','accepted_by_meta')
        log_outgoing_attachment(to,'audio',filename,filepath,media_id,'sent','')
        print('Audio enviado',response,flush=True)
        return True
    except urllib.error.HTTPError as e:
        error=f'HTTP {e.code}: {e.read().decode(errors="replace")[:500]}'
    except Exception as e: error=str(e)[:500]
    save(to,'out',f'🎧 Audio no enviado: {filename}','failed',error)
    log_outgoing_attachment(to,'audio',filename,filepath,'','failed',error)
    print('Error enviando audio:',error,flush=True)
    return False

def send_text(to,text):
    message_id=save(to,'out',text,'pending')
    if not META_ACCESS_TOKEN:
        update_message_delivery(message_id,'failed','Falta META_ACCESS_TOKEN')
        return False
    url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
    recipient={'recipient':to} if isinstance(to,str) and '.' in to else {'to':to}
    payload=json.dumps({'messaging_product':'whatsapp','recipient_type':'individual',**recipient,'type':'text','text':{'preview_url':False,'body':text}}).encode()
    req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as r:
            response=r.read().decode(errors='replace')
            update_message_delivery(message_id,'accepted_by_meta')
            print('Meta aceptó mensaje',r.status,response[:500],flush=True)
        return True
    except urllib.error.HTTPError as e:
        detail=f'HTTP {e.code}: {e.read().decode(errors="replace")[:800]}'
    except Exception as e:
        detail=str(e)[:800]
    update_message_delivery(message_id,'failed',detail)
    print('Error enviando mensaje:',detail,flush=True)
    return False

def send_admin_alert(text, sender=''):
    alert_id=uuid.uuid4().hex
    created=datetime.now(timezone.utc).isoformat()
    try:
        c=db(); c.execute('INSERT INTO admin_alerts(id,sender,body,status,attempts,error,created_at,sent_at) VALUES(?,?,?,?,?,?,?,?)',(alert_id,sender,text,'pending',0,'',created,'')); c.commit(); c.close()
    except Exception as e:
        print('Error registrando alerta:',e,flush=True)
        return False
    if not META_ACCESS_TOKEN or not ADMIN_PHONE:
        error='Falta META_ACCESS_TOKEN o ADMIN_PHONE'
        try:
            c=db(); c.execute('UPDATE admin_alerts SET status=?,error=? WHERE id=?',('failed',error,alert_id)); c.commit(); c.close()
        except Exception: pass
        print('Alerta fallida:',error,flush=True)
        return False
    url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
    payload=json.dumps({'messaging_product':'whatsapp','to':ADMIN_PHONE,'type':'text','text':{'preview_url':False,'body':text}}).encode()
    last_error='Error desconocido'
    for attempt in range(1,3):
        try:
            c=db(); c.execute('UPDATE admin_alerts SET attempts=? WHERE id=?',(attempt,alert_id)); c.commit(); c.close()
            req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=8) as r:
                response=r.read().decode()
                print('Alerta enviada',r.status,response,flush=True)
            c=db(); c.execute('UPDATE admin_alerts SET status=?,error=?,sent_at=? WHERE id=?',('sent','',datetime.now(timezone.utc).isoformat(),alert_id)); c.commit(); c.close()
            return True
        except urllib.error.HTTPError as e:
            detail=e.read().decode(errors='replace')
            last_error=f'HTTP {e.code}: {detail[:500]}'
        except Exception as e:
            last_error=str(e)[:500]
        print(f'Error alerta intento {attempt}: {last_error}',flush=True)
        if attempt < 2: time.sleep(1)
    try:
        c=db(); c.execute('UPDATE admin_alerts SET status=?,error=? WHERE id=?',('failed',last_error,alert_id)); c.commit(); c.close()
    except Exception as e: print('Error guardando fallo de alerta:',e,flush=True)
    return False

def campaign_from_text(text):
    m=re.search(r'\b(GRUA|MONTACARGAS|TRANSPORTE)(?:-[A-Z0-9]+)?\b', (text or '').upper())
    return m.group(0) if m else ''

def prospect_priority(text, completed=False):
    low=(text or '').lower()
    if any(x in low for x in ('urgente','ya','hoy','parado','no funciona','emergencia')): return 'Urgente'
    if completed: return 'Alta'
    return 'Media'

def save_prospect(sender, data, prospect_id=None):
    """Idempotently persist a prospect under the stable Sheets sync identifier."""
    sync_id = str(prospect_id or (data or {}).get('_prospect_sync_id') or uuid.uuid4().hex)
    now=datetime.now(timezone.utc).isoformat()
    details=json.dumps(data, ensure_ascii=False)
    try:
        priority=prospect_priority(details, True)
        follow_up_at=data.get('follow_up_at') or calculate_follow_up_at(priority)
        c=db(); c.execute('''INSERT INTO prospects(id,sender,contact,service,campaign,source,municipality,origin,destination,weight,dimensions,service_date,duration,operator,quoted_value,next_action,outcome,priority,details,status,follow_up_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
            (sync_id,sender,data.get('contact',''),data.get('service','Por definir'),data.get('campaign',''),data.get('source','WhatsApp'),data.get('municipality',''),data.get('origin',''),data.get('destination',''),data.get('weight',''),data.get('dimensions',''),data.get('service_date',''),data.get('duration',''),data.get('operator',''),data.get('quoted_value',''),data.get('next_action',''),data.get('outcome',''),priority,details,'Nuevo',follow_up_at,now,now)); c.commit(); c.close()
    except Exception as e:
        print('Error guardando prospecto:',e,flush=True)
    return sync_id


def build_sheets_prospect_payload(sender, data, sync_id):
    """Map the conversation state to the exact Apps Script Prospectos columns."""
    details = json.dumps(data or {}, ensure_ascii=False)
    service = (data or {}).get('service') or (data or {}).get('service_details') or (data or {}).get('sale_details') or (data or {}).get('visit_details') or ''
    return {
        'sync_id': str(sync_id),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'name': (data or {}).get('contact', ''),
        'company': (data or {}).get('company', ''),
        'phone': sender or '',
        'service': service,
        'municipality': (data or {}).get('municipality') or (data or {}).get('work_location') or '',
        'origin': (data or {}).get('origin', ''),
        'destination': (data or {}).get('destination', ''),
        'service_date': (data or {}).get('service_date', ''),
        'source': (data or {}).get('source') or 'WhatsApp',
        'priority': prospect_priority(details, True),
        'status': (data or {}).get('status') or 'Nuevo',
        'responsible': (data or {}).get('responsible', ''),
        'quoted_value': (data or {}).get('quoted_value', ''),
        'outcome': (data or {}).get('outcome', ''),
        'next_action': (data or {}).get('next_action', ''),
        'followup_date': (data or {}).get('followup_date') or (data or {}).get('follow_up_at') or '',
    }


def enqueue_sheets_sync(sync_id, payload):
    """Persist one idempotent job and report its current state on duplicate enqueue."""
    sync_id = str(sync_id or '').strip()
    if not sync_id:
        return False, 'sync_id requerido'
    now = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps(payload, ensure_ascii=False)
    c = None
    try:
        c=db()
        c.execute('''INSERT INTO sheets_sync_queue(sync_id,payload,status,attempts,next_attempt_at,last_error,created_at,updated_at,synced_at)
            VALUES(?,?, 'pending', 0, ?, '', ?, ?, NULL) ON CONFLICT(sync_id) DO NOTHING''', (sync_id,serialized,now,now,now))
        row=c.execute('SELECT status FROM sheets_sync_queue WHERE sync_id=?',(sync_id,)).fetchone()
        c.commit(); c.close(); c=None
        status=row['status'] if row else 'pending'
        if status == 'synced':
            return True, 'already_synced'
        if status == 'processing':
            return True, 'processing'
        return True, 'queued'
    except Exception as e:
        if c:
            try: c.close()
            except Exception: pass
        print('Error encolando sincronización a Sheets:',e,flush=True)
        return False, str(e)[:300]


def process_sheets_sync_queue(limit=5, sync_id=None):
    """Claim due jobs atomically, retry with backoff, and reclaim expired claims."""
    try:
        limit=max(1,min(int(limit or 5),20))
    except (TypeError, ValueError):
        limit=5
    now_dt=datetime.now(timezone.utc)
    now=now_dt.isoformat()
    expired_before=(now_dt-timedelta(minutes=15)).isoformat()
    try:
        c=db()
        # Requests time out far sooner than this lease; reclaim only abandoned jobs.
        c.execute("UPDATE sheets_sync_queue SET status='pending',next_attempt_at=?,last_error=CASE WHEN last_error='' THEN 'expired_processing_claim' ELSE last_error END,updated_at=? WHERE status='processing' AND updated_at<=?",(now,now,expired_before))
        if sync_id:
            rows=c.execute("SELECT sync_id,payload,attempts FROM sheets_sync_queue WHERE status='pending' AND next_attempt_at<=? AND sync_id=? ORDER BY created_at LIMIT ?",(now,str(sync_id),limit)).fetchall()
        else:
            rows=c.execute("SELECT sync_id,payload,attempts FROM sheets_sync_queue WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,created_at LIMIT ?",(now,limit)).fetchall()
        c.commit(); c.close()
    except Exception as e:
        return {'ok':False,'attempted':0,'synced':0,'error':str(e)[:300]}

    attempted=0
    synced=0
    results=[]
    for row in rows:
        row_id=str(row['sync_id'])
        claimed_at=datetime.now(timezone.utc).isoformat()
        claimed=False
        c=None
        try:
            c=db()
            claim=c.execute("UPDATE sheets_sync_queue SET status='processing',updated_at=? WHERE sync_id=? AND status='pending' AND next_attempt_at<=?",(claimed_at,row_id,now))
            claimed=(claim.rowcount == 1)
            c.commit(); c.close(); c=None
        except Exception:
            if c:
                try: c.close()
                except Exception: pass
            continue
        if not claimed:
            continue
        attempted += 1
        try:
            payload=json.loads(row['payload'])
            ok,detail=sync_prospect_to_sheets(payload)
        except Exception as e:
            ok,detail=False,str(e)[:300]
        updated=datetime.now(timezone.utc)
        c=db()
        if ok:
            c.execute("UPDATE sheets_sync_queue SET status='synced',attempts=attempts+1,last_error='',updated_at=?,synced_at=? WHERE sync_id=? AND status='processing'",(updated.isoformat(),updated.isoformat(),row_id))
            synced += 1
            results.append({'sync_id':row_id,'status':'synced'})
        else:
            attempts=int(row['attempts'] or 0) + 1
            delay_seconds=min(21600,60 * (2 ** min(attempts - 1, 8)))
            next_attempt=(updated + timedelta(seconds=delay_seconds)).isoformat()
            c.execute("UPDATE sheets_sync_queue SET status='pending',attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE sync_id=? AND status='processing'",(attempts,next_attempt,(detail or 'sync_failed')[:500],updated.isoformat(),row_id))
            results.append({'sync_id':row_id,'status':'pending','attempts':attempts,'next_attempt_at':next_attempt})
        c.commit(); c.close()
    return {'ok':True,'attempted':attempted,'synced':synced,'results':results}


def sync_prospect_to_sheets(prospect):
    """Send one stable-id payload and require an explicit Apps Script success ack."""
    if not APPS_SCRIPT_WEBHOOK_URL:
        return False, 'APPS_SCRIPT_WEBHOOK_URL no configurada'
    if not APPS_SCRIPT_SYNC_TOKEN:
        return False, 'APPS_SCRIPT_SYNC_TOKEN no configurado'
    prospect_payload = dict(prospect or {})
    if not str(prospect_payload.get('sync_id') or '').strip():
        return False, 'sync_id requerido para idempotencia'
    payload = {'prospect': prospect_payload, 'token': APPS_SCRIPT_SYNC_TOKEN}
    req = urllib.request.Request(
        APPS_SCRIPT_WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body=response.read().decode('utf-8', errors='replace')
            if not 200 <= response.status < 300:
                return False, f'HTTP {response.status}: {body[:300]}'
            try:
                result=json.loads(body)
            except ValueError:
                return False, 'Respuesta de Apps Script no es JSON válido'
            if not isinstance(result, dict) or result.get('ok') is not True:
                return False, str(result.get('error') or 'Apps Script no confirmó la escritura')[:300] if isinstance(result, dict) else 'Respuesta inválida de Apps Script'
            return True, 'duplicado ya registrado' if result.get('duplicate') else 'guardado confirmado'
    except Exception as error:
        return False, str(error)[:300]

def _marketing_sync_token():
    """Separate inbound admin authentication from the outbound Apps Script token."""
    return (MARKETING_SYNC_TOKEN or APPS_SCRIPT_SYNC_TOKEN).strip()


def _marketing_request_authorized(data):
    expected=request.headers.get('X-Sync-Token','') or data.get('token','')
    token=_marketing_sync_token()
    return bool(token and expected and hmac.compare_digest(str(expected),token))


@app.post('/marketing/sync-sheets')
def marketing_sync_sheets():
    data=request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'JSON inválido'}, 400
    if not _marketing_sync_token():
        return {'ok': False, 'error': 'Sincronización no configurada de forma segura'}, 503
    if not _marketing_request_authorized(data):
        return {'ok': False, 'error': 'No autorizado'}, 403
    if 'prospect' in data:
        prospect=data.get('prospect')
        if not isinstance(prospect, dict):
            return {'ok':False,'error':'prospect debe ser un objeto'},400
        prospect=dict(prospect)
    else:
        # Preserve the legacy flat JSON shape while excluding its auth token.
        prospect={key:value for key,value in data.items() if key != 'token'}
    sync_id=str(prospect.get('sync_id') or request.headers.get('Idempotency-Key','')).strip()
    generated_sync_id=not bool(sync_id)
    if generated_sync_id:
        # Keep legacy callers working; new callers should send an Idempotency-Key.
        sync_id=uuid.uuid4().hex
    prospect['sync_id']=sync_id
    queued,detail=enqueue_sheets_sync(sync_id,prospect)
    if not queued:
        return {'ok':False,'error':detail},500
    if detail == 'already_synced':
        return {'ok':True,'duplicate':True,'sync_id':sync_id,'sync_id_generated':generated_sync_id,'status':'synced','detail':'ya registrado'},200
    if detail == 'processing':
        return {'ok':False,'duplicate':False,'sync_id':sync_id,'sync_id_generated':generated_sync_id,'status':'processing','detail':'ya está en proceso'},202
    result=process_sheets_sync_queue(limit=1,sync_id=sync_id)
    synced=bool(result.get('synced'))
    return {'ok':synced,'duplicate':False,'sync_id':sync_id,'sync_id_generated':generated_sync_id,'status':'synced' if synced else 'pending','detail':result}, (200 if synced else 202)


@app.post('/marketing/retry-sheets')
def marketing_retry_sheets():
    data=request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {'ok':False,'error':'JSON inválido'},400
    if not _marketing_sync_token():
        return {'ok':False,'error':'Sincronización no configurada de forma segura'},503
    if not _marketing_request_authorized(data):
        return {'ok':False,'error':'No autorizado'},403
    result=process_sheets_sync_queue(limit=data.get('limit',5))
    return result, (200 if result.get('ok') else 503)


def finish_request(s, sender=''):
    d=s['data']; s['step']='done'
    if d.get('service'):
        if d.get('service')=='Alquiler de grúa tipo planchón':
            detail=(f"Vehículo/equipo, recogida y destino: {d.get('equipment_details','')}\nFecha y hora: {d.get('dates_operator','')}" )
        else:
            detail=(f"Detalle: {d.get('equipment_details','')}\nTrabajo y ubicación: {d.get('work_location','')}\nFecha, duración y operador: {d.get('dates_operator','')}" )
    elif d.get('service_details'):
        detail='Caso: '+d['service_details']
    elif d.get('sale_details'):
        detail='Solicitud: '+d['sale_details']
    else:
        detail='Visita: '+d.get('visit_details','')
    files=len(d.get('attachments',[]))
    attached=f'\nArchivos adjuntos: {files}' if files else ''
    extra=f"\nInformación adicional: {d.get('additional_info','')}" if d.get('additional_info') else ''
    if not d.get('_prospect_saved'):
        sync_id=str(d.get('_prospect_sync_id') or uuid.uuid4().hex)
        d['_prospect_sync_id']=sync_id
        # Persist the id before either write so a repeated completion reuses it.
        if sender:
            save_conversation_state(sender,s)
        save_prospect(sender,d,sync_id)
        payload=build_sheets_prospect_payload(sender,d,sync_id)
        queued,queue_detail=enqueue_sheets_sync(sync_id,payload)
        if queued:
            d['_prospect_saved']=True
            result=process_sheets_sync_queue(limit=1,sync_id=sync_id)
            print(f"Sincronización a Sheets {sync_id}: {result}", flush=True)
        else:
            print(f"No se pudo encolar la sincronización a Sheets {sync_id}: {queue_detail}", flush=True)
    return f"✅ Gracias por la información.\n\n{detail}\nCliente: {d['contact']}{attached}{extra}\n\nYa un asesor de DT Grúas y Montacargas te atenderá."

def _process(sender,text):
    text=(text or '').strip(); low=text.lower(); s=conversations.setdefault(sender,{'step':'menu','data':{}})
    campaign=campaign_from_text(text)
    if campaign:
        current=s.setdefault('data',{}).get('campaign','')
        if not current or ('-' in campaign and '-' not in current): s['data']['campaign']=campaign
    if 'facebook' in low or 'instagram' in low or 'meta ads' in low: s.setdefault('data',{})['source']='Meta'
    if low in {'hola','buenas','inicio','menu','menú','menu principal','menú principal','0','reiniciar'}:
        s.update(step='menu',data={}); return WELCOME
    if s['step']=='menu':
        if text=='1' or 'montacarga' in low:
            s.update(step='rental_equipment',data={'service':'Alquiler de montacargas', **({'campaign':campaign} if campaign else {})})
            return 'Perfecto. ¿Qué capacidad y altura de elevación necesitas?'
        if text=='2' or ('alquiler' in low and 'grúa' in low) or ('alquiler' in low and 'grua' in low) or 'planchón' in low or 'planchon' in low:
            s.update(step='rental_equipment',data={'service':'Alquiler de grúa tipo planchón', **({'campaign':campaign} if campaign else {})})
            return 'Perfecto. ¿Qué vehículo o equipo necesitas transportar y cuál es el lugar de recogida y destino?'
        if text=='3' or 'mantenimiento' in low or 'reparación' in low or 'reparacion' in low:
            s['data']['service']='Mantenimiento y reparación'; s['step']='service'; return 'Cuéntanos qué equipo necesita mantenimiento o reparación y cuál es la falla.'
        if text=='4' or 'venta' in low or 'repuesto' in low:
            s['data']['service']='Venta de equipos o repuestos'; s['step']='sale'; return '¿Qué equipo o repuesto buscas? Indícanos la marca o referencia, si la conoces.'
        if text=='5' or 'visita' in low:
            s['data']['service']='Visita técnica'; s['step']='visit'; return '¿En qué ciudad o dirección necesitas la visita técnica y qué equipo revisaremos?'
        return 'Por favor responde con un número del 1 al 5.\n\n'+WELCOME
    if s['step']=='rental_equipment':
        s['data']['equipment_details']=text
        if s['data'].get('service')=='Alquiler de grúa tipo planchón':
            s['step']='origin'; return '¿Cuál es el lugar exacto de recogida?'
        s['step']='work'
        return '¿Qué tipo de trabajo realizarás y en qué ciudad o dirección?'
    if s['step']=='origin':
        s['data']['origin']=text; s['step']='destination'; return '¿Cuál es el destino exacto?'
    if s['step']=='destination':
        s['data']['destination']=text; s['step']='date'; return '¿Para qué fecha y hora necesitas el servicio?'
    if s['step']=='work':
        s['data']['work_location']=text; s['step']='municipality'; return '¿En qué municipio se realizará el trabajo?'
    if s['step']=='municipality':
        s['data']['municipality']=text; s['step']='date'; return '¿Para qué fecha necesitas el servicio?'
    if s['step']=='date':
        s['data']['service_date']=text
        if s['data'].get('service')=='Alquiler de grúa tipo planchón':
            s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto? Puedes enviar una foto del vehículo o equipo si aplica.'
        s['step']='duration'; return '¿Por cuánto tiempo necesitas el alquiler?'
    if s['step']=='duration':
        s['data']['duration']=text; s['step']='operator'; return '¿Requieres operador? Responde SI o NO.'
    if s['step']=='operator':
        s['data']['operator']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='dates':
        s['data']['dates_operator']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='attachments_choice':
        if low in {'si','sí','s'}:
            s['step']='additional_info'; return 'Por privacidad no almacenamos archivos adjuntos. Escribe aquí los datos importantes que quieras compartir por texto.'
        if low in {'no','n'}:
            return finish_request(s, sender)
        return 'Por favor responde SI o NO. ¿Deseas agregar información adicional por escrito?'
    if s['step']=='attachments':
        s['step']='additional_info'
        return 'Por privacidad no almacenamos archivos adjuntos. Escribe aquí los datos importantes que quieras compartir por texto; si no deseas agregar nada, responde NO.'
    if s['step']=='additional_choice':
        if low in {'si','sí','s'}:
            s['step']='additional_info'; return 'Escribe la información adicional que deseas agregar.'
        if low in {'no','n'}:
            return finish_request(s, sender)
        return 'Por favor responde SI o NO. ¿Deseas agregar alguna información adicional?'
    if s['step']=='additional_info':
        if low in {'no','n'}:
            return finish_request(s, sender)
        if low in {'si','sí','s'}:
            return 'Escribe aquí la información importante que quieras agregar por texto.'
        s['data']['additional_info']=text; return finish_request(s, sender)
    if s['step']=='contact':
        s['data']['contact']=text; s['step']='attachments_choice'
        return '¿Deseas agregar información adicional por escrito? Responde SI o NO. Por privacidad no almacenamos archivos adjuntos.'
    if s['step']=='service':
        s['data']['service_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='sale':
        s['data']['sale_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='visit':
        s['data']['visit_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    return 'Escribe hola para volver al menú principal.'

def process(sender,text):
    # Render instances are ephemeral: hydrate before every turn and persist even
    # when a branch returns early. The in-memory dict remains for compatibility.
    conversations[sender]=load_conversation_state(sender)
    try:
        return _process(sender,text)
    finally:
        save_conversation_state(sender, conversations.get(sender, {'step':'menu','data':{}}))

@app.get('/webhook')
def verify():
    if not VERIFY_TOKEN: return 'Webhook verification is not configured',503
    if request.args.get('hub.mode')=='subscribe' and hmac.compare_digest(request.args.get('hub.verify_token',''),VERIFY_TOKEN): return request.args.get('hub.challenge',''),200
    return 'Forbidden',403

@app.post('/webhook')
def webhook():
    raw_body=request.get_data(cache=True)
    if not META_APP_SECRET:
        print('Webhook rechazado: META_APP_SECRET no configurado',flush=True)
        return 'Webhook security is not configured',503
    if not valid_meta_signature(raw_body,request.headers.get('X-Hub-Signature-256','')):
        return 'Invalid signature',401
    try:
        data=json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError,ValueError):
        return 'Invalid JSON',400
    if not isinstance(data,dict):
        return 'Invalid payload',400
    try:
        for entry in data.get('entry',[]):
            for change in entry.get('changes',[]):
                for m in change.get('value',{}).get('messages',[]):
                    sender=m.get('from') or m.get('from_user_id')
                    kind=m.get('type')
                    if not sender or kind not in ('text','image','video','document','audio'):
                        continue
                    message_id=m.get('id')
                    if not message_id:
                        raise ValueError('Meta message is missing its id')
                    if not claim_webhook_message(str(message_id)):
                        print('Evento repetido ignorado',message_id,flush=True)
                        continue
                    try:
                        if kind=='text':
                            text=m.get('text',{}).get('body','')
                            reply=process(sender,text)
                        else:
                            # Do not fetch, persist, or log customer-supplied attachment details.
                            text='📎 El cliente envió un adjunto; no se almacenó por privacidad.'
                            conversations[sender]=load_conversation_state(sender)
                            state=conversations[sender]
                            current_step=state.get('step','menu')
                            if current_step in ('attachments','attachments_choice','additional_choice'):
                                state['step']='additional_info'
                                reply='Por privacidad no almacenamos archivos adjuntos. Si quieres incluir información importante de la foto o documento, escríbela aquí; si no, responde NO.'
                            elif current_step=='additional_info':
                                reply='Por privacidad no almacenamos archivos adjuntos. Escribe aquí cualquier dato importante que quieras agregar o responde NO para finalizar.'
                            else:
                                reply='Por privacidad no almacenamos archivos adjuntos. Si contienen información importante para tu solicitud, descríbela aquí y continuemos con la pregunta pendiente.'
                            save_conversation_state(sender,state)
                        if 'En cualquier momento escribe MENU' not in reply:
                            reply += '\n\n↩️ Menú principal: escribe 0 o MENU PRINCIPAL.'
                        save(sender,'in',text,'received')
                        send_text(sender,reply)
                        state=conversations.get(sender,{})
                        urgent=any(x in text.lower() for x in ('urgente','ya','hoy','parado','no funciona','emergencia'))
                        qualified=state.get('step') in ('done','advisor')
                        if sender != ADMIN_PHONE:
                            service=state.get('data',{}).get('service','Por definir')
                            received_at=format_dt(datetime.now(timezone.utc).isoformat())
                            reason=('Solicitud urgente' if urgent else ('Solicitud completada' if qualified else 'Nuevo contacto'))
                            type_label='Texto' if kind=='text' else 'Adjunto no almacenado'
                            alert=('🔔 NUEVA SOLICITUD — DT GRÚAS\n\n'
                                   +'📅 Fecha y hora: '+received_at+'\n'
                                   +'📱 Cliente: '+sender+'\n'
                                   +'🛠️ Servicio: '+service+'\n'
                                   +'📎 Tipo de mensaje: '+type_label+'\n'
                                   +'📌 Estado: '+reason+'\n'
                                   +'💬 Mensaje recibido:\n'+text+'\n\n'
                                   +'🔎 Revisa la conversación en el visor: https://dt-gruas-webhook.onrender.com/dashboard')
                            send_admin_alert(alert,sender)
                        mark_webhook_message(str(message_id),'processed')
                    except Exception as message_error:
                        try: mark_webhook_message(str(message_id),'failed')
                        except Exception: pass
                        print('Error procesando mensaje entrante:',str(message_error)[:500],flush=True)
                        return 'Message processing failed',500
    except Exception as e:
        print('Error procesando webhook:',str(e)[:500],flush=True)
        return 'Webhook processing failed',500
    return 'EVENT_RECEIVED',200

@app.get('/dashboard/export.csv')
def export_csv():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT id,sender,direction,body,created_at,delivery_status,delivery_error FROM messages ORDER BY id').fetchall(); c.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(['ID','Cliente','Dirección','Mensaje','Fecha y hora','Estado de envío','Detalle de fallo'])
    for r in rows: writer.writerow([r['id'],r['sender'],'Entrante' if r['direction']=='in' else 'Saliente',r['body'],r['created_at'],r['delivery_status'],r['delivery_error']])
    data='\\ufeff'+buf.getvalue()
    return Response(data, mimetype='text/csv; charset=utf-8', headers={'Content-Disposition':'attachment; filename=dt_gruas_respaldo_conversaciones.csv'})

@app.get('/dashboard/print')
def print_all():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT sender,direction,body,created_at FROM messages ORDER BY id').fetchall(); c.close()
    out=['<!doctype html><meta charset="utf-8"><title>Respaldo de conversaciones - DT Grúas</title><style>body{font-family:Arial;margin:28px}.toolbar{margin-bottom:20px}button{padding:10px 16px}.msg{padding:8px;margin:6px 0;border-radius:6px;white-space:pre-wrap}.in{background:#eee}.out{background:#d9fdd3}</style><div class="toolbar"><button onclick="window.print()">Imprimir / Guardar como PDF</button> <a href="/dashboard">Volver al visor</a></div><h1>Respaldo de conversaciones</h1><p>Generado: '+html.escape(format_dt(datetime.now(timezone.utc).isoformat()))+'</p>']
    current=None
    for r in rows:
        if r['sender']!=current:
            current=r['sender']; out.append('<hr><h2>Cliente: '+html.escape(r['sender'])+'</h2>')
        cls='in' if r['direction']=='in' else 'out'; label='Entrante' if cls=='in' else 'Saliente'
        out.append(f'<div class="msg {cls}"><b>{label}</b> · {html.escape(format_dt(r["created_at"]))}<br>{html.escape(r["body"])}</div>')
    if not rows: out.append('<p>No hay mensajes todavía.</p>')
    return ''.join(out)

@app.get('/dashboard/prospects.csv')
def prospects_csv():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT id,sender,contact,service,campaign,source,priority,status,details,follow_up_at,created_at,updated_at FROM prospects ORDER BY created_at DESC').fetchall(); c.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(['ID','Teléfono','Contacto','Servicio','Campaña','Fuente','Prioridad','Estado','Seguimiento','Estado seguimiento','Detalles','Creado','Actualizado'])
    for r in rows: writer.writerow([r['id'],r['sender'],r['contact'],r['service'],r['campaign'],r['source'],r['priority'],r['status'],r['follow_up_at'],follow_up_state(r['follow_up_at'],r['status']),r['details'],r['created_at'],r['updated_at']])
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=dt_gruas_prospectos.csv'})

@app.route('/dashboard/prospects', methods=['GET','POST'])
def prospects_dashboard():
    if not authorized(request): return login()
    if request.method=='POST' and not same_origin_dashboard_post(request): return Response('Origen no permitido',403)
    if request.method=='POST':
        pid=request.form.get('id',''); status=request.form.get('status','').strip()
        if pid and status:
            c=db(); c.execute('UPDATE prospects SET status=?,updated_at=? WHERE id=?',(status,datetime.now(timezone.utc).isoformat(),pid)); c.commit(); c.close()
    q=request.args.get('q','').strip().lower(); campaign=request.args.get('campaign','').strip(); priority=request.args.get('priority','').strip(); pending=request.args.get('pending','')=='1'
    c=db(); rows=c.execute('SELECT * FROM prospects ORDER BY created_at DESC').fetchall(); c.close()
    rows=[r for r in rows if (not q or q in (r['sender'] or '').lower() or q in (r['contact'] or '').lower() or q in (r['service'] or '').lower()) and (not campaign or r['campaign']==campaign) and (not priority or r['priority']==priority) and (not pending or is_follow_up_pending(r))]
    out=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prospectos DT</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}.card{background:white;padding:16px;margin:10px 0;border-radius:8px}.warning{background:#fff3cd;border:1px solid #e0a800;padding:14px;border-radius:8px}input,select,button{padding:9px;margin:4px}.tag{display:inline-block;background:#e8f1f8;padding:4px;border-radius:5px;margin:2px}.follow{font-weight:bold}.overdue{color:#b42318}.soon{color:#9a6700}.btn{background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none}</style><a class="btn" href="/dashboard">← Visor</a> <a class="btn" href="/dashboard/prospects.csv">CSV</a><h1>Prospectos comerciales</h1><div class="warning">⚠️ <b>Revisión manual requerida:</b> los seguimientos solo son información de apoyo. No se envían mensajes automáticamente.</div><form><input name="q" placeholder="Contacto, teléfono o servicio" value="'+html.escape(request.args.get('q',''))+'"><input name="campaign" placeholder="Campaña" value="'+html.escape(campaign)+'"><select name="priority"><option value="">Todas las prioridades</option>'+''.join(f'<option {"selected" if x==priority else ""}>{x}</option>' for x in ('Urgente','Alta','Media','Baja'))+'</select><label><input type="checkbox" name="pending" value="1" '+('checked' if pending else '')+'> Seguimiento pendiente</label><button>Filtrar</button></form>']
    for r in rows:
        status=r['status'] or 'Nuevo'; follow_state=follow_up_state(r['follow_up_at'],status); follow_class='overdue' if follow_state=='Vencido' else ('soon' if follow_state=='Próximo' else '')
        follow_text=(f'Seguimiento: <span class="follow {follow_class}">{html.escape(follow_state)} · {html.escape(format_dt(r["follow_up_at"]))}</span>') if r['follow_up_at'] else 'Seguimiento: sin fecha'
        out.append(f'<div class="card"><b>{html.escape(r["contact"] or "Sin contacto")}</b> · {html.escape(r["sender"]) }<br><span class="tag">{html.escape(r["service"] or "Por definir")}</span><span class="tag">{html.escape(r["campaign"] or "Sin campaña")}</span><span class="tag">{html.escape(r["priority"])}</span><br>{follow_text}<br>Estado: <form method="post" style="display:inline"><input type="hidden" name="id" value="{html.escape(r["id"])}"><select name="status">'+''.join(f'<option {"selected" if x==status else ""}>{x}</option>' for x in ('Nuevo','Contactado','Cotización pendiente','Cotizado','En negociación','Ganado','Perdido','Seguimiento'))+'</select><button>Guardar</button></form><br><small>{html.escape((r["details"] or "")[:500])}</small></div>')
    if not rows: out.append('<div class="card">No hay prospectos.</div>')
    return ''.join(out)

CONTENT_STATUSES = ('Borrador', 'Pendiente de aprobación', 'Aprobado', 'Publicado')

@app.route('/dashboard/content', methods=['GET', 'POST'])
def content_dashboard():
    """Supervised content workspace; stores/previews copy only, never publishes."""
    if not authorized(request): return login()
    if request.method=='POST' and not same_origin_dashboard_post(request): return Response('Origen no permitido',403)
    now = datetime.now(timezone.utc).isoformat()
    if request.method == 'POST':
        action, item_id = request.form.get('action', 'save'), request.form.get('id', '').strip()
        if action == 'status' and item_id:
            status = request.form.get('status', 'Borrador').strip()
            if status in CONTENT_STATUSES:
                c=db(); c.execute('UPDATE content_items SET status=?,updated_at=? WHERE id=?',(status,now,item_id)); c.commit(); c.close()
            return redirect('/dashboard/content')
        title=request.form.get('title','').strip(); copy_text=request.form.get('copy','').strip(); channel=request.form.get('channel','').strip(); campaign=request.form.get('campaign','').strip(); scheduled=request.form.get('scheduled_at','').strip() or None
        if not title or not copy_text or not channel: return Response('Título, texto y canal son obligatorios',400)
        c=db()
        if item_id: c.execute('UPDATE content_items SET title=?,"copy"=?,channel=?,campaign=?,scheduled_at=?,updated_at=? WHERE id=?',(title,copy_text,channel,campaign,scheduled,now,item_id))
        else: c.execute('INSERT INTO content_items(id,title,"copy",channel,campaign,status,scheduled_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,title,copy_text,channel,campaign,'Borrador',scheduled,now,now))
        c.commit(); c.close(); return redirect('/dashboard/content')
    edit_id=request.args.get('edit','').strip(); c=db(); rows=c.execute('SELECT id,title,"copy",channel,campaign,status,scheduled_at,created_at,updated_at FROM content_items ORDER BY updated_at DESC').fetchall(); editing=next((r for r in rows if r['id']==edit_id),None); c.close()
    def v(k): return html.escape(str((editing[k] if editing else '') or ''),quote=True)
    eid=html.escape(editing['id'],quote=True) if editing else ''
    out=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Contenidos orgánicos - DT Grúas</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8;color:#1f2937}.card,.editor{background:white;padding:16px;margin:12px 0;border-radius:8px;max-width:900px}.btn,button{background:#075e9b;color:white;padding:9px 13px;border:0;border-radius:6px;text-decoration:none;margin:4px}.muted{color:#667085}.preview{background:#f8fafc;border:1px solid #d0d5dd;padding:12px;white-space:pre-wrap;border-radius:6px}input,select,textarea{padding:9px;margin:4px 0;width:95%;box-sizing:border-box}textarea{min-height:130px}</style><a class="btn" href="/dashboard">← Visor</a><h1>Contenidos orgánicos</h1><p class="muted">Flujo supervisado: solo guarda, revisa y aprueba textos. No se publica nada automáticamente.</p><div class="editor"><h2>'+('Editar contenido' if editing else 'Nuevo contenido')+'</h2><form method="post"><input type="hidden" name="id" value="'+eid+'"><label>Título<br><input name="title" required value="'+v('title')+'"></label><br><label>Texto / copy<br><textarea name="copy" required>'+v('copy')+'</textarea></label><br><label>Canal<br><input name="channel" required placeholder="Instagram, Facebook..." value="'+v('channel')+'"></label><br><label>Campaña (opcional)<br><input name="campaign" value="'+v('campaign')+'"></label><br><label>Fecha programada (referencia)<br><input type="datetime-local" name="scheduled_at" value="'+v('scheduled_at')+'"></label><br><button>Guardar borrador</button>'+(' <a class="btn" href="/dashboard/content">Cancelar</a>' if editing else '')+'</form></div>']
    for r in rows:
        sid=html.escape(r['id'],quote=True); opts=''.join('<option '+('selected' if x==r['status'] else '')+'>'+x+'</option>' for x in CONTENT_STATUSES)
        out.append('<div class="card"><h2>'+html.escape(r['title'])+'</h2><p><b>Canal:</b> '+html.escape(r['channel'])+' · <b>Campaña:</b> '+html.escape(r['campaign'] or '—')+' · <b>Estado:</b> '+html.escape(r['status'] or 'Borrador')+'</p><p><b>Vista previa:</b></p><div class="preview">'+html.escape(r['copy'])+'</div><p class="muted">Programado: '+html.escape(r['scheduled_at'] or 'Sin fecha')+' · Actualizado: '+html.escape(format_dt(r['updated_at']))+'</p><a class="btn" href="/dashboard/content?edit='+sid+'">Editar</a><form method="post" style="display:inline"><input type="hidden" name="action" value="status"><input type="hidden" name="id" value="'+sid+'"><select name="status">'+opts+'</select><button>Cambiar estado</button></form></div>')
    if not rows: out.append('<div class="card">No hay contenidos. Crea el primer borrador.</div>')
    return ''.join(out)

@app.get('/dashboard/stats')
def stats():
    if not authorized(request): return login()
    c=db()
    total_conversations=c.execute('SELECT COUNT(DISTINCT sender) n FROM messages').fetchone()['n']
    total_messages=c.execute('SELECT COUNT(*) n FROM messages').fetchone()['n']
    total_prospects=c.execute('SELECT COUNT(*) n FROM prospects').fetchone()['n']
    incoming=c.execute("SELECT COUNT(*) n FROM messages WHERE direction='in'").fetchone()['n']
    status_rows=c.execute('SELECT status,COUNT(*) n FROM conversation_meta GROUP BY status ORDER BY status').fetchall()
    alert_rows=c.execute('SELECT status,COUNT(*) n FROM admin_alerts GROUP BY status ORDER BY status').fetchall()
    service_rows=c.execute("SELECT body,COUNT(DISTINCT sender) n FROM messages WHERE direction='out' AND (body LIKE '%montacargas%' OR body LIKE '%grúa%' OR body LIKE '%Mantenimiento%' OR body LIKE '%Venta%' OR body LIKE '%Visita%') GROUP BY body").fetchall()
    c.close()
    statuses={r['status']:r['n'] for r in status_rows}
    alert_statuses={r['status']:r['n'] for r in alert_rows}
    services=[('Alquiler de montacargas',0),('Alquiler de grúa tipo planchón',0),('Mantenimiento o reparación',0),('Venta de equipos o repuestos',0),('Visita técnica',0)]
    # Count conversations by service keywords found in their outbound questions/summaries.
    for label,_ in services:
        terms=[label]
        if label.startswith('Alquiler de montacargas'): terms=['montacargas']
        elif label.startswith('Alquiler de grúa'): terms=['grúa','grua','planchón','planchon']
        elif label.startswith('Mantenimiento'): terms=['mantenimiento','reparación','reparacion']
        elif label.startswith('Venta'): terms=['venta','repuesto']
        elif label.startswith('Visita'): terms=['visita técnica','visita tecnica']
        n=0
        for term in terms:
            n=max(n, next((r['n'] for r in service_rows if term.lower() in r['body'].lower()),0))
        services[services.index((label,0))]=(label,n)
    cards=''.join(f'<div class="metric"><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>' for k,v in [('Conversaciones',total_conversations),('Mensajes totales',total_messages),('Mensajes de clientes',incoming),('Prospectos',total_prospects)])
    status_html=''.join(f'<tr><td>{html.escape(k)}</td><td><b>{v}</b></td></tr>' for k,v in statuses.items()) or '<tr><td colspan="2">Sin estados registrados</td></tr>'
    alert_html=''.join(f'<tr><td>{html.escape(k)}</td><td><b>{v}</b></td></tr>' for k,v in alert_statuses.items()) or '<tr><td colspan="2">Sin alertas registradas</td></tr>'
    service_html=''.join(f'<tr><td>{html.escape(k)}</td><td><b>{v}</b></td></tr>' for k,v in services)
    return '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Estadísticas - DT Grúas</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8;color:#1f2937}.top{display:flex;gap:10px;flex-wrap:wrap}.metric,.box{background:white;padding:18px;border-radius:10px;margin:8px 0;box-shadow:0 1px 4px #ccd}.metric{min-width:160px}.metric b{display:block;font-size:30px;color:#075e9b}.metric span{display:block;margin-top:6px}table{width:100%;max-width:600px;border-collapse:collapse;background:white;margin:10px 0 22px}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.btn{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin-bottom:16px}</style><a class="btn" href="/dashboard">← Volver al visor</a><h1>Estadísticas comerciales</h1><div class="top">'+cards+'</div><div class="box"><h2>Conversaciones por estado</h2><table><tr><th>Estado</th><th>Cantidad</th></tr>'+status_html+'</table><h2>Alertas administrativas</h2><table><tr><th>Estado</th><th>Cantidad</th></tr>'+alert_html+'</table><h2>Solicitudes por servicio</h2><table><tr><th>Servicio</th><th>Conversaciones</th></tr>'+service_html+'</table></div>'

@app.get('/dashboard')
def dashboard():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT sender,MAX(created_at) last_time,COUNT(*) total FROM messages GROUP BY sender ORDER BY last_time DESC').fetchall(); c.close()
    q=request.args.get('q','').strip().lower(); status_filter=request.args.get('status','').strip()
    rows=[r for r in rows if (not q or q in r['sender'].lower()) and (not status_filter or get_status(r['sender'])==status_filter)]
    q_safe=html.escape(request.args.get('q','')); status_safe=html.escape(status_filter)
    opts=''.join(f'<option {"selected" if x==status_filter else ""}>{x}</option>' for x in ('','Nuevo','Contactado','Cotización pendiente','Servicio contratado','Cerrado'))
    out=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>DT Grúas</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}.card{background:white;padding:16px;margin:10px 0;border-radius:8px}a{color:#075e9b}.actions{margin:16px 0}.btn{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin-right:8px}input,select,button{padding:9px;margin:4px}</style><h1>DT Grúas y Montacargas</h1><p>Conversaciones</p><form method="get"><input name="q" placeholder="Buscar por teléfono" value="'+q_safe+'"><select name="status">'+opts+'</select><button>Filtrar</button> <a href="/dashboard">Limpiar</a></form><div class="actions"><a class="btn" href="/dashboard/stats">📊 Estadísticas comerciales</a><a class="btn" href="/dashboard/prospects">👥 Prospectos</a><a class="btn" href="/dashboard/content">📝 Contenidos orgánicos</a><a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a><a class="btn" href="/dashboard/export.csv">⬇️ Descargar respaldo CSV</a><a class="btn" href="/dashboard/prospects.csv">⬇️ Descargar prospectos</a></div>']
    if not rows: out.append('<div class="card">No hay conversaciones que coincidan.</div>')
    for r in rows:
        status=html.escape(get_status(r['sender']))
        out.append(f'<div class="card"><a href="/dashboard/{r["sender"]}"><b>📱 {r["sender"]}</b></a><br>Estado: <b>{status}</b><br>{r["total"]} mensajes<br>Última actividad: <b>{html.escape(format_dt(r["last_time"]))}</b></div>')
    return ''.join(out)

@app.get('/dashboard/media/<attachment_id>')
def media(attachment_id):
    if not authorized(request): return login()
    c=db(); row=c.execute('SELECT local_path FROM attachments WHERE id=?',(attachment_id,)).fetchone(); c.close()
    if not row or not row['local_path'] or not os.path.exists(row['local_path']): return 'Archivo no disponible',404
    return send_file(row['local_path'], as_attachment=False)

@app.route('/dashboard/<sender>',methods=['GET','POST'])
def chat(sender):
    if not authorized(request): return login()
    if request.method=='POST' and not same_origin_dashboard_post(request): return Response('Origen no permitido',403)
    if request.method=='POST':
        if request.form.get('status'):
            set_status(sender,request.form.get('status'))
        text=request.form.get('text','').strip()
        if text: send_text(sender,text)
        audio=request.files.get('audio')
        if audio and audio.filename:
            filename=os.path.basename(audio.filename)
            content_type=audio.mimetype or mimetypes.guess_type(filename)[0] or ''
            if not content_type.startswith('audio/'):
                return Response('El archivo debe ser un audio', 400)
            upload_dir=MEDIA_DIR / 'outgoing'
            upload_dir.mkdir(parents=True, exist_ok=True)
            stored=upload_dir / (uuid.uuid4().hex+'_'+filename.replace('/', '_').replace('\\','_'))
            audio.save(stored)
            send_audio(sender, str(stored), filename)
        return redirect('/dashboard/'+sender)
    c=db(); rows=c.execute('SELECT direction,body,created_at,delivery_status,delivery_error FROM messages WHERE sender=? ORDER BY id',(sender,)).fetchall(); attachments=c.execute('SELECT id,type,filename,status,created_at FROM attachments WHERE sender=? ORDER BY created_at',(sender,)).fetchall(); c.close()
    safe_sender=html.escape(sender)
    current_status=html.escape(get_status(sender))
    options=''.join(f'<option {"selected" if x==current_status else ""}>{x}</option>' for x in ('Nuevo','Contactado','Cotización pendiente','Servicio contratado','Cerrado'))
    quick=''.join('<form method="post" style="display:inline"><input type="hidden" name="text" value="'+html.escape(msg,quote=True)+'"><button type="submit">'+html.escape(label)+'</button></form>' for label,msg in QUICK_REPLIES)
    out=[f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chat</title><style>body{{font-family:Arial;margin:24px;max-width:800px}}.in,.out{{padding:10px;margin:8px;border-radius:8px;white-space:pre-wrap}}.in{{background:#eee}}.out{{background:#d9fdd3;text-align:right}}textarea{{width:80%;height:55px}}button{{padding:12px}}.btn{{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin:8px 0}}.quick{{background:#f4f6f8;padding:10px;border-radius:8px;margin:10px 0}}</style><a href="/dashboard">← Conversaciones</a><h2>{safe_sender}</h2><form method="post">Estado: <select name="status">{options}</select> <button>Guardar estado</button></form><a class="btn" href="/dashboard/stats">📊 Estadísticas</a> <a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a><div class="quick"><b>Respuestas rápidas:</b><br>{quick}</div>']
    for r in rows:
        delivery=''
        if r['direction']=='out':
            state=r['delivery_status'] or 'recorded'
            labels={'pending':'Pendiente','accepted_by_meta':'Aceptado por Meta (no confirma entrega)','failed':'Fallido','recorded':'Estado anterior no verificado'}
            delivery='<br><small>Estado: '+html.escape(labels.get(state,state))
            if state=='failed' and r['delivery_error']:
                delivery+=' · '+html.escape(r['delivery_error'][:300])
            delivery+='</small>'
        out.append(f'<div class="{r["direction"]}"><small>{html.escape(format_dt(r["created_at"]))}</small><br>{html.escape(r["body"])}{delivery}</div>')
    if attachments:
        out.append('<div class="quick"><b>Archivos recibidos:</b><ul>')
        for a in attachments:
            label={'audio':'🎧 Audio','image':'🖼️ Imagen','video':'🎬 Video','document':'📄 Documento'}.get(a['type'],a['type'])
            link=f'<a href="/dashboard/media/{a["id"]}" target="_blank">Abrir</a>' if a['status']=='downloaded' else 'No disponible'
            out.append(f'<li>{label} · {html.escape(a["filename"] or "sin nombre")} · {html.escape(format_dt(a["created_at"]))} · {link}</li>')
        out.append('</ul></div>')
    out.append('<form method="post"><textarea name="text" placeholder="Escribir respuesta..."></textarea><button>Enviar</button></form><form method="post" enctype="multipart/form-data" style="margin-top:12px"><label>🎧 Enviar audio: <input type="file" name="audio" accept="audio/*" required></label><button type="submit">Enviar audio</button></form>'); return ''.join(out)

@app.get('/')
def health(): return 'DT Grúas webhook activo',200

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))
