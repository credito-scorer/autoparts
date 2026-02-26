import os
import time
import threading
from flask import Flask, request, jsonify, make_response, redirect
from dotenv import load_dotenv
from agent.parser import parse_request, extract_partial, parse_correction, interpret_option_choice, detect_needs_human
from agent.sourcing import source_parts
from agent.recommender import build_options
from agent.approval import send_for_approval, handle_approval, send_whatsapp
from agent.responder import generate_response, generate_quote_presentation
from utils.logger import log_request
from utils.dashboard import render_dashboard
from utils.followup import schedule_followup, cancel_followup
from connectors.whatsapp_supplier import (
    handle_supplier_response,
    get_registered_suppliers
)
from connectors.local_store import (
    get_store_numbers,
    handle_store_message,
    handle_owner_reply_to_store,
    store_message_map
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

# Partial request state: customer number → {part, make, model, year, last_seen}
pending_requests: dict = {}
STATE_TTL = 1800  # 30 minutes

# Complete requests awaiting customer confirmation before sourcing
pending_confirmation: dict = {}


def _get_request_state(number: str) -> dict | None:
    state = pending_requests.get(number)
    if not state:
        return None
    if time.time() - state.get("last_seen", 0) > STATE_TTL:
        pending_requests.pop(number, None)
        return None
    return state


def _update_request_state(number: str, fields: dict) -> dict:
    existing = pending_requests.get(number, {})
    updated = {
        "part":  str(fields.get("part")  or existing.get("part")  or "").strip(),
        "make":  str(fields.get("make")  or existing.get("make")  or "").strip(),
        "model": str(fields.get("model") or existing.get("model") or "").strip(),
        "year":  str(fields.get("year")  or existing.get("year")  or "").strip(),
        "last_seen": time.time(),
    }
    pending_requests[number] = updated
    return updated


def _clear_request_state(number: str) -> None:
    pending_requests.pop(number, None)


def _is_request_complete(state: dict) -> bool:
    return all(state.get(f) for f in ("part", "make", "model", "year"))


def _missing_fields(state: dict) -> list:
    return [f for f in ("part", "make", "model", "year") if not state.get(f)]


def _merge_with_state(new_fields: dict, existing: dict | None) -> dict:
    """Merge parsed fields with existing state. New non-empty values take priority."""
    if not existing:
        return new_fields
    return {
        "part":  str(new_fields.get("part")  or existing.get("part")  or "").strip(),
        "make":  str(new_fields.get("make")  or existing.get("make")  or "").strip(),
        "model": str(new_fields.get("model") or existing.get("model") or "").strip(),
        "year":  str(new_fields.get("year")  or existing.get("year")  or "").strip(),
        **{k: v for k, v in new_fields.items() if k not in ("part", "make", "model", "year")},
    }

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


def _run_sourcing(incoming_number: str, incoming_message: str, parsed: dict) -> None:
    """Run sourcing + approval flow for a confirmed, complete request."""
    send_whatsapp(
        incoming_number,
        "🔩 Recibido. Estamos buscando tu pieza, te confirmamos en unos minutos. ⏳"
    )
    schedule_followup(incoming_number, delay=300)

    log_request({
        "customer_number": incoming_number,
        "raw_message": incoming_message,
        "parsed": parsed,
        "status": "received"
    })

    results = source_parts(parsed)

    if not results:
        cancel_followup(incoming_number)
        send_whatsapp(
            incoming_number,
            generate_response("part_not_found", incoming_message, context={
                "pieza": parsed.get("part"),
                "vehículo": f"{parsed.get('make')} {parsed.get('model')} {parsed.get('year')}"
            })
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


def _send_missing_prompt(number: str, message: str, state: dict, is_first: bool) -> None:
    known = {k: v for k, v in state.items() if v and k in ("part", "make", "model", "year")}
    missing = _missing_fields(state)
    send_whatsapp(number, generate_response("missing_fields", message, {
        "known": known,
        "missing": missing,
        "is_first_message": is_first,
    }))


def process_customer_request(incoming_number: str, incoming_message: str):
    existing_state = _get_request_state(incoming_number)
    parsed = parse_request(incoming_message)

    # ── Conversation state tracking ──────────────────────────────────────────
    # Has any part/vehicle info? (partial or complete parse)
    has_part_info = parsed is not None and any(
        parsed.get(f) for f in ("part", "make", "model", "year")
    )

    if has_part_info:
        merged = _merge_with_state(parsed, existing_state)
        if _is_request_complete(merged):
            _clear_request_state(incoming_number)
            parsed = merged          # fall through to sourcing
        else:
            _update_request_state(incoming_number, merged)
            _send_missing_prompt(incoming_number, incoming_message, merged, existing_state is None)
            return

    elif existing_state:
        # No structured parse but we have state — try to extract new fields from this message
        partial = extract_partial(incoming_message, existing_state)
        if partial:
            merged = _merge_with_state(partial, existing_state)
            if _is_request_complete(merged):
                _clear_request_state(incoming_number)
                parsed = merged      # fall through to sourcing
            else:
                _update_request_state(incoming_number, merged)
                _send_missing_prompt(incoming_number, incoming_message, merged, False)
                return
        else:
            # Message didn't add info — handle conversationally but nudge toward completion
            if is_wait(incoming_message):
                send_whatsapp(incoming_number, generate_response("wait_acknowledgment", incoming_message))
            elif is_thanks(incoming_message):
                _clear_request_state(incoming_number)
                cancel_followup(incoming_number)
                send_whatsapp(incoming_number, generate_response("thanks", incoming_message))
            else:
                # Re-prompt for the missing fields (brief, no re-greeting)
                _send_missing_prompt(incoming_number, incoming_message, existing_state, False)
            return

    # ── Purely conversational (no part info, no existing state) ─────────────
    if parsed is None:
        if is_human_request(incoming_message):
            live_sessions[incoming_number] = True
            cancel_followup(incoming_number)
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

            send_whatsapp(incoming_number, generate_response("human_request", incoming_message))

        elif is_greeting(incoming_message):
            send_whatsapp(incoming_number, generate_response("greeting", incoming_message))
        elif is_secondary_greeting(incoming_message):
            send_whatsapp(incoming_number, generate_response("secondary_greeting", incoming_message))
        elif is_wait(incoming_message):
            send_whatsapp(incoming_number, generate_response("wait_acknowledgment", incoming_message))
        elif is_ack(incoming_message):
            send_whatsapp(incoming_number, generate_response("ack", incoming_message))
        elif is_thanks(incoming_message):
            cancel_followup(incoming_number)
            send_whatsapp(incoming_number, generate_response("thanks", incoming_message))
        elif is_vague_intent(incoming_message):
            send_whatsapp(incoming_number, generate_response("vague_intent", incoming_message))
        elif detect_needs_human(incoming_message):
            pending_live_offers[incoming_number] = True
            send_whatsapp(incoming_number, generate_response("human_request", incoming_message))
        else:
            send_whatsapp(incoming_number, generate_response("unknown", incoming_message))
        return

    # ── Request complete → ask customer to confirm before sourcing ────────────
    pending_confirmation[incoming_number] = dict(parsed)
    send_whatsapp(
        incoming_number,
        generate_response("confirmation_summary", incoming_message, context={
            "part":  parsed.get("part"),
            "make":  parsed.get("make"),
            "model": parsed.get("model"),
            "year":  parsed.get("year"),
        })
    )


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

        # Reply to a store message → route back to store
        if replied_to_sid and replied_to_sid in store_message_map:
            store_number = store_message_map[replied_to_sid]
            handle_owner_reply_to_store(store_number, incoming_message, replied_to_sid)
            send_whatsapp(owner_number, "✅ Mensaje enviado a la tienda.")
            return jsonify({"status": "ok"}), 200

        # Reply to a live session / escalation message
        if replied_to_sid and replied_to_sid in escalation_message_map:
            customer_number = escalation_message_map[replied_to_sid]

            if incoming_message.strip().lower() == "fin":
                live_sessions.pop(customer_number, None)
                send_whatsapp(
                    customer_number,
                    "Fue un gusto atenderte. Si necesitas algo más, aquí estamos. 👋\n\n"
                    "Para buscar un repuesto: Pieza + marca + modelo + año"
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

    # 3. LOCAL STORE → forward message to owner, never treat as customer
    if incoming_number in get_store_numbers():
        handle_store_message(incoming_number, incoming_message)
        return jsonify({"status": "ok"}), 200

    # 4. PENDING LIVE OFFER → customer responding to live session offer
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
                "Entendido, aquí estamos si necesitas algo. "
                "Para buscar un repuesto envíanos: Pieza + marca + modelo + año"
            )
        return jsonify({"status": "ok"}), 200

    # 5. LIVE SESSION → forward to owner, skip the bot

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

    # 6. CUSTOMER SELECTING AN OPTION
    if incoming_number in pending_selections:
        pending = pending_selections.get(incoming_number)
        options = pending["options"]
        final_prices = pending["final_prices"]
        parsed = pending["parsed"]

        choice = interpret_option_choice(incoming_message, options, final_prices)

        if choice is not None:
            chosen = options[choice]
            price = final_prices[choice]

            cancel_followup(incoming_number)
            send_whatsapp(
                incoming_number,
                f"✅ Confirmado. Tu {parsed.get('part')} para "
                f"{parsed.get('make')} {parsed.get('model')} {parsed.get('year')} "
                f"está apartado — *${price}*, entrega {chosen['lead_time']}. "
                f"Te contactamos para coordinar. 🙌"
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
            nums = " o ".join(str(i) for i in range(1, len(options) + 1))
            send_whatsapp(incoming_number, f"¿Cuál opción prefieres? Responde con el número ({nums}).")

        return jsonify({"status": "ok"}), 200

    # 6.5 PENDING CONFIRMATION → customer confirming or correcting their request
    if incoming_number in pending_confirmation:
        pending = pending_confirmation[incoming_number]
        msg_lower = incoming_message.strip().lower()
        affirmative = msg_lower in [
            "sí", "si", "correcto", "ok", "okey", "dale", "listo", "yes", "sip", "claro", "bueno", "va"
        ]
        if affirmative:
            del pending_confirmation[incoming_number]
            thread = threading.Thread(
                target=_run_sourcing,
                args=(incoming_number, incoming_message, pending)
            )
            thread.daemon = True
            thread.start()
        else:
            correction = parse_correction(incoming_message, pending)
            if correction:
                updated = {
                    **pending,
                    **{k: v for k, v in correction.items()
                       if k in ("part", "make", "model", "year") and v}
                }
                pending_confirmation[incoming_number] = updated
                send_whatsapp(
                    incoming_number,
                    generate_response("confirmation_summary", incoming_message, context={
                        "part":  updated.get("part"),
                        "make":  updated.get("make"),
                        "model": updated.get("model"),
                        "year":  updated.get("year"),
                    })
                )
            else:
                send_whatsapp(
                    incoming_number,
                    generate_response("correction_reminder", incoming_message, context={
                        "part":  pending.get("part"),
                        "make":  pending.get("make"),
                        "model": pending.get("model"),
                        "year":  pending.get("year"),
                    })
                )
        return jsonify({"status": "ok"}), 200

    # 7. ALL OTHER MESSAGES → process in background
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
        return redirect("/?failed=1", 302)
    return make_response(render_dashboard(), 200)


@app.route("/", methods=["GET"])
def index():
    failed = request.args.get("failed")
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoParts Dashboard</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f0f2f5; display: flex; align-items: center;
            justify-content: center; height: 100vh;
        }}
        .card {{
            background: white; border-radius: 14px;
            box-shadow: 0 4px 20px rgba(0,0,0,.1);
            padding: 40px; width: 100%; max-width: 360px; text-align: center;
        }}
        h1 {{ font-size: 1.3rem; color: #1a1a2e; margin-bottom: 6px; }}
        p {{ color: #888; font-size: 0.85rem; margin-bottom: 24px; }}
        input {{
            width: 100%; padding: 12px 14px; border: 1px solid #ddd;
            border-radius: 8px; font-size: 0.95rem; outline: none;
            margin-bottom: 12px; transition: border .2s;
        }}
        input:focus {{ border-color: #1a1a2e; }}
        button {{
            width: 100%; padding: 12px; background: #1a1a2e; color: white;
            border: none; border-radius: 8px; font-size: 0.95rem;
            font-weight: 600; cursor: pointer; transition: opacity .2s;
        }}
        button:hover {{ opacity: 0.85; }}
        .error {{ color: #c62828; font-size: 0.82rem; margin-bottom: 12px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>📊 AutoParts Dashboard</h1>
        <p>Ingresa tu contraseña para continuar</p>
        {'<div class="error">Contraseña incorrecta. Intenta de nuevo.</div>' if failed else ''}
        <form action="/dashboard" method="get">
            <input type="password" name="key" placeholder="Contraseña" autofocus required>
            <button type="submit">Entrar</button>
        </form>
    </div>
</body>
</html>"""
    return make_response(html, 200)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "running", "service": "AutoParts Trading Co."}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
