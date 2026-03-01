import threading
from agent.approval import send_whatsapp

_timers:      dict = {}
_long_timers: dict = {}
_timers_lock = threading.RLock()

FOLLOWUP_MESSAGE = (
    "Aún estamos buscando tu pieza, queremos darte la mejor opción. "
    "Un momento más. 🔩"
)


def schedule_followup(customer_number: str, delay: int = 300) -> None:
    """Send a follow-up message to the customer after `delay` seconds if not cancelled."""
    cancel_followup(customer_number)

    def _send():
        with _timers_lock:
            _timers.pop(customer_number, None)
        send_whatsapp(customer_number, FOLLOWUP_MESSAGE)
        print(f"⏰ Follow-up sent to {customer_number}")

    t = threading.Timer(delay, _send)
    t.daemon = True
    t.start()
    with _timers_lock:
        _timers[customer_number] = t
    print(f"⏳ Follow-up scheduled for {customer_number} in {delay}s")


def cancel_followup(customer_number: str) -> None:
    """Cancel a pending follow-up timer."""
    with _timers_lock:
        t = _timers.pop(customer_number, None)
    if t:
        t.cancel()
        print(f"✅ Follow-up cancelled for {customer_number}")


def schedule_long_wait_alert(customer_number: str, request_info: dict, delay: int = 600) -> None:
    """Alert owner if no quote is sent to customer after `delay` seconds (default 10 min)."""
    cancel_long_wait_alert(customer_number)

    def _alert():
        with _timers_lock:
            _long_timers.pop(customer_number, None)
        req = request_info or {}
        try:
            from utils.monitor import alert_customer_waiting
            alert_customer_waiting(
                customer_number,
                req.get("part", "?"),
                req.get("make", "?"),
                req.get("model", "?"),
                req.get("year", "?"),
            )
        except Exception as e:
            print(f"⚠️ Long-wait alert failed for {customer_number}: {e}")

    t = threading.Timer(delay, _alert)
    t.daemon = True
    t.start()
    with _timers_lock:
        _long_timers[customer_number] = t
    print(f"⏳ Long-wait alert scheduled for {customer_number} in {delay}s")


def cancel_long_wait_alert(customer_number: str) -> None:
    """Cancel a pending long-wait alert timer."""
    with _timers_lock:
        t = _long_timers.pop(customer_number, None)
    if t:
        t.cancel()
