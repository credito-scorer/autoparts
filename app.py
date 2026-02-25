import os
import threading
from flask import Flask, request, jsonify, make_response
from dotenv import load_dotenv
from agent.parser import parse_request, detect_needs_human
from agent.sourcing import source_parts
from agent.recommender import build_options
from agent.approval import send_for_approval, handle_approval, send_whatsapp
from utils.logger import log_request
from utils.dashboard import render_dashboard
from connectors.whatsapp_supplier import (
    handle_supplier_response,
    get_registered_suppliers
)

load_dotenv()

app = Flask(__name__)

pending_approvals = {}
pending_selections = {}
approval_message_map = {}

# Maps message ID → customer number for reply forwarding
escalation_message_map = {}

# Customers currently in a live session (bot is paused for them)
live_sessions = {}

# Customers who were offered a live session and we're waiting for their confirmation
pending_live_offers = {}

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

VAGUE_INTENT = [
    "si necesito", "sí necesito", "necesito unas", "necesito algo",
    "busco unas", "quiero unas", "quiero una", "quiero un",
    "tengo que buscar", "necesito piezas", "necesito repuestos",
    "necesito varios", "si tengo", "sí tengo", "tengo varios",
    "tengo unas", "no entiendo", "no sé cómo", "no se como",
    "si", "sí"
]

