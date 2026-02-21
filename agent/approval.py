import os
from twilio.rest import Client
from agent.recommender import format_approval_message, format_customer_quote
from dotenv import load_dotenv

load_dotenv()

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

def send_whatsapp(to: str, message: str):
    twilio_client.messages.create(
        body=message,
        from_=os.getenv("TWILIO_WHATSAPP_NUMBER"),
        to=to
    )

def send_for_approval(options: list, parsed: dict,
                      customer_number: str, pending_approvals: dict,
                      approval_message_map: dict = None):
    approval_message = format_approval_message(options, parsed, customer_number)

    pending_approvals[customer_number] = {
        "customer_number": customer_number,
        "parsed": parsed,
        "options": options,
        "status": "awaiting_approval"
    }

    if approval_message_map is not None:
        approval_message_map[approval_message] = customer_number

    send_whatsapp(os.getenv("YOUR_PERSONAL_WHATSAPP"), approval_message)
    print(f"✅ Approval request sent to your WhatsApp")

def handle_approval(message: str, pending_approvals: dict,
                    pending_selections: dict,
                    approval_message_map: dict = None,
                    replied_to: str = None) -> str:
    message = message.strip().lower()

    if message == "cancelar":
        if pending_approvals:
            customer_number = list(pending_approvals.keys())[-1]
            pending_approvals.pop(customer_number)
            send_whatsapp(
                customer_number,
                "Lo sentimos, no pudimos conseguir esa pieza en este momento. "
                "Te avisamos cuando tengamos disponibilidad. 🙏"
            )
            return "❌ Orden cancelada. Cliente notificado."
        return "No hay órdenes pendientes."

    try:
        price_strings = message.replace(" ", "").split(",")
        final_prices = [float(p) for p in price_strings]
    except ValueError:
        return (
            "No entendí los precios. Responde con números separados por coma.\n"
            "Ejemplo: *195,222*\n"
            "O escribe *cancelar* para cancelar."
        )

    # Match by WhatsApp reply-to first
    matched_customer = None
    if replied_to and approval_message_map:
        matched_customer = approval_message_map.get(replied_to)
        if matched_customer:
            approval_message_map.pop(replied_to, None)

    # Fall back to matching by number of prices
    if not matched_customer:
        for customer_number, pending in pending_approvals.items():
            if pending["status"] == "awaiting_approval":
                if len(final_prices) == len(pending["options"]):
                    matched_customer = customer_number
                    break

    # Last resort
    if not matched_customer:
        if len(pending_approvals) == 1:
            matched_customer = list(pending_approvals.keys())[0]
        else:
            return (
                "No encontré una orden pendiente que coincida.\n"
                f"Órdenes pendientes: {len(pending_approvals)}"
            )

    pending = pending_approvals.pop(matched_customer)
    options = pending["options"]
    parsed = pending["parsed"]
    customer_number = pending["customer_number"]

    pending_selections[customer_number] = {
        "options": options,
        "parsed": parsed,
        "final_prices": final_prices
    }

    customer_quote = format_customer_quote(options, parsed, final_prices)
    send_whatsapp(customer_number, customer_quote)

    prices_display = " / ".join([f"${p}" for p in final_prices])
    return (
        f"✅ Cotización enviada\n"
        f"Pieza: {parsed.get('part')} {parsed.get('make')} "
        f"{parsed.get('model')} {parsed.get('year')}\n"
        f"Precios: {prices_display}\n"
        f"Esperando selección del cliente."
    )
