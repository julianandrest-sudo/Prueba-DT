import os, json, base64, sqlite3, urllib.request, urllib.error, csv, io, html, time, uuid, mimetypes, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, Response, redirect, send_file
from pathlib import Path
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

app = Flask(__name__)
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN','')
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

class PGConnection:
    def __init__(self, url):
        self.conn=psycopg2.connect(url, connect_timeout=10)
        self.cur=self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def execute(self, query, params=()):
        self.cur.execute(query.replace('?', '%s'), params)
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
        c.execute('CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, sender TEXT NOT NULL, direction TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS conversation_meta (sender TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT \'Nuevo\')')
        c.execute('CREATE TABLE IF NOT EXISTS admin_alerts (id TEXT PRIMARY KEY, sender TEXT, body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, sent_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, sender TEXT NOT NULL, media_id TEXT, type TEXT NOT NULL, filename TEXT, local_path TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS prospects (id TEXT PRIMARY KEY, sender TEXT NOT NULL, contact TEXT, service TEXT, campaign TEXT, source TEXT, priority TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'Nuevo\', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        c.commit(); return c
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, direction TEXT, body TEXT, created_at TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS conversation_meta (sender TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT \'Nuevo\')')
    c.execute('CREATE TABLE IF NOT EXISTS admin_alerts (id TEXT PRIMARY KEY, sender TEXT, body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, sent_at TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, sender TEXT NOT NULL, media_id TEXT, type TEXT NOT NULL, filename TEXT, local_path TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS prospects (id TEXT PRIMARY KEY, sender TEXT NOT NULL, contact TEXT, service TEXT, campaign TEXT, source TEXT, priority TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'Nuevo\', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    c.commit(); return c

def save(sender,direction,body):
    try:
        c=db(); c.execute('INSERT INTO messages(sender,direction,body,created_at) VALUES(?,?,?,?)',(sender,direction,body,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
    except Exception as e: print('Error guardando mensaje:',e,flush=True)

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
        u,p=base64.b64decode(h[6:]).decode().split(':',1); return u==DASHBOARD_USER and p==DASHBOARD_PASSWORD
    except Exception: return False

def login(): return Response('Autenticación requerida',401,{'WWW-Authenticate':'Basic realm="DT Gruas"'})

def save_attachment(sender, media_id, kind, filename=''):
    attachment_id=uuid.uuid4().hex
    created=datetime.now(timezone.utc).isoformat()
    local_path=''; status='pending'; error=''
    try:
        if not META_ACCESS_TOKEN or not media_id: raise RuntimeError('Falta token o ID multimedia')
        headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}'}
        info_req=urllib.request.Request(f'https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}',headers=headers)
        with urllib.request.urlopen(info_req,timeout=15) as r: info=json.loads(r.read().decode())
        media_url=info.get('url')
        if not media_url: raise RuntimeError('Meta no devolvió URL del archivo')
        file_req=urllib.request.Request(media_url,headers=headers)
        ext={'image':'jpg','video':'mp4','audio':'ogg','document':'bin'}.get(kind,'bin')
        safe_name=filename.replace('/','_').replace('\\\\','_') if filename else f'{attachment_id}.{ext}'
        target=MEDIA_DIR / f'{attachment_id}_{safe_name}'
        with urllib.request.urlopen(file_req,timeout=30) as r: target.write_bytes(r.read())
        local_path=str(target); status='downloaded'
    except Exception as e:
        error=str(e)[:500]; status='failed'
        print('Error descargando adjunto:',error,flush=True)
    try:
        c=db(); c.execute('INSERT INTO attachments(id,sender,media_id,type,filename,local_path,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(attachment_id,sender,media_id,kind,filename,local_path,status,error,created)); c.commit(); c.close()
    except Exception as e: print('Error registrando adjunto:',e,flush=True)
    return attachment_id, status, local_path

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
    try:
        media_id=upload_media(filepath, content_type)
        url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
        payload=json.dumps({'messaging_product':'whatsapp','to':to,'type':'audio','audio':{'id':media_id}}).encode()
        req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req, timeout=15) as r: response=r.read().decode()
        save(to,'out',f'🎧 Audio enviado: {filename}')
        log_outgoing_attachment(to,'audio',filename,filepath,media_id,'sent','')
        print('Audio enviado',response,flush=True)
        return True
    except urllib.error.HTTPError as e:
        error=f'HTTP {e.code}: {e.read().decode(errors="replace")[:500]}'
    except Exception as e: error=str(e)[:500]
    save(to,'out',f'🎧 Audio no enviado: {filename}')
    log_outgoing_attachment(to,'audio',filename,filepath,'','failed',error)
    print('Error enviando audio:',error,flush=True)
    return False

def send_text(to,text):
    save(to,'out',text)
    if not META_ACCESS_TOKEN: return False
    url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
    recipient={'recipient':to} if isinstance(to,str) and '.' in to else {'to':to}
    payload=json.dumps({'messaging_product':'whatsapp','recipient_type':'individual',**recipient,'type':'text','text':{'preview_url':False,'body':text}}).encode()
    req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as r: print('Meta',r.status,r.read().decode(),flush=True)
        return True
    except urllib.error.HTTPError as e: print('Error Meta',e.code,e.read().decode(),flush=True)
    except Exception as e: print('Error enviando',e,flush=True)
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

def save_prospect(sender, data):
    now=datetime.now(timezone.utc).isoformat()
    details=json.dumps(data, ensure_ascii=False)
    try:
        c=db(); c.execute('''INSERT INTO prospects(id,sender,contact,service,campaign,source,priority,details,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(uuid.uuid4().hex,sender,data.get('contact',''),data.get('service','Por definir'),data.get('campaign',''),data.get('source','WhatsApp'),prospect_priority(details, True),details,'Nuevo',now,now)); c.commit(); c.close()
    except Exception as e: print('Error guardando prospecto:',e,flush=True)

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
        save_prospect(sender, d); d['_prospect_saved']=True
    return f"✅ Gracias por la información.\n\n{detail}\nCliente: {d['contact']}{attached}{extra}\n\nYa un asesor de DT Grúas y Montacargas te atenderá."

def process(sender,text):
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
            s['step']='dates'; return '¿Para qué fecha y hora necesitas el servicio?'
        s['step']='work'
        return '¿Qué tipo de trabajo realizarás y en qué ciudad o dirección?'
    if s['step']=='work':
        s['data']['work_location']=text
        if s['data'].get('service')=='Alquiler de grúa tipo planchón':
            s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto? Puedes enviar una foto del vehículo o equipo si aplica.'
        s['step']='dates'; return '¿Para qué fecha inicia y por cuánto tiempo necesitas el alquiler? ¿Requieres operador?'
    if s['step']=='dates':
        s['data']['dates_operator']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='attachments_choice':
        if low in {'si','sí','s'}:
            s['step']='attachments'; return 'Envía la foto, video o documento que desees adjuntar.'
        if low in {'no','n'}:
            return finish_request(s, sender)
        return 'Por favor responde SI o NO.\n\n¿Deseas adjuntar fotos, videos o documentos?'
    if s['step']=='attachments':
        return 'Archivo recibido. ¿Deseas agregar alguna información adicional? Responde SI o NO.'
    if s['step']=='additional_choice':
        if low in {'si','sí','s'}:
            s['step']='additional_info'; return 'Escribe la información adicional que deseas agregar.'
        if low in {'no','n'}:
            return finish_request(s, sender)
        return 'Por favor responde SI o NO. ¿Deseas agregar alguna información adicional?'
    if s['step']=='additional_info':
        s['data']['additional_info']=text; return finish_request(s, sender)
    if s['step']=='contact':
        s['data']['contact']=text; s['step']='attachments_choice'
        return '¿Deseas adjuntar fotos, videos o documentos para complementar tu solicitud? Responde SI o NO.'
    if s['step']=='service':
        s['data']['service_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='sale':
        s['data']['sale_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    if s['step']=='visit':
        s['data']['visit_details']=text; s['step']='contact'; return '¿Cuál es tu nombre, empresa y teléfono de contacto?'
    return 'Escribe hola para volver al menú principal.'

@app.get('/webhook')
def verify():
    if request.args.get('hub.mode')=='subscribe' and request.args.get('hub.verify_token')==VERIFY_TOKEN: return request.args.get('hub.challenge',''),200
    return 'Forbidden',403

@app.post('/webhook')
def webhook():
    data=request.get_json(silent=True) or {}; print(json.dumps(data,ensure_ascii=False),flush=True)
    try:
        for entry in data.get('entry',[]):
            for change in entry.get('changes',[]):
                for m in change.get('value',{}).get('messages',[]):
                    sender=m.get('from') or m.get('from_user_id')
                    if sender and m.get('type') in ('text','image','video','document','audio'):
                        kind=m.get('type')
                        if kind=='text':
                            text=m.get('text',{}).get('body','')
                            reply=process(sender,text)
                        else:
                            media=m.get(kind,{}) or {}; filename=media.get('filename',''); media_id=media.get('id','')
                            text=f'📎 Archivo recibido: {kind}'+(f' ({filename})' if filename else '')
                            state=conversations.setdefault(sender,{'step':'menu','data':{}})
                            state.setdefault('data',{}).setdefault('attachments',[]).append({'type':kind,'id':media_id,'filename':filename})
                            attachment_record, attachment_status, attachment_path=save_attachment(sender,media_id,kind,filename)
                            if state.get('step') in ('attachments','additional_choice','additional_info'):
                                state['step']='additional_choice'
                                reply='Recibimos el archivo correctamente 📎. ¿Deseas agregar alguna información adicional? Responde SI o NO.'
                            else:
                                reply='Recibimos el archivo correctamente 📎. ¿Deseas agregar alguna información adicional? Responde SI o NO.'
                                state['step']='additional_choice'
                        if 'En cualquier momento escribe MENU' not in reply:
                            reply += '\n\n↩️ Menú principal: escribe 0 o MENU PRINCIPAL.'
                        save(sender,'in',text); send_text(sender,reply)
                        state=conversations.get(sender,{})
                        urgent=any(x in text.lower() for x in ('urgente','ya','hoy','parado','no funciona','emergencia'))
                        qualified=state.get('step') in ('done','advisor')
                        if sender != ADMIN_PHONE:
                            service=state.get('data',{}).get('service','Por definir')
                            received_at=format_dt(datetime.now(timezone.utc).isoformat())
                            reason=('Solicitud urgente' if urgent else ('Solicitud completada' if qualified else 'Nuevo contacto'))
                            type_label={'text':'Texto','image':'Imagen','video':'Video','document':'Documento','audio':'Audio'}.get(kind,kind)
                            alert=('🔔 NUEVA SOLICITUD — DT GRÚAS\\n\\n'
                                   +'📅 Fecha y hora: '+received_at+'\\n'
                                   +'📱 Cliente: '+sender+'\\n'
                                   +'🛠️ Servicio: '+service+'\\n'
                                   +'📎 Tipo de mensaje: '+type_label+'\\n'
                                   +'📌 Estado: '+reason+'\\n'
                                   +'💬 Mensaje recibido:\\n'+text+'\\n\\n'
                                   +'🔎 Revisa la conversación en el visor: https://dt-gruas-webhook.onrender.com/dashboard')
                            send_admin_alert(alert, sender)
    except Exception as e: print('Error procesando:',e,flush=True)
    return 'EVENT_RECEIVED',200

@app.get('/dashboard/export.csv')
def export_csv():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT id,sender,direction,body,created_at FROM messages ORDER BY id').fetchall(); c.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(['ID','Cliente','Dirección','Mensaje','Fecha y hora'])
    for r in rows: writer.writerow([r['id'],r['sender'],'Entrante' if r['direction']=='in' else 'Saliente',r['body'],r['created_at']])
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
    c=db(); rows=c.execute('SELECT id,sender,contact,service,campaign,source,priority,status,details,created_at,updated_at FROM prospects ORDER BY created_at DESC').fetchall(); c.close()
    buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(['ID','Teléfono','Contacto','Servicio','Campaña','Fuente','Prioridad','Estado','Detalles','Creado','Actualizado'])
    for r in rows: writer.writerow([r['id'],r['sender'],r['contact'],r['service'],r['campaign'],r['source'],r['priority'],r['status'],r['details'],r['created_at'],r['updated_at']])
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=dt_gruas_prospectos.csv'})

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
    out=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>DT Grúas</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}.card{background:white;padding:16px;margin:10px 0;border-radius:8px}a{color:#075e9b}.actions{margin:16px 0}.btn{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin-right:8px}input,select,button{padding:9px;margin:4px}</style><h1>DT Grúas y Montacargas</h1><p>Conversaciones</p><form method="get"><input name="q" placeholder="Buscar por teléfono" value="'+q_safe+'"><select name="status">'+opts+'</select><button>Filtrar</button> <a href="/dashboard">Limpiar</a></form><div class="actions"><a class="btn" href="/dashboard/stats">📊 Estadísticas comerciales</a><a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a><a class="btn" href="/dashboard/export.csv">⬇️ Descargar respaldo CSV</a><a class="btn" href="/dashboard/prospects.csv">⬇️ Descargar prospectos</a></div>']
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
    c=db(); rows=c.execute('SELECT direction,body,created_at FROM messages WHERE sender=? ORDER BY id',(sender,)).fetchall(); attachments=c.execute('SELECT id,type,filename,status,created_at FROM attachments WHERE sender=? ORDER BY created_at',(sender,)).fetchall(); c.close()
    safe_sender=html.escape(sender)
    current_status=html.escape(get_status(sender))
    options=''.join(f'<option {"selected" if x==current_status else ""}>{x}</option>' for x in ('Nuevo','Contactado','Cotización pendiente','Servicio contratado','Cerrado'))
    quick=''.join('<form method="post" style="display:inline"><input type="hidden" name="text" value="'+html.escape(msg,quote=True)+'"><button type="submit">'+html.escape(label)+'</button></form>' for label,msg in QUICK_REPLIES)
    out=[f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chat</title><style>body{{font-family:Arial;margin:24px;max-width:800px}}.in,.out{{padding:10px;margin:8px;border-radius:8px;white-space:pre-wrap}}.in{{background:#eee}}.out{{background:#d9fdd3;text-align:right}}textarea{{width:80%;height:55px}}button{{padding:12px}}.btn{{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin:8px 0}}.quick{{background:#f4f6f8;padding:10px;border-radius:8px;margin:10px 0}}</style><a href="/dashboard">← Conversaciones</a><h2>{safe_sender}</h2><form method="post">Estado: <select name="status">{options}</select> <button>Guardar estado</button></form><a class="btn" href="/dashboard/stats">📊 Estadísticas</a> <a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a><div class="quick"><b>Respuestas rápidas:</b><br>{quick}</div>']
    for r in rows: out.append(f'<div class="{r["direction"]}"><small>{html.escape(format_dt(r["created_at"]))}</small><br>{html.escape(r["body"])}</div>')
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
