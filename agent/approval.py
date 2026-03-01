import os
import time
import threading
import uuid
import random
import requests
from agent.recommender import format_approval_message
from agent.responder import generate_quote_presentation
from dotenv import load_dotenv

load_dotenv()

PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30


def send_whatsapp(to: str, message: str) -> str | None:
    """Send a WhatsApp message via Meta Cloud API and return the message ID.
    Uses connect/read timeouts; retries with exponential backoff on 429/5xx."""
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

    def _do_request() -> requests.Response:
        return requests.post(
            API_URL, json=payload, headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )

    try:
        resp = _do_request()
        resp.raise_for_status()
        msg_id = resp.json()["messages"][0]["id"]
        return msg_id
    except Exception as e:
        print(f"❌ Failed to send WhatsApp to {to}: {e}")
        status = getattr(e, "response", None) and getattr(e.response, "status_code", None)
        # Retry with exponential backoff for 429/5xx
        def _retry():
            for attempt in range(3):
                delay = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                try:
                    r = _do_request()
                    if r.status_code == 429:
                        retry_after = int(r.headers.get("Retry-After", 60))
                        time.sleep(retry_after)
                        continue
                    r.raise_for_status()
                    print(f"✅ WhatsApp retry succeeded for {to}")
                    return
                except Exception as retry_err:
                    print(f"❌ WhatsApp retry attempt {attempt + 1} failed for {to}: {retry_err}")
            try:
                from utils.monitor import alert_whatsapp_send_failed
                alert_whatsapp_send_failed(to, message)
            except Exception:
                pass
        threading.Thread(target=_retry, daemon=True).start()
        return None


def send_whatsapp_image(to: str, media_id: str, caption: str = "") -> str | None:
    """Send a WhatsApp image message via Meta Cloud API and return the message ID."""
    number = to.replace("whatsapp:", "").replace("+", "")

    headers = {
        "Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    image_payload: dict = {"id": media_id}
    if caption:
        image_payload["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "image",
        "image": image_payload,
    }

    try:
        resp = requests.post(
            API_URL, json=payload, headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        )
        if not resp.ok:
            print(f"❌ send_whatsapp_image HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()["messages"][0]["id"]
    except Exception as e:
        print(f"❌ send_whatsapp_image failed: {type(e).__name__}: {e}")
        raise


def send_for_approval(options: list, parsed: dict,
                      customer_number: str, pending_approvals: dict,
                      approval_message_map: dict = None):
    """Send approval request to owner. Keys by approval_id; approval_message_map by outbound SID."""
    approval_message = format_approval_message(options, parsed, customer_number)
    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")
    if not owner_number:
        print("⚠️ YOUR_PERSONAL_WHATSAPP not set — skipping approval")
        return

    approval_id = str(uuid.uuid4())
    pending_approvals[approval_id] = {
        "approval_id": approval_id,
        "customer_number": customer_number,
        "parsed": parsed,
        "options": options,
        "status": "awaiting_approval"
    }

    msg_sid = send_whatsapp(owner_number, approval_message)
    if msg_sid and approval_message_map is not None:
        approval_message_map[msg_sid] = approval_id  # key by outbound message SID
    print(f"✅ Approval request sent to your WhatsApp")


def handle_approval(message: str, pending_approvals: dict,
                    pending_selections: dict,
                    approval_message_map: dict = None,
                    replied_to: str = None,
                    on_cancel_reset: callable = None) -> str:
    message = message.strip().lower()

    if message == "cancelar":
        # Match by reply-to if present
        approval_id = None
        if replied_to and approval_message_map:
            approval_id = approval_message_map.pop(replied_to, None)
        if not approval_id and pending_approvals:
            # Fallback: cancel most recently added (last key)
            approval_id = list(pending_approvals.keys())[-1]
        if approval_id:
            pending = pending_approvals.pop(approval_id, None)
            if pending:
                customer_number = pending["customer_number"]
                if on_cancel_reset:
                    try:
                        on_cancel_reset(customer_number)
                    except Exception as e:
                        print(f"⚠️ on_cancel_reset for {customer_number}: {e}")
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

    # Match by reply-to (outbound message SID) first
    approval_id = None
    if replied_to and approval_message_map:
        approval_id = approval_message_map.pop(replied_to, None)

    # Fall back to matching by number of prices
    if not approval_id:
        for aid, pending in pending_approvals.items():
            if pending["status"] == "awaiting_approval":
                if len(final_prices) == len(pending["options"]):
                    approval_id = aid
                    break

    # Last resort
    if not approval_id:
        if len(pending_approvals) == 1:
            approval_id = list(pending_approvals.keys())[0]
        else:
            return (
                "No encontré una orden pendiente que coincida.\n"
                f"Órdenes pendientes: {len(pending_approvals)}"
            )

    pending = pending_approvals.pop(approval_id)
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
