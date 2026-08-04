import os
import json
import urllib.request
import urllib.error
from flask import Flask, request

app = Flask(__name__)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "1190081650863434")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v23.0")

# Temporary conversation memory. It is enough for the first tests; later we can
# replace it with a database so conversations survive service restarts.
conversations = {}

WELCOME = (
    "¡Hola! Soy el asistente de DT Grúas y Montacargas 🚜\n\n"
    "¿En qué podemos ayudarte?\n"
    "1️⃣ Solicitar cotización de alquiler\n"
    "2️⃣ Mantenimiento o reparación\n"
    "3️⃣ Hablar con un asesor"
)


def send_text(recipient, text):
    """Send a WhatsApp Cloud API text message when Meta credentials are ready."""
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print(f"[SIMULACIÓN] Para {recipient}: {text}", flush=True)
        return False
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            print(f"Meta respondió {response.status}: {response.read().decode()}", flush=True)
        return True
    except urllib.error.HTTPError as error:
        print(f"Error de Meta {error.code}: {error.read().decode()}", flush=True)
    except Exception as error:
        print(f"Error enviando mensaje: {error}", flush=True)
    return False


def process_message(sender, text):
    text = (text or "").strip()
    lower = text.lower()
    state = conversations.setdefault(sender, {"step": "menu", "data": {}})

    if lower in {"hola", "buenas", "inicio", "menu", "menú", "0", "reiniciar"}:
        state.update(step="menu", data={})
        return WELCOME

    if state["step"] == "menu":
        if text == "1" or "cotizar" in lower or "alquiler" in lower:
            state["step"] = "equipment"
            return "Perfecto. ¿Qué equipo necesitas alquilar y para qué tipo de trabajo?"
        if text == "2" or "mantenimiento" in lower or "reparación" in lower or "reparacion" in lower:
            state["step"] = "service"
            return "Cuéntanos qué equipo necesita mantenimiento o reparación y cuál es la falla."
        if text == "3" or "asesor" in lower or "persona" in lower:
            state["step"] = "advisor"
            return "Claro. Déjanos tu nombre y un asesor te contactará lo antes posible."
        return "Por favor responde con 1, 2 o 3.\n\n" + WELCOME

    if state["step"] == "equipment":
        state["data"]["equipment"] = text
        state["step"] = "location"
        return "¿En qué ciudad o dirección se utilizaría el montacargas?"

    if state["step"] == "location":
        state["data"]["location"] = text
        state["step"] = "dates"
        return "¿Para qué fecha y por cuánto tiempo lo necesitas?"

    if state["step"] == "dates":
        state["data"]["dates"] = text
        state["step"] = "contact"
        return "Gracias. ¿Cuál es tu nombre y empresa para preparar la cotización?"

    if state["step"] == "contact":
        state["data"]["contact"] = text
        details = state["data"]
        state["step"] = "done"
        return (
            "✅ Recibimos tu solicitud de cotización.\n\n"
            f"Equipo: {details['equipment']}\n"
            f"Ubicación: {details['location']}\n"
            f"Fecha/duración: {details['dates']}\n"
            f"Cliente: {details['contact']}\n\n"
            "Un asesor de DT Grúas y Montacargas revisará la información y te contactará."
        )

    if state["step"] == "service":
        state["data"]["service"] = text
        state["step"] = "contact"
        return "¿Cuál es tu nombre y empresa? Un asesor revisará el caso y te contactará."

    if state["step"] == "advisor":
        state["data"]["contact"] = text
        state["step"] = "done"
        return "Gracias. Un asesor de DT Grúas y Montacargas te contactará pronto."

    return "Escribe *hola* para volver al menú principal."


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403


@app.post("/webhook")
def receive_webhook():
    data = request.get_json(silent=True) or {}
    print(json.dumps(data, ensure_ascii=False), flush=True)
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    sender = message.get("from")
                    if sender and message.get("type") == "text":
                        reply = process_message(sender, message.get("text", {}).get("body", ""))
                        send_text(sender, reply)
    except Exception as error:
        print(f"Error procesando webhook: {error}", flush=True)
    return "EVENT_RECEIVED", 200


@app.get("/")
def health():
    return "DT Grúas webhook activo", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
