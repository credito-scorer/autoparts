import os
import json
import threading
import anthropic
from flask import Flask, request
from dotenv import load_dotenv

# ── DORMANT VERTICAL IMPORTS (kept for future use) ───────────────
# from agent.parser import parse_request
# from agent.sourcing import source_parts
# from agent.recommender import build_options
# from agent.approval import send_for_approval, handle_approval
# from connectors.whatsapp_supplier import handle_supplier_response, get_registered_suppliers
# from utils.logger import log_request

load_dotenv()

app = Flask(__name__)

# ── WHATSAPP SENDER ───────────────────────────────────────────────
# Carried forward from auto parts vertical — core utility
import requests as http_requests

def send_whatsapp(to: str, message: str) -> str | None:
    token = os.getenv("META_ACCESS_TOKEN")
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        res = http_requests.post(url, json=payload, headers=headers)
        data = res.json()
        msg_id = data.get("messages", [{}])[0].get("id")
        print(f"📤 Sent to {to}: {message[:60]}...")
        return msg_id
    except Exception as e:
        print(f"❌ send_whatsapp error: {e}")
        return None


# ── CLIENT REGISTRY ───────────────────────────────────────────────
# Maps Meta Phone Number ID → client config.
# Zeli's own number runs the demo/sales bot.
# Add real clients below as they onboard.

CLIENTS = {
    os.getenv("META_PHONE_NUMBER_ID"): {
        "name": "Zeli",
        "mode": "demo",
        "escalation_number": os.getenv("YOUR_PERSONAL_WHATSAPP"),
        "knowledge_base": """Eres Zeli, una asistente de ventas inteligente de Zeli Technologies.

Zeli Technologies ofrece bots de WhatsApp con inteligencia artificial para negocios en Panamá. Atención al cliente 24/7, respuestas instantáneas y precisas, escalación inteligente al dueño solo cuando es necesario.

Lo que ofrecemos:
- Bot de WhatsApp con IA entrenado en el negocio del cliente
- Responde preguntas de productos, servicios, precios, horarios
- Escala al dueño cuando el cliente lo pide o cuando la situación lo requiere
- Mismo número de WhatsApp del negocio — los clientes no notan ningún cambio
- Configuración completa por parte de Zeli — el dueño no tiene que hacer nada técnico

Precio: desde $150/mes. Setup único de $200-300.

Proceso: el negocio nos contacta → nosotros configuramos todo en pocos días → el bot entra en vivo → sus clientes reciben atención 24/7 desde el mismo número de siempre.

Tu trabajo en esta conversación es dos cosas: primero, demostrar con esta misma interacción lo que un bot Zeli puede hacer — sé útil, rápido, inteligente, natural. Segundo, responder cualquier pregunta sobre Zeli con claridad y confianza.

Si alguien muestra interés en contratar, anímalo. Si alguien pregunta cómo funciona, explícalo con entusiasmo. Si alguien es escéptico, entiéndelo y responde con honestidad.

Habla siempre en español panameño, de forma natural y profesional. Sin emojis excesivos. Sin respuestas robóticas.""",
        "interest_triggers": [
            "me interesa", "quiero esto", "cómo me registro",
            "cómo me apunto", "quiero el bot", "quiero contratar",
            "me apunto", "vamos", "dale", "cómo empezamos",
            "quiero empezar", "cuánto cuesta", "cómo funciona",
            "quiero más información", "me pueden llamar",
            "quiero que me contacten"
        ]
    }
}

# ── STATE ─────────────────────────────────────────────────────────
# Carried forward pattern from auto parts escalation flow
escalation_message_map = {}       # msg_sid → prospect number (for owner reply forwarding)
live_mode_numbers = set()         # prospects already handed off — bot stays silent
conversation_history = {}         # phone_number → list of message turns


# ── PHRASE DETECTORS ──────────────────────────────────────────────
# Carried forward from auto parts vertical — reusable across all clients

GREETINGS = ["hola", "buenas", "buenos dias", "buenos días", "buenas tardes",
             "buenas noches", "hi", "hello", "hey"]

SECONDARY_GREETINGS = ["que tal", "qué tal", "como estas", "cómo estás",
                       "como estás", "cómo estas", "todo bien", "que hay"]

WAIT_PHRASES = [
    "dame un segundo", "un momento", "un seg", "espera", "espérate",
    "ahorita te digo", "ahorita", "déjame revisar", "dejame revisar",
    "déjame ver", "dejame ver", "ya vuelvo", "un momentito"
]

ACK_PHRASES = [
    "ok", "okey", "okay", "entendido", "perfecto", "listo", "bueno",
    "ah ok", "ah okey", "ya veo", "ya", "claro", "dale", "va",
    "de acuerdo", "10 puntos", "excelente", "genial"
]

THANKS_PHRASES = [
    "gracias", "muchas gracias", "mil gracias", "ok gracias",
    "okey gracias", "gracias!", "gracias!!", "ty", "thanks"
]

HUMAN_REQUEST = [
    "con alguien", "hablar con", "un agente", "una persona", "con una persona",
    "con un humano", "con el dueño", "con el encargado", "me pueden llamar",
    "me pueden contactar", "quiero hablar", "necesito hablar", "llamenme",
    "llámenme", "me llaman", "por favor alguien", "alguien me ayude",
    "alguien que trabaje"
]


def is_greeting(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(g) for g in GREETINGS)

