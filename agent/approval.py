import os
import time
import threading
import requests
from agent.recommender import format_approval_message
from agent.responder import generate_quote_presentation
from dotenv import load_dotenv

load_dotenv()

PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1016895944841092")
API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"


def send_whatsapp(to: str, message: str) -> str | None:
    """Send a WhatsApp message via Meta Cloud API and return the message ID."""
    number = to.replace("whatsapp:", "").replace("+", "")

    headers = {
        "Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "text",
        "text": {"body": message}
    }

    try:
        resp = requests.post(API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        msg_id = resp.json()["messages"][0]["id"]
        return msg_id
    except Exception as e:
        print(f"❌ Failed to send WhatsApp to {to}: {e}")
        # Schedule one retry after 10 seconds in a background thread
        def _retry():
            time.sleep(10)
            try:
                r = requests.post(API_URL, json=payload, headers=headers)
                r.raise_for_status()
                print(f"✅ WhatsApp retry succeeded for {to}")
            except Exception as retry_err:
                print(f"❌ WhatsApp retry also failed for {to}: {retry_err}")
                try:
                    from utils.monitor import alert_whatsapp_send_failed
                    alert_whatsapp_send_failed(to, message)
                except Exception:
                    pass
        threading.Thread(target=_retry, daemon=True).start()
        return None


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

    # Match by reply-to first
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

    from utils.followup import cancel_followup, cancel_long_wait_alert
    cancel_followup(customer_number)
    cancel_long_wait_alert(customer_number)
    customer_quote = generate_quote_presentation(options, parsed, final_prices)
    send_whatsapp(customer_number, customer_quote)
    try:
        from utils.monitor import increment_stat
        increment_stat("quotes_sent")
    except Exception:
        pass

    prices_display = " / ".join([f"${p}" for p in final_prices])
    return (
        f"✅ Cotización enviada\n"
        f"Pieza: {parsed.get('part')} {parsed.get('make')} "
        f"{parsed.get('model')} {parsed.get('year')}\n"
        f"Precios: {prices_display}\n"
        f"Esperando selección del cliente."
    )
