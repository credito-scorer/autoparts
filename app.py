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

    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")

    # 1. OWNER → Approval or reply-forwarding flow
    if incoming_number == owner_number:

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


@app.route("/privacy", methods=["GET"])
def privacy():
    html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Privacy Policy — AutoParts Santiago</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f9f9f9; color: #333; line-height: 1.7;
        }
        header {
            background: #1a1a2e; color: white;
            padding: 40px 24px; text-align: center;
        }
        header h1 { font-size: 1.6rem; margin-bottom: 6px; }
        header p { color: #aaa; font-size: 0.9rem; }
        .container {
            max-width: 800px; margin: 40px auto; padding: 0 24px 60px;
        }
        .lang-section {
            background: white; border-radius: 12px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
            padding: 36px 40px; margin-bottom: 32px;
        }
        .lang-label {
            display: inline-block; font-size: 0.75rem; font-weight: 700;
            letter-spacing: .08em; text-transform: uppercase;
            background: #1a1a2e; color: white;
            padding: 3px 12px; border-radius: 20px; margin-bottom: 20px;
        }
        h2 { font-size: 1.3rem; margin-bottom: 20px; color: #1a1a2e; }
        h3 { font-size: 1rem; font-weight: 700; margin: 24px 0 8px; color: #333; }
        p { margin-bottom: 12px; font-size: 0.95rem; color: #444; }
        ul { margin: 8px 0 12px 20px; }
        li { margin-bottom: 6px; font-size: 0.95rem; color: #444; }
        a { color: #1a1a2e; }
        .contact-box {
            background: #f0f2f5; border-radius: 8px;
            padding: 16px 20px; margin-top: 24px; font-size: 0.9rem;
        }
        .contact-box strong { display: block; margin-bottom: 4px; }
        footer {
            text-align: center; color: #aaa;
            font-size: 0.8rem; padding-bottom: 40px;
        }
    </style>
</head>
<body>

<header>
    <h1>AutoParts Santiago</h1>
    <p>Privacy Policy &nbsp;·&nbsp; Política de Privacidad</p>
    <p style="margin-top:8px">Santiago, Veraguas, Panama &nbsp;·&nbsp; Last updated: 2026</p>
</header>

<div class="container">

    <!-- SPANISH -->
    <div class="lang-section">
        <span class="lang-label">Español</span>
        <h2>Política de Privacidad</h2>

        <h3>1. Información que recopilamos</h3>
        <p>Cuando interactúas con nuestro asistente de WhatsApp, recopilamos:</p>
        <ul>
            <li>Tu número de teléfono de WhatsApp</li>
            <li>El contenido de los mensajes que nos envías</li>
        </ul>

        <h3>2. Cómo usamos tu información</h3>
        <p>La información recopilada se utiliza exclusivamente para:</p>
        <ul>
            <li>Procesar y responder a tus consultas de repuestos automotrices</li>
            <li>Coordinarte con el equipo de AutoParts Santiago</li>
            <li>Mejorar la calidad del servicio</li>
        </ul>

        <h3>3. Compartición de datos</h3>
        <p>No vendemos, alquilamos ni compartimos tu información personal con terceros con fines comerciales. Los datos solo se comparten internamente con el equipo de AutoParts Santiago para atender tu solicitud.</p>

        <h3>4. Almacenamiento y seguridad</h3>
        <p>Tus datos se almacenan de forma segura y únicamente durante el tiempo necesario para completar tu solicitud. Aplicamos medidas razonables para proteger tu información contra accesos no autorizados.</p>

        <h3>5. Tus derechos</h3>
        <p>Tienes derecho a solicitar la eliminación de tus datos en cualquier momento. Para hacerlo, contáctanos por correo electrónico.</p>

        <h3>6. Contacto</h3>
        <div class="contact-box">
            <strong>AutoParts Santiago</strong>
            Santiago, Veraguas, Panamá<br>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a>
        </div>
    </div>

    <!-- ENGLISH -->
    <div class="lang-section">
        <span class="lang-label">English</span>
        <h2>Privacy Policy</h2>

        <h3>1. Information We Collect</h3>
        <p>When you interact with our WhatsApp assistant, we collect:</p>
        <ul>
            <li>Your WhatsApp phone number</li>
            <li>The content of messages you send us</li>
        </ul>

        <h3>2. How We Use Your Information</h3>
        <p>The information collected is used exclusively to:</p>
        <ul>
            <li>Process and respond to your auto parts inquiries</li>
            <li>Coordinate with the AutoParts Santiago team</li>
            <li>Improve the quality of our service</li>
        </ul>

        <h3>3. Data Sharing</h3>
        <p>We do not sell, rent, or share your personal information with third parties for commercial purposes. Data is only shared internally with the AutoParts Santiago team to fulfill your request.</p>

        <h3>4. Storage & Security</h3>
        <p>Your data is stored securely and only for as long as necessary to complete your request. We apply reasonable measures to protect your information against unauthorized access.</p>

        <h3>5. Your Rights</h3>
        <p>You have the right to request deletion of your data at any time. To do so, please contact us by email.</p>

        <h3>6. Contact</h3>
        <div class="contact-box">
            <strong>AutoParts Santiago</strong>
            Santiago, Veraguas, Panama<br>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a>
        </div>
    </div>

</div>

<footer>
    &copy; 2026 AutoParts Santiago &nbsp;·&nbsp; Santiago, Veraguas, Panama
</footer>

</body>
</html>"""
    return make_response(html, 200)


@app.route("/terms", methods=["GET"])
def terms():
    html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Terms of Service — AutoParts Santiago</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f9f9f9; color: #333; line-height: 1.7;
        }
        header {
            background: #1a1a2e; color: white;
            padding: 40px 24px; text-align: center;
        }
        header h1 { font-size: 1.6rem; margin-bottom: 6px; }
        header p { color: #aaa; font-size: 0.9rem; }
        .container {
            max-width: 800px; margin: 40px auto; padding: 0 24px 60px;
        }
        .lang-section {
            background: white; border-radius: 12px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
            padding: 36px 40px; margin-bottom: 32px;
        }
        .lang-label {
            display: inline-block; font-size: 0.75rem; font-weight: 700;
            letter-spacing: .08em; text-transform: uppercase;
            background: #1a1a2e; color: white;
            padding: 3px 12px; border-radius: 20px; margin-bottom: 20px;
        }
        h2 { font-size: 1.3rem; margin-bottom: 20px; color: #1a1a2e; }
        h3 { font-size: 1rem; font-weight: 700; margin: 24px 0 8px; color: #333; }
        p { margin-bottom: 12px; font-size: 0.95rem; color: #444; }
        ul { margin: 8px 0 12px 20px; }
        li { margin-bottom: 6px; font-size: 0.95rem; color: #444; }
        a { color: #1a1a2e; }
        .contact-box {
            background: #f0f2f5; border-radius: 8px;
            padding: 16px 20px; margin-top: 24px; font-size: 0.9rem;
        }
        .contact-box strong { display: block; margin-bottom: 4px; }
        footer {
            text-align: center; color: #aaa;
            font-size: 0.8rem; padding-bottom: 40px;
        }
    </style>
</head>
<body>

<header>
    <h1>AutoParts Santiago</h1>
    <p>Terms of Service &nbsp;·&nbsp; Términos de Servicio</p>
    <p style="margin-top:8px">Santiago, Veraguas, Panama &nbsp;·&nbsp; Last updated: 2026</p>
</header>

<div class="container">

    <!-- SPANISH -->
    <div class="lang-section">
        <span class="lang-label">Español</span>
        <h2>Términos de Servicio</h2>

        <h3>1. Aceptación de los términos</h3>
        <p>Al interactuar con el asistente de WhatsApp de AutoParts Santiago, aceptas los presentes términos de servicio. Si no estás de acuerdo, por favor abstente de utilizar el servicio.</p>

        <h3>2. Uso del servicio</h3>
        <p>El asistente de WhatsApp de AutoParts Santiago está diseñado exclusivamente para facilitar consultas de repuestos automotrices. El servicio no debe utilizarse para ningún otro fin.</p>

        <h3>3. Disponibilidad de piezas</h3>
        <p>AutoParts Santiago no garantiza la disponibilidad de ninguna pieza o repuesto en particular. La disponibilidad puede variar según el inventario de nuestros proveedores y está sujeta a cambios sin previo aviso.</p>

        <h3>4. Precios</h3>
        <p>Los precios proporcionados a través del asistente son estimaciones y están sujetos a cambios. El precio final será confirmado por un miembro del equipo antes de completar cualquier transacción.</p>

        <h3>5. Limitación de responsabilidad</h3>
        <p>AutoParts Santiago no se hace responsable por:</p>
        <ul>
            <li>Retrasos en la entrega de piezas causados por terceros</li>
            <li>Incompatibilidades de piezas no informadas al momento de la consulta</li>
            <li>Interrupciones del servicio de WhatsApp o del asistente virtual</li>
        </ul>

        <h3>6. Modificaciones</h3>
        <p>Nos reservamos el derecho de modificar estos términos en cualquier momento. El uso continuado del servicio constituye la aceptación de los términos vigentes.</p>

        <h3>7. Contacto</h3>
        <div class="contact-box">
            <strong>AutoParts Santiago</strong>
            Santiago, Veraguas, Panamá<br>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a>
        </div>
    </div>

    <!-- ENGLISH -->
    <div class="lang-section">
        <span class="lang-label">English</span>
        <h2>Terms of Service</h2>

        <h3>1. Acceptance of Terms</h3>
        <p>By interacting with the AutoParts Santiago WhatsApp assistant, you agree to these terms of service. If you do not agree, please refrain from using the service.</p>

        <h3>2. Use of Service</h3>
        <p>The AutoParts Santiago WhatsApp assistant is designed exclusively to facilitate auto parts inquiries. The service must not be used for any other purpose.</p>

        <h3>3. Parts Availability</h3>
        <p>AutoParts Santiago does not guarantee the availability of any particular part or component. Availability may vary based on supplier inventory and is subject to change without notice.</p>

        <h3>4. Pricing</h3>
        <p>Prices provided through the assistant are estimates and are subject to change. The final price will be confirmed by a team member before completing any transaction.</p>

        <h3>5. Limitation of Liability</h3>
        <p>AutoParts Santiago is not responsible for:</p>
        <ul>
            <li>Delivery delays caused by third parties</li>
            <li>Part incompatibilities not disclosed at the time of inquiry</li>
            <li>Interruptions to the WhatsApp service or virtual assistant</li>
        </ul>

        <h3>6. Modifications</h3>
        <p>We reserve the right to modify these terms at any time. Continued use of the service constitutes acceptance of the current terms.</p>

        <h3>7. Contact</h3>
        <div class="contact-box">
            <strong>AutoParts Santiago</strong>
            Santiago, Veraguas, Panama<br>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a>
        </div>
    </div>

</div>

<footer>
    &copy; 2026 AutoParts Santiago &nbsp;·&nbsp; Santiago, Veraguas, Panama
</footer>

</body>
</html>"""
    return make_response(html, 200)


@app.route("/deletion", methods=["GET"])
def deletion():
    html = """<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Data Deletion — AutoParts Santiago</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f9f9f9; color: #333; line-height: 1.7;
        }
        header {
            background: #1a1a2e; color: white;
            padding: 40px 24px; text-align: center;
        }
        header h1 { font-size: 1.6rem; margin-bottom: 6px; }
        header p { color: #aaa; font-size: 0.9rem; }
        .container {
            max-width: 800px; margin: 40px auto; padding: 0 24px 60px;
        }
        .lang-section {
            background: white; border-radius: 12px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
            padding: 36px 40px; margin-bottom: 32px;
        }
        .lang-label {
            display: inline-block; font-size: 0.75rem; font-weight: 700;
            letter-spacing: .08em; text-transform: uppercase;
            background: #1a1a2e; color: white;
            padding: 3px 12px; border-radius: 20px; margin-bottom: 20px;
        }
        h2 { font-size: 1.3rem; margin-bottom: 20px; color: #1a1a2e; }
        h3 { font-size: 1rem; font-weight: 700; margin: 24px 0 8px; color: #333; }
        p { margin-bottom: 12px; font-size: 0.95rem; color: #444; }
        ul { margin: 8px 0 12px 20px; }
        li { margin-bottom: 6px; font-size: 0.95rem; color: #444; }
        a { color: #1a1a2e; }
        .contact-box {
            background: #f0f2f5; border-radius: 8px;
            padding: 16px 20px; margin-top: 24px; font-size: 0.9rem;
        }
        .contact-box strong { display: block; margin-bottom: 4px; }
        .steps {
            counter-reset: steps;
            margin: 12px 0;
        }
        .step {
            display: flex; gap: 16px; align-items: flex-start;
            margin-bottom: 16px;
        }
        .step-num {
            background: #1a1a2e; color: white;
            border-radius: 50%; min-width: 28px; height: 28px;
            display: flex; align-items: center; justify-content: center;
            font-size: 0.8rem; font-weight: bold; margin-top: 2px;
        }
        .step-text { font-size: 0.95rem; color: #444; }
        footer {
            text-align: center; color: #aaa;
            font-size: 0.8rem; padding-bottom: 40px;
        }
    </style>
</head>
<body>

<header>
    <h1>AutoParts Santiago</h1>
    <p>Data Deletion Request &nbsp;·&nbsp; Solicitud de Eliminación de Datos</p>
    <p style="margin-top:8px">Santiago, Veraguas, Panama &nbsp;·&nbsp; Last updated: 2026</p>
</header>

<div class="container">

    <!-- SPANISH -->
    <div class="lang-section">
        <span class="lang-label">Español</span>
        <h2>Solicitud de Eliminación de Datos</h2>

        <p>En AutoParts Santiago respetamos tu derecho a controlar tu información personal. Si deseas que eliminemos los datos que tenemos asociados a ti, sigue los pasos a continuación.</p>

        <h3>¿Qué datos eliminamos?</h3>
        <ul>
            <li>Tu número de teléfono de WhatsApp</li>
            <li>El historial de mensajes asociado a tu número</li>
            <li>Cualquier consulta o cotización registrada a tu nombre</li>
        </ul>

        <h3>Cómo solicitar la eliminación</h3>
        <div class="steps">
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-text">Envía un correo a <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a> con el asunto <strong>"Solicitud de eliminación de datos"</strong>.</div>
            </div>
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-text">Incluye el número de WhatsApp del que deseas eliminar los datos.</div>
            </div>
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-text">Recibirás una confirmación de recepción dentro de las 48 horas hábiles.</div>
            </div>
            <div class="step">
                <div class="step-num">4</div>
                <div class="step-text">Tus datos serán eliminados dentro de un plazo máximo de <strong>30 días</strong> a partir de la recepción de tu solicitud.</div>
            </div>
        </div>

        <div class="contact-box">
            <strong>Contacto para eliminación de datos</strong>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a><br>
            AutoParts Santiago — Santiago, Veraguas, Panamá
        </div>
    </div>

    <!-- ENGLISH -->
    <div class="lang-section">
        <span class="lang-label">English</span>
        <h2>Data Deletion Request</h2>

        <p>At AutoParts Santiago, we respect your right to control your personal information. If you would like us to delete the data we have associated with you, please follow the steps below.</p>

        <h3>What data we delete</h3>
        <ul>
            <li>Your WhatsApp phone number</li>
            <li>The message history associated with your number</li>
            <li>Any inquiries or quotes recorded under your name</li>
        </ul>

        <h3>How to request deletion</h3>
        <div class="steps">
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-text">Send an email to <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a> with the subject line <strong>"Data Deletion Request"</strong>.</div>
            </div>
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-text">Include the WhatsApp number whose data you want deleted.</div>
            </div>
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-text">You will receive a confirmation of receipt within 48 business hours.</div>
            </div>
            <div class="step">
                <div class="step-num">4</div>
                <div class="step-text">Your data will be deleted within a maximum of <strong>30 days</strong> from the date your request is received.</div>
            </div>
        </div>

        <div class="contact-box">
            <strong>Data Deletion Contact</strong>
            <a href="mailto:ronelalmanza20@gmail.com">ronelalmanza20@gmail.com</a><br>
            AutoParts Santiago — Santiago, Veraguas, Panama
        </div>
    </div>

</div>

<footer>
    &copy; 2026 AutoParts Santiago &nbsp;·&nbsp; Santiago, Veraguas, Panama
</footer>

</body>
</html>"""
    return make_response(html, 200)


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