def is_wait(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(w) for w in WAIT_PHRASES)

def is_ack(message: str) -> bool:
    msg = message.lower().strip()
    return msg in ACK_PHRASES

def is_thanks(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(t) for t in THANKS_PHRASES)

def is_human_request(message: str) -> bool:
    msg = message.lower().strip()
    return any(phrase in msg for phrase in HUMAN_REQUEST)


# ── DORMANT VERTICAL FLOWS ────────────────────────────────────────
# Auto parts customer request handler — kept dormant, do not delete
# def process_customer_request(incoming_number, incoming_message):
#     parsed = parse_request(incoming_message)
#     ... (full auto parts flow preserved in app_autoparts_backup.py)

# Real estate qualifier flow — kept dormant
# def process_real_estate_lead(incoming_number, incoming_message):
#     ... (real estate flow preserved in app_autoparts_backup.py)

# Product aggregation live mode — kept dormant
# GLOBAL_LIVE_MODE was used here to bypass routing entirely
# Same pattern now used in live_mode_numbers set above


# ── CORE MESSAGE HANDLER ──────────────────────────────────────────

def process_message(phone_number_id: str, incoming_number: str, incoming_message: str):
    client = CLIENTS.get(phone_number_id)
    if not client:
        print(f"⚠️ No client config for phone_number_id: {phone_number_id}")
        return

    msg_lower = incoming_message.lower().strip()

    # Check human request first — always escalate regardless of triggers
    if is_human_request(incoming_message):
        _escalate(client, incoming_number, incoming_message, reason="human_request")
        return

    # Check interest triggers
    if any(trigger in msg_lower for trigger in client.get("interest_triggers", [])):
        _escalate(client, incoming_number, incoming_message, reason="interest")
        return

    # Build conversation history (keep last 10 turns for context)
    history = conversation_history.get(incoming_number, [])
    history.append({"role": "user", "content": incoming_message})

    # Call Claude
    try:
        claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=client["knowledge_base"],
            messages=history[-10:]
        )
        reply = response.content[0].text
    except Exception as e:
        print(f"❌ Claude error: {e}")
        reply = "Disculpa, tuve un problema técnico. Intenta de nuevo en un momento."

    history.append({"role": "assistant", "content": reply})
    conversation_history[incoming_number] = history

    send_whatsapp(incoming_number, reply)


def _escalate(client: dict, incoming_number: str, incoming_message: str, reason: str):
    """Carried forward from auto parts approval flow — generalized for any client."""
    escalation_number = client.get("escalation_number")
    if not escalation_number:
        print(f"⚠️ No escalation number configured for {client['name']}")
        return

    label = "🔥 *Prospecto interesado*" if reason == "interest" else "⚠️ *Cliente pidió hablar con alguien*"

    msg_sid = send_whatsapp(
        escalation_number,
        f"{label} — {client['name']}\n"
        f"Número: {incoming_number}\n"
        f"Mensaje: \"{incoming_message}\"\n\n"
        f"_Responde aquí para hablarle directamente._"
    )

    if msg_sid:
        escalation_message_map[msg_sid] = incoming_number
        live_mode_numbers.add(incoming_number)
        print(f"📋 Escalation mapped: {msg_sid} → {incoming_number} (reason: {reason})")

    if reason == "interest":
        send_whatsapp(
            incoming_number,
            "¡Perfecto! 🙌 Alguien de Zeli te contacta ahora mismo.\n\n"
            "En unos minutos te escribimos para coordinar todo."
        )
    else:
        send_whatsapp(
            incoming_number,
            "Claro, en un momento te contacta alguien del equipo. 👍"
        )


# ── WEBHOOK ───────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN"):
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return "ok", 200

    try:
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]
        phone_number_id = change["metadata"]["phone_number_id"]

        if "messages" not in change:
            return "ok", 200

        msg = change["messages"][0]
        incoming_number = msg["from"]

        # Handle text messages only for now
        if msg.get("type") != "text":
            return "ok", 200

        incoming_message = msg["text"]["body"].strip()
        if not incoming_message:
            return "ok", 200

        print(f"\n📨 [{phone_number_id}] From {incoming_number}: {incoming_message}")

        # 1. OWNER REPLY → forward to prospect in live mode
        owner = os.getenv("YOUR_PERSONAL_WHATSAPP", "").replace("+", "")
        if incoming_number == owner:
            # Check if this is a reply to an escalation notification
            # (reply forwarding via message context — handled by Meta thread)
            context = msg.get("context", {})
            replied_to_id = context.get("id")
            if replied_to_id and replied_to_id in escalation_message_map:
                prospect_number = escalation_message_map[replied_to_id]
                send_whatsapp(prospect_number, incoming_message)
                print(f"📤 Forwarded owner reply to {prospect_number}")
            return "ok", 200

        # 2. LIVE MODE → prospect already handed off, bot stays silent
        if incoming_number in live_mode_numbers:
            print(f"🔕 Live mode active for {incoming_number} — bot silent")
            return "ok", 200

        # 3. ROUTE TO CLIENT BOT
        thread = threading.Thread(
            target=process_message,
            args=(phone_number_id, incoming_number, incoming_message)
        )
        thread.daemon = True
        thread.start()

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        import traceback
        traceback.print_exc()

    return "ok", 200


# ── HEALTH ────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "running",
        "service": "Zeli Customer Service MVP",
        "clients": list(CLIENTS.keys())
    }, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
