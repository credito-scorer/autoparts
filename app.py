import os
import threading
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from agent.parser import parse_request
from agent.sourcing import source_parts
from agent.recommender import build_options
from agent.approval import send_for_approval, handle_approval, send_whatsapp
from utils.logger import log_request
from connectors.whatsapp_supplier import (
    handle_supplier_response,
    get_registered_suppliers
)

load_dotenv()

app = Flask(__name__)

pending_approvals = {}
pending_selections = {}
approval_message_map = {}

# Maps escalation message SID → customer number
# So when you reply to an escalation, we know who to forward to
escalation_message_map = {}

# Customers currently in a live session (bot is paused for them)
live_sessions = {}

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
                # Send escalation and capture the message SID
                msg_sid = send_whatsapp(
                    owner_number,
                    f"🔴 *Sesión en vivo iniciada*\n"
                    f"Cliente: {incoming_number.replace('whatsapp:', '')}\n"
                    f"Mensaje: \"{incoming_message}\"\n\n"
                    f"_Responde a este mensaje para hablarle directamente. "
                    f"Escribe *fin* para terminar la sesión y devolver el control al bot._"
                )
                # Map the SID → customer number for reply forwarding
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


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_number = request.form.get("From")
    incoming_message = request.form.get("Body", "").strip()
    response = MessagingResponse()

    print(f"\n📨 Message from {incoming_number}: {incoming_message}")

    # 1. YOUR PERSONAL NUMBER → Approval or reply-forwarding flow
    if incoming_number == os.getenv("YOUR_PERSONAL_WHATSAPP"):
        replied_to_sid = request.form.get("OriginalRepliedMessageSid", None)

        # Check if this is a reply to a live session / escalation message
        if replied_to_sid and replied_to_sid in escalation_message_map:
            customer_number = escalation_message_map[replied_to_sid]

            if incoming_message.strip().lower() == "fin":
                # End live session — hand control back to the bot
                live_sessions.pop(customer_number, None)
                send_whatsapp(
                    customer_number,
                    "Gracias por tu paciencia. Si necesitas algo más, "
                    "estamos aquí. 👋\n\n"
                    "Para buscar un repuesto escríbenos:\n"
                    "Pieza + marca + modelo + año"
                )
                display = customer_number.replace("whatsapp:", "")
                print(f"🟢 Live session ended for {customer_number}")
                response.message(f"✅ Sesión terminada. Bot activo para {display}.")
                return str(response)

            # Forward owner's message to customer and keep session alive
            send_whatsapp(
                customer_number,
                f"💬 *AutoParts Santiago:*\n{incoming_message}"
            )
            # Track this new message SID so the owner can keep replying
            escalation_message_map.pop(replied_to_sid, None)
            print(f"📤 Forwarded owner reply to {customer_number}: {incoming_message}")
            response.message("✅ Mensaje enviado al cliente.")
            return str(response)

        # Otherwise handle as normal approval
        result = handle_approval(
            incoming_message,
            pending_approvals,
            pending_selections,
            approval_message_map,
            replied_to_sid
        )
        response.message(result)
        return str(response)

    # 2. WHATSAPP SUPPLIER → Supplier response flow
    registered_suppliers = get_registered_suppliers()
    supplier_numbers = [s["number"] for s in registered_suppliers]

    if incoming_number in supplier_numbers:
        result = handle_supplier_response(incoming_number, incoming_message)
        if result:
            print(f"✅ Supplier response: {result['supplier_name']}")
        return str(response)

    # 3. CUSTOMER IN LIVE SESSION → forward to owner, skip the bot
    if incoming_number in live_sessions:
        owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")
        if owner_number:
            display = incoming_number.replace("whatsapp:", "")
            msg_sid = send_whatsapp(
                owner_number,
                f"💬 *{display}:*\n{incoming_message}"
            )
            if msg_sid:
                escalation_message_map[msg_sid] = incoming_number
                print(f"📨 Forwarded live message from {incoming_number} → owner")
        return str(response)

    # 4. CUSTOMER SELECTING AN OPTION
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
                    os.getenv("YOUR_PERSONAL_WHATSAPP"),
                    f"🎯 *Cliente confirmó opción {choice + 1}*\n"
                    f"Pieza: {parsed.get('part')} "
                    f"{parsed.get('make')} {parsed.get('model')} "
                    f"{parsed.get('year')}\n"
                    f"Precio: ${price}\n"
                    f"Proveedor: {chosen['supplier_name']}\n"
                    f"Entrega: {chosen['lead_time']}\n"
                    f"Cliente: {incoming_number.replace('whatsapp:', '')}"
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
                response.message("Por favor responde con 1, 2 o 3.")

            return str(response)

    # 5. ALL OTHER MESSAGES → process in background
    thread = threading.Thread(
        target=process_customer_request,
        args=(incoming_number, incoming_message)
    )
    thread.daemon = True
    thread.start()

    return str(response)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "running", "service": "AutoParts Trading Co."}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