PART_KEYWORDS = [
    "pieza", "repuesto", "parte", "necesito", "neceisto", "nececito",
    "busco", "quiero", "tienen", "tienes", "hay ", "consiguen"
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


def is_secondary_greeting(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(g) for g in SECONDARY_GREETINGS)


def is_wait(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(w) for w in WAIT_PHRASES)


def is_ack(message: str) -> bool:
    msg = message.lower().strip()
    return msg in ACK_PHRASES


def is_thanks(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(t) for t in THANKS_PHRASES)


def is_vague_intent(message: str) -> bool:
    msg = message.lower().strip()
    if any(msg.startswith(v) for v in VAGUE_INTENT):
        return True
    return any(keyword in msg for keyword in PART_KEYWORDS)


def is_human_request(message: str) -> bool:
    msg = message.lower().strip()
    return any(phrase in msg for phrase in HUMAN_REQUEST)


def process_customer_request(incoming_number: str, incoming_message: str):
    parsed = parse_request(incoming_message)

    if not parsed:
        if is_human_request(incoming_message):
            # Flag this customer as in a live session — bot steps aside
            live_sessions[incoming_number] = True
            print(f"🔴 Live session started for {incoming_number}")

            owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")
            if owner_number:
                msg_sid = send_whatsapp(
                    owner_number,
                    f"🔴 *Sesión en vivo iniciada*\n"
                    f"Cliente: {incoming_number}\n"
                    f"Mensaje: \"{incoming_message}\"\n\n"
                    f"_Responde a este mensaje para hablarle directamente. "
                    f"Escribe *fin* para terminar la sesión y devolver el control al bot._"
                )
                if msg_sid:
                    escalation_message_map[msg_sid] = incoming_number
                    print(f"📋 Live session mapped: {msg_sid} → {incoming_number}")

            send_whatsapp(
                incoming_number,
                "Claro, en un momento te contacta alguien del equipo. 👍\n\n"
                "Si mientras tanto quieres buscar una pieza, solo envíanos:\n"
                "Pieza + marca + modelo + año"
            )
        elif is_greeting(incoming_message):
            send_whatsapp(
                incoming_number,
                "👋 Hola! Somos *AutoParts Santiago*.\n\n"
                "Encuentra cualquier repuesto sin salir de tu taller. "
                "Solo envíanos la pieza, marca, modelo y año.\n\n"
                "Ejemplo: *alternador Toyota Hilux 2008*"
            )
        elif is_secondary_greeting(incoming_message):
            send_whatsapp(
                incoming_number,
                "¡Todo bien! ¿En qué te puedo ayudar hoy? 😊"
            )
        elif is_wait(incoming_message):
            send_whatsapp(
                incoming_number,
                "Claro, tómate tu tiempo. Aquí estoy cuando estés listo. 👍"
            )
        elif is_ack(incoming_message):
            send_whatsapp(
                incoming_number,
                "Perfecto. 😊 ¿Hay algo más en que te pueda ayudar?"
            )
        elif is_thanks(incoming_message):
            send_whatsapp(
                incoming_number,
                "¡Con gusto! Si necesitas algo más, aquí estamos. 👋"
            )
        elif is_vague_intent(incoming_message):
            send_whatsapp(
                incoming_number,
                "Con gusto te ayudo. 🔧\n\n"
                "Dime qué pieza necesitas y para qué vehículo:\n"
                "Pieza + marca + modelo + año\n\n"
                "Ejemplo: *filtro de aceite Corolla 2015*"
            )
        elif detect_needs_human(incoming_message):
            pending_live_offers[incoming_number] = True
            send_whatsapp(
                incoming_number,
                "Veo que quizás no te estoy ayudando como deberías. 🙏\n\n"
                "¿Quieres hablar directamente con alguien del equipo?\n"
                "Responde *sí* para conectarte."
            )
        else:
            send_whatsapp(
                incoming_number,
                "No entendí tu mensaje. 🙏\n\n"
                "Para buscar un repuesto envíanos:\n"
                "Pieza + marca + modelo + año\n\n"
                "Ejemplo: *filtro de aceite Corolla 2015*"
            )
        return

    # It's a real part request — acknowledge now
    send_whatsapp(
        incoming_number,
        "🔩 *Recibido!*\n"
        "Estamos buscando tu pieza, te confirmamos en unos minutos. ⏳"
    )

    log_request({
        "customer_number": incoming_number,
        "raw_message": incoming_message,
        "parsed": parsed,
        "status": "received"
    })

    results = source_parts(parsed)

    if not results:
        send_whatsapp(
            incoming_number,
            f"Lo sentimos, no encontramos *{parsed.get('part')}* "
            f"para {parsed.get('make')} {parsed.get('model')} "
            f"{parsed.get('year')} en este momento. 😔\n\n"
            "Te avisamos si conseguimos algo."
        )
        log_request({
            "customer_number": incoming_number,
            "raw_message": incoming_message,
            "parsed": parsed,
            "status": "not_found"
        })
        return

    options = build_options(results, parsed)

    pending_selections[incoming_number] = {
        "options": options,
        "parsed": parsed,
        "final_prices": [opt["suggested_price"] for opt in options]
    }

    send_for_approval(
        options, parsed, incoming_number,
        pending_approvals, approval_message_map
    )

    log_request({
        "customer_number": incoming_number,
        "raw_message": incoming_message,
        "parsed": parsed,
        "options": options,
        "status": "pending_approval"
    })


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Meta webhook verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN"):
        print("✅ Webhook verified by Meta")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    # Parse Meta's nested webhook structure
    try:
        value = data["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return jsonify({"status": "ok"}), 200

    # Skip status updates (delivered, read, sent, etc.)
    if "messages" not in value:
        return jsonify({"status": "ok"}), 200

    message = value["messages"][0]

    # Only handle text messages
    if message.get("type") != "text":
        return jsonify({"status": "ok"}), 200

    # Meta sends numbers without + (e.g. 56912345678) — normalize to +56912345678
    incoming_number = "+" + message["from"]
    incoming_message = message.get("text", {}).get("body", "").strip()
    replied_to_sid = message.get("context", {}).get("id")

    print(f"\n📨 Message from {incoming_number}: {incoming_message}")

    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP", "").replace("whatsapp:", "").replace("+", "").strip()
    owner_number = "+" + owner_number
    incoming_normalized = incoming_number.replace("+", "").strip()

    # 1. OWNER → Approval or reply-forwarding flow
    if incoming_normalized == owner_number.replace("+", ""):

        # Reply to a live session / escalation message
        if replied_to_sid and replied_to_sid in escalation_message_map:
            customer_number = escalation_message_map[replied_to_sid]

            if incoming_message.strip().lower() == "fin":
                live_sessions.pop(customer_number, None)
                send_whatsapp(
                    customer_number,
                    "Gracias por tu paciencia. Si necesitas algo más, "
                    "estamos aquí. 👋\n\n"
                    "Para buscar un repuesto escríbenos:\n"
                    "Pieza + marca + modelo + año"
                )
                print(f"🟢 Live session ended for {customer_number}")
                send_whatsapp(owner_number, f"✅ Sesión terminada. Bot activo para {customer_number}.")
                return jsonify({"status": "ok"}), 200

            send_whatsapp(customer_number, f"💬 *AutoParts Santiago:*\n{incoming_message}")
            escalation_message_map.pop(replied_to_sid, None)
            print(f"📤 Forwarded owner reply to {customer_number}: {incoming_message}")
            send_whatsapp(owner_number, "✅ Mensaje enviado al cliente.")
            return jsonify({"status": "ok"}), 200

        # Manual live session command: "tomar +56912345678"
        if incoming_message.lower().startswith("tomar "):
            parts = incoming_message.strip().split()
            raw_number = parts[1] if len(parts) > 1 else ""
            if not raw_number.startswith("+"):
                raw_number = "+" + raw_number
            live_sessions[raw_number] = True
            send_whatsapp(
                raw_number,
                "Hola, alguien del equipo de AutoParts Santiago se pondrá en "
                "contacto contigo en un momento. 👋"
            )
            print(f"🔴 Manual live session started for {raw_number}")
            send_whatsapp(owner_number, f"🔴 Sesión en vivo iniciada con {raw_number}.")
            return jsonify({"status": "ok"}), 200

        # Normal approval handling
        result = handle_approval(
            incoming_message,
            pending_approvals,
            pending_selections,
            approval_message_map,
            replied_to_sid
        )
        send_whatsapp(owner_number, result)
        return jsonify({"status": "ok"}), 200

    # 2. SUPPLIER → Supplier response flow
    registered_suppliers = get_registered_suppliers()
    supplier_numbers = [s["number"] for s in registered_suppliers]

    if incoming_number in supplier_numbers:
        result = handle_supplier_response(incoming_number, incoming_message)
        if result:
            print(f"✅ Supplier response: {result['supplier_name']}")
        return jsonify({"status": "ok"}), 200

    # 3. PENDING LIVE OFFER → customer responding to live session offer
    if incoming_number in pending_live_offers:
        pending_live_offers.pop(incoming_number)
        affirmative = incoming_message.strip().lower() in [
            "sí", "si", "yes", "dale", "ok", "okey", "sip", "claro", "bueno", "va"
        ]
        if affirmative:
            live_sessions[incoming_number] = True
            if owner_number:
                msg_sid = send_whatsapp(
                    owner_number,
                    f"🔴 *Sesión en vivo iniciada*\n"
                    f"Cliente: {incoming_number}\n\n"
                    f"_El cliente aceptó conectarse con el equipo. "
                    f"Escribe *fin* para terminar la sesión._"
                )
                if msg_sid:
                    escalation_message_map[msg_sid] = incoming_number
            send_whatsapp(
                incoming_number,
                "Perfecto, en un momento te contacta alguien del equipo. 👍"
            )
        else:
            send_whatsapp(
                incoming_number,
                "Entendido. 😊 Si necesitas algo más, aquí estamos.\n\n"
                "Para buscar un repuesto: Pieza + marca + modelo + año"
            )
        return jsonify({"status": "ok"}), 200

    # 4. LIVE SESSION → forward to owner, skip the bot

    if incoming_number in live_sessions:
        if owner_number:
            msg_sid = send_whatsapp(
                owner_number,
                f"💬 *{incoming_number}:*\n{incoming_message}"
            )
            if msg_sid:
                escalation_message_map[msg_sid] = incoming_number
                print(f"📨 Forwarded live message from {incoming_number} → owner")
        return jsonify({"status": "ok"}), 200

    # 5. CUSTOMER SELECTING AN OPTION
    if incoming_number in pending_selections:
        if incoming_message.strip() in ["1", "2", "3"]:
            pending = pending_selections.get(incoming_number)
            choice = int(incoming_message.strip()) - 1
            options = pending["options"]
            final_prices = pending["final_prices"]
            parsed = pending["parsed"]

            if choice < len(options):
                chosen = options[choice]
                price = final_prices[choice]

                send_whatsapp(
                    incoming_number,
                    f"✅ *Perfecto!* Confirmado.\n\n"
                    f"🔩 {parsed.get('part')} — "
                    f"{parsed.get('make')} {parsed.get('model')} "
                    f"{parsed.get('year')}\n"
                    f"💵 Precio: *${price}*\n"
                    f"🚚 Entrega: {chosen['lead_time']}\n\n"
                    f"Te contactamos para coordinar la entrega. 🙌"
                )

                send_whatsapp(
                    owner_number,
                    f"🎯 *Cliente confirmó opción {choice + 1}*\n"
                    f"Pieza: {parsed.get('part')} "
                    f"{parsed.get('make')} {parsed.get('model')} "
                    f"{parsed.get('year')}\n"
                    f"Precio: ${price}\n"
                    f"Proveedor: {chosen['supplier_name']}\n"
                    f"Entrega: {chosen['lead_time']}\n"
                    f"Cliente: {incoming_number}"
                )

                log_request({
                    "customer_number": incoming_number,
                    "raw_message": incoming_message,
                    "parsed": parsed,
                    "options": options,
                    "final_prices": final_prices,
                    "chosen_option": choice + 1,
                    "status": "confirmed"
                })

                del pending_selections[incoming_number]
            else:
                send_whatsapp(incoming_number, "Por favor responde con 1, 2 o 3.")

        return jsonify({"status": "ok"}), 200

    # 6. ALL OTHER MESSAGES → process in background
    thread = threading.Thread(
        target=process_customer_request,
        args=(incoming_number, incoming_message)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"status": "ok"}), 200




@app.route("/dashboard", methods=["GET"])
def dashboard():
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if request.args.get("key") != password:
        return make_response("Unauthorized", 401)
    return make_response(render_dashboard(), 200)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "running", "service": "AutoParts Trading Co."}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
