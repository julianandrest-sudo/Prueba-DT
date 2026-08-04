import os
from flask import Flask, request

app = Flask(__name__)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")

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
    print(data, flush=True)
    return "EVENT_RECEIVED", 200

@app.get("/")
def health():
    return "DT Grúas webhook activo", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
