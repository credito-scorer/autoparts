import threading
from agent.approval import send_whatsapp

_timers: dict = {}

FOLLOWUP_MESSAGE = (
    "Aún estamos buscando tu pieza, queremos darte la mejor opción. "
    "Un momento más. 🔩"
)


def schedule_followup(customer_number: str, delay: int = 300) -> None:
    """Send a follow-up message to the customer after `delay` seconds if not cancelled."""
    cancel_followup(customer_number)

    def _send():
        _timers.pop(customer_number, None)
        send_whatsapp(customer_number, FOLLOWUP_MESSAGE)
        print(f"⏰ Follow-up sent to {customer_number}")

    t = threading.Timer(delay, _send)
    t.daemon = True
    t.start()
    _timers[customer_number] = t
    print(f"⏳ Follow-up scheduled for {customer_number} in {delay}s")


def cancel_followup(customer_number: str) -> None:
    """Cancel a pending follow-up timer."""
    t = _timers.pop(customer_number, None)
    if t:
        t.cancel()
        print(f"✅ Follow-up cancelled for {customer_number}")
