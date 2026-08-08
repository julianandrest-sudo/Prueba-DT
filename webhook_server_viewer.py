import os, json, base64, sqlite3, urllib.request, urllib.error, csv, io, html
from datetime import datetime, timezone
from flask import Flask, request, Response, redirect

app = Flask(__name__)
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN','')
META_ACCESS_TOKEN = os.environ.get('META_ACCESS_TOKEN','')
PHONE_NUMBER_ID = os.environ.get('PHONE_NUMBER_ID','1190081650863434')
GRAPH_API_VERSION = os.environ.get('GRAPH_API_VERSION','v23.0')
DB_PATH = os.environ.get('DB_PATH','conversations.db')
DASHBOARD_USER = os.environ.get('DASHBOARD_USER','admin')
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD','')
conversations = {}
WELCOME = ('¡Hola! Soy el asistente de DT Grúas y Montacargas 🚜\n\n'
           '¿En qué podemos ayudarte?\n'
           '1️⃣ Solicitar cotización de alquiler\n'
           '2️⃣ Mantenimiento o reparación\n'
           '3️⃣ Hablar con un asesor')

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, direction TEXT, body TEXT, created_at TEXT)'); c.commit(); return c

def save(sender,direction,body):
    try:
        c=db(); c.execute('INSERT INTO messages(sender,direction,body,created_at) VALUES(?,?,?,?)',(sender,direction,body,datetime.now(timezone.utc).isoformat())); c.commit(); c.close()
    except Exception as e: print('Error guardando mensaje:',e,flush=True)

def authorized(req):
    h=req.headers.get('Authorization','')
    if not DASHBOARD_PASSWORD or not h.startswith('Basic '): return False
    try:
        u,p=base64.b64decode(h[6:]).decode().split(':',1); return u==DASHBOARD_USER and p==DASHBOARD_PASSWORD
    except Exception: return False

def login(): return Response('Autenticación requerida',401,{'WWW-Authenticate':'Basic realm="DT Gruas"'})

