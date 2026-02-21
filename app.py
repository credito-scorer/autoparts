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

def process_customer_request(incoming_number: str, incoming_message: str):
    parsed = parse_request(incoming_message)

    if not parsed:
        send_whatsapp(
            incoming_number,
            "No pude entender tu solicitud. 🙏\n\n"
            "Por favor envía la pieza, marca, modelo y año.\n"
            "Ejemplo: *alternador Toyota Hilux 2008*"
        )
        return

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

    # 1. YOUR PERSONAL NUMBER → Approval flow
    if incoming_number == os.getenv("YOUR_PERSONAL_WHATSAPP"):
        replied_to_sid = request.form.get("OriginalRepliedMessageSid", None)
        print(f"REPLIED TO SID: {replied_to_sid}")
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

    # 3. CUSTOMER SELECTING AN OPTION
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

    # 4. NEW CUSTOMER REQUEST
    response.message(
        "🔩 *Recibido!*\n"
        "Estamos buscando tu pieza, te confirmamos en unos minutos. ⏳"
    )

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