def send_text(to,text):
    save(to,'out',text)
    if not META_ACCESS_TOKEN: return False
    url=f'https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages'
    payload=json.dumps({'messaging_product':'whatsapp','to':to,'type':'text','text':{'preview_url':False,'body':text}}).encode()
    req=urllib.request.Request(url,data=payload,headers={'Authorization':f'Bearer {META_ACCESS_TOKEN}','Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as r: print('Meta',r.status,r.read().decode(),flush=True)
        return True
    except urllib.error.HTTPError as e: print('Error Meta',e.code,e.read().decode(),flush=True)
    except Exception as e: print('Error enviando',e,flush=True)
    return False

def process(sender,text):
    text=(text or '').strip(); low=text.lower(); s=conversations.setdefault(sender,{'step':'menu','data':{}})
    if low in {'hola','buenas','inicio','menu','menú','0','reiniciar'}: s.update(step='menu',data={}); return WELCOME
    if s['step']=='menu':
        if text=='1' or 'cotizar' in low or 'alquiler' in low: s['step']='equipment'; return 'Perfecto. ¿Qué equipo necesitas alquilar y para qué tipo de trabajo?'
        if text=='2' or 'mantenimiento' in low or 'reparación' in low or 'reparacion' in low: s['step']='service'; return 'Cuéntanos qué equipo necesita mantenimiento o reparación y cuál es la falla.'
        if text=='3' or 'asesor' in low or 'persona' in low: s['step']='advisor'; return 'Claro. Déjanos tu nombre y un asesor te contactará lo antes posible.'
        return 'Por favor responde con 1, 2 o 3.\n\n'+WELCOME
    if s['step']=='equipment': s['data']['equipment']=text; s['step']='location'; return '¿En qué ciudad o dirección se utilizaría el montacargas?'
    if s['step']=='location': s['data']['location']=text; s['step']='dates'; return '¿Para qué fecha y por cuánto tiempo lo necesitas?'
    if s['step']=='dates': s['data']['dates']=text; s['step']='contact'; return 'Gracias. ¿Cuál es tu nombre y empresa para preparar la cotización?'
    if s['step']=='contact':
        s['data']['contact']=text; d=s['data']; s['step']='done'
        return f"✅ Recibimos tu solicitud de cotización.\n\nEquipo: {d['equipment']}\nUbicación: {d['location']}\nFecha/duración: {d['dates']}\nCliente: {d['contact']}\n\nUn asesor de DT Grúas y Montacargas revisará la información y te contactará."
    if s['step']=='service': s['data']['service']=text; s['step']='contact'; return '¿Cuál es tu nombre y empresa? Un asesor revisará el caso y te contactará.'
    if s['step']=='advisor': s['data']['contact']=text; s['step']='done'; return 'Gracias. Un asesor de DT Grúas y Montacargas te contactará pronto.'
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
                    sender=m.get('from')
                    if sender and m.get('type')=='text':
                        text=m.get('text',{}).get('body',''); save(sender,'in',text); send_text(sender,process(sender,text))
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
    out=['<!doctype html><meta charset="utf-8"><title>Respaldo de conversaciones - DT Grúas</title><style>body{font-family:Arial;margin:28px}.toolbar{margin-bottom:20px}button{padding:10px 16px}.msg{padding:8px;margin:6px 0;border-radius:6px;white-space:pre-wrap}.in{background:#eee}.out{background:#d9fdd3}</style><div class="toolbar"><button onclick="window.print()">Imprimir / Guardar como PDF</button> <a href="/dashboard">Volver al visor</a></div><h1>Respaldo de conversaciones</h1><p>Generado: '+html.escape(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S'))+'</p>']
    current=None
    for r in rows:
        if r['sender']!=current:
            current=r['sender']; out.append('<hr><h2>Cliente: '+html.escape(r['sender'])+'</h2>')
        cls='in' if r['direction']=='in' else 'out'; label='Entrante' if cls=='in' else 'Saliente'
        out.append(f'<div class="msg {cls}"><b>{label}</b> · {html.escape(r["created_at"])}<br>{html.escape(r["body"])}</div>')
    if not rows: out.append('<p>No hay mensajes todavía.</p>')
    return ''.join(out)

@app.get('/dashboard')
def dashboard():
    if not authorized(request): return login()
    c=db(); rows=c.execute('SELECT sender,MAX(created_at) last_time,COUNT(*) total FROM messages GROUP BY sender ORDER BY last_time DESC').fetchall(); c.close()
    out=['<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>DT Grúas</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}.card{background:white;padding:16px;margin:10px 0;border-radius:8px}a{color:#075e9b}.actions{margin:16px 0}.btn{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin-right:8px}</style><h1>DT Grúas y Montacargas</h1><p>Conversaciones</p><div class="actions"><a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a><a class="btn" href="/dashboard/export.csv">⬇️ Descargar respaldo CSV</a></div>']
    if not rows: out.append('<div class="card">No hay mensajes todavía.</div>')
    for r in rows: out.append(f'<div class="card"><a href="/dashboard/{r["sender"]}"><b>{r["sender"]}</b></a><br>{r["total"]} mensajes · {r["last_time"]}</div>')
    return ''.join(out)

@app.route('/dashboard/<sender>',methods=['GET','POST'])
def chat(sender):
    if not authorized(request): return login()
    if request.method=='POST':
        text=request.form.get('text','').strip()
        if text: send_text(sender,text)
        return redirect('/dashboard/'+sender)
    c=db(); rows=c.execute('SELECT direction,body,created_at FROM messages WHERE sender=? ORDER BY id',(sender,)).fetchall(); c.close()
    safe_sender=html.escape(sender)
    out=[f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chat</title><style>body{{font-family:Arial;margin:24px;max-width:800px}}.in,.out{{padding:10px;margin:8px;border-radius:8px;white-space:pre-wrap}}.in{{background:#eee}}.out{{background:#d9fdd3;text-align:right}}textarea{{width:80%;height:55px}}button{{padding:12px}}.btn{{display:inline-block;background:#075e9b;color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin:8px 0}}</style><a href="/dashboard">← Conversaciones</a><h2>{safe_sender}</h2><a class="btn" href="/dashboard/print">🖨️ Imprimir / Guardar PDF</a>']
    for r in rows: out.append(f'<div class="{r["direction"]}"><small>{html.escape(r["created_at"])}</small><br>{html.escape(r["body"])}</div>')
    out.append('<form method="post"><textarea name="text" placeholder="Escribir respuesta..."></textarea><button>Enviar</button></form>'); return ''.join(out)

@app.get('/')
def health(): return 'DT Grúas webhook activo',200

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',10000)))
