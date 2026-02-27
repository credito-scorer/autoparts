"""
Central production monitoring and alerting for Zeli Bot.

All alerts go to YOUR_PERSONAL_WHATSAPP via send_whatsapp().
send_whatsapp is lazy-imported inside send_alert() to avoid circular imports.

Cooldown: same alert_type cannot fire more than once per cooldown window
(default 5 minutes, configurable per alert type).
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta

PANAMA_TZ    = timezone(timedelta(hours=-5))
DEFAULT_COOLDOWN = 300   # 5 minutes

# ── Cooldown tracking ──────────────────────────────────────────────────────────

_cooldown: dict  = {}
_cooldown_lock   = threading.Lock()


def _can_send(alert_type: str, cooldown: int) -> bool:
    now = time.time()
    with _cooldown_lock:
        if now - _cooldown.get(alert_type, 0) >= cooldown:
            _cooldown[alert_type] = now
            return True
    return False


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def panama_now() -> str:
    return datetime.now(PANAMA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _panama_date() -> str:
    return datetime.now(PANAMA_TZ).strftime("%Y-%m-%d")


# ── Daily stats ────────────────────────────────────────────────────────────────

_stats: dict = {
    "date":              None,
    "conversations":     0,
    "quotes_sent":       0,
    "orders_confirmed":  0,
    "parts_not_found":   0,
    "errors":            0,
    "quote_times":       [],   # list of float minutes (sourcing start → quote sent)
}
_prev_stats: dict = {}
_stats_lock = threading.Lock()


def _reset_if_new_day() -> None:
    """Called inside _stats_lock. Captures previous day before resetting."""
    global _prev_stats
    today = _panama_date()
    if _stats["date"] != today:
        _prev_stats = {k: list(v) if isinstance(v, list) else v for k, v in _stats.items()}
        _stats.update({
            "date":             today,
            "conversations":    0,
            "quotes_sent":      0,
            "orders_confirmed": 0,
            "parts_not_found":  0,
            "errors":           0,
            "quote_times":      [],
        })


def increment_stat(key: str, value=1) -> None:
    with _stats_lock:
        _reset_if_new_day()
        if key == "quote_times":
            _stats["quote_times"].append(value)
        elif key in _stats:
            _stats[key] += value


def get_stats() -> dict:
    with _stats_lock:
        _reset_if_new_day()
        return {k: list(v) if isinstance(v, list) else v for k, v in _stats.items()}


# ── Message volume tracking ────────────────────────────────────────────────────

_msg_times: list = []
_msg_lock   = threading.Lock()


def track_message() -> int:
    """Record an incoming webhook message. Returns count in the last 60 minutes."""
    now = time.time()
    cutoff = now - 3600
    with _msg_lock:
        _msg_times.append(now)
        while _msg_times and _msg_times[0] < cutoff:
            _msg_times.pop(0)
        return len(_msg_times)


# ── Core send_alert ────────────────────────────────────────────────────────────

def send_alert(alert_type: str, message: str, cooldown: int = DEFAULT_COOLDOWN) -> None:
    """
    Send a WhatsApp alert to the owner, subject to cooldown.
    Uses lazy import to avoid circular dependencies.
    """
    if not _can_send(alert_type, cooldown):
        return
    owner = os.getenv("YOUR_PERSONAL_WHATSAPP")
    if not owner:
        return
    try:
        from agent.approval import send_whatsapp   # lazy import
        send_whatsapp(owner, message)
    except Exception as e:
        print(f"⚠️ [monitor] alert '{alert_type}' failed to send: {e}")


# ── Alert 1 — Claude API failure ───────────────────────────────────────────────

def alert_claude_error(error: Exception, module: str, customer: str = "unknown") -> None:
    msg = (
        f"⚠️ *Claude API Error*\n"
        f"❌ {error}\n"
        f"📍 Module: {module}\n"
        f"🕐 {panama_now()}\n"
        f"👤 Affected customer: {customer}"
    )
    print(f"⚠️ Claude API Error in {module}: {error}")
    send_alert("claude_error", msg)
    increment_stat("errors")


# ── Alert 2 — WhatsApp send failure (Railway log only) ────────────────────────

def alert_whatsapp_send_failed(recipient: str, message_preview: str) -> None:
    """Called when both initial send AND retry fail. WhatsApp is down → Railway log only."""
    msg = (
        f"⚠️ *WhatsApp Send Failed*\n"
        f"❌ Could not deliver message\n"
        f"👤 Intended recipient: {recipient}\n"
        f"📝 Message preview: {message_preview[:50]}\n"
        f"🕐 {panama_now()}"
    )
    # Transport is down — only Railway logs are reliable here
    print(f"❌ WHATSAPP SEND FAILED → {recipient}: {message_preview[:50]}")
    print(f"   Alert (Railway only): {msg}")


# ── Alert 3 — Sourcing timeout ─────────────────────────────────────────────────

def alert_sourcing_timeout(part: str, make: str, model: str, year: str, customer: str) -> None:
    msg = (
        f"⚠️ *Sourcing Timeout*\n"
        f"⏱️ source_parts() exceeded 30 seconds\n"
        f"🔩 Part: {part} — {make} {model} {year}\n"
        f"👤 Customer: {customer}\n"
        f"🕐 {panama_now()}"
    )
    print(f"⏱️ Sourcing timeout: {part} {make} {model} {year} for {customer}")
    send_alert(f"sourcing_timeout", msg)


# ── Alert 4 — Customer waiting too long ───────────────────────────────────────

def alert_customer_waiting(customer: str, part: str, make: str, model: str, year: str) -> None:
    msg = (
        f"⚠️ *Customer Waiting Too Long*\n"
        f"👤 Customer: {customer}\n"
        f"🔩 Request: {part} {make} {model} {year}\n"
        f"⏱️ No quote sent after 10 minutes\n"
        f"🕐 {panama_now()}\n"
        f"Acción requerida: verificar sourcing manualmente."
    )
    print(f"⚠️ Customer {customer} waiting >10min for {part}")
    # Per-customer cooldown so multiple parts don't spam
    send_alert(f"waiting_too_long_{customer}", msg, cooldown=600)


# ── Alert 5 — Customer abandoned at confirmation ──────────────────────────────

def alert_abandoned_confirmation(customer: str, part: str, make: str, model: str, year: str) -> None:
    msg = (
        f"📊 *Pedido Abandonado*\n"
        f"👤 Customer: {customer}\n"
        f"🔩 Was requesting: {part} {make} {model} {year}\n"
        f"📍 Dropped at: confirmation step\n"
        f"🕐 {panama_now()}\n"
        f"Considera hacer seguimiento manual."
    )
    send_alert(f"abandoned_{customer}", msg, cooldown=3600)


# ── Alert 6 — Part not found ───────────────────────────────────────────────────

def alert_part_not_found(part: str, make: str, model: str, year: str, customer: str) -> None:
    msg = (
        f"📊 *Pieza No Encontrada*\n"
        f"🔩 {part} — {make} {model} {year}\n"
        f"👤 Customer: {customer}\n"
        f"🕐 {panama_now()}"
    )
    print(f"📊 Part not found: {part} {make} {model} {year}")
    send_alert(f"not_found_{customer}_{part}", msg, cooldown=60)  # 1-minute cooldown
    increment_stat("parts_not_found")


# ── Alert 8 — Google Sheets failure ───────────────────────────────────────────

def alert_sheets_failed(error: Exception, order_summary: str) -> None:
    msg = (
        f"⚠️ *Sheets Logging Failed*\n"
        f"❌ {error}\n"
        f"📝 Lost entry: {order_summary}\n"
        f"🕐 {panama_now()}\n"
        f"Acción: verificar Google credentials en Railway."
    )
    print(f"⚠️ Sheets logging failed: {error}")
    send_alert("sheets_failed", msg, cooldown=1800)  # 30-minute cooldown
    increment_stat("errors")


# ── Alert 9 — High message volume ─────────────────────────────────────────────

def alert_high_volume(count: int) -> None:
    msg = (
        f"📊 *Alto Volumen de Mensajes*\n"
        f"📈 {count} mensajes en la última hora\n"
        f"🕐 {panama_now()}\n"
        f"Puede ser crecimiento orgánico o uso inesperado."
    )
    send_alert("high_volume", msg, cooldown=3600)


# ── Alert 10 — High memory ────────────────────────────────────────────────────

def check_memory_mb() -> float:
    """Return current RSS memory in MB. Uses /proc on Linux, resource as fallback."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024   # kB → MB
    except Exception:
        pass
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: kB; macOS: bytes
        import sys
        return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024
    except Exception:
        return 0.0


def alert_high_memory(mb: float) -> None:
    msg = (
        f"⚠️ *Alta Memoria en Producción*\n"
        f"💾 Memory usage: {mb:.0f}MB / ~512MB limit\n"
        f"🕐 {panama_now()}\n"
        f"Considera reiniciar el servicio si supera 480MB."
    )
    print(f"⚠️ High memory: {mb:.0f}MB")
    send_alert("high_memory", msg, cooldown=900)


# ── Daily summary ──────────────────────────────────────────────────────────────

def send_daily_summary() -> None:
    """Send yesterday's stats summary to the owner."""
    with _stats_lock:
        stats = {k: list(v) if isinstance(v, list) else v for k, v in _prev_stats.items()} if _prev_stats else get_stats()

    date         = stats.get("date", _panama_date())
    quote_times  = stats.get("quote_times", [])
    avg_time     = f"{sum(quote_times) / len(quote_times):.1f} min" if quote_times else "N/A"

    msg = (
        f"📊 *Resumen Diario — Zeli Bot*\n"
        f"🕐 {date}\n\n"
        f"Conversaciones: {stats.get('conversations', 0)}\n"
        f"Cotizaciones enviadas: {stats.get('quotes_sent', 0)}\n"
        f"Pedidos confirmados: {stats.get('orders_confirmed', 0)}\n"
        f"Piezas no encontradas: {stats.get('parts_not_found', 0)}\n"
        f"Errores: {stats.get('errors', 0)}\n"
        f"Tiempo promedio hasta cotización: {avg_time}\n\n"
        f"Bot status: ✅ Online"
    )
    owner = os.getenv("YOUR_PERSONAL_WHATSAPP")
    if owner:
        try:
            from agent.approval import send_whatsapp
            send_whatsapp(owner, msg)
            print("📊 Daily summary sent")
        except Exception as e:
            print(f"⚠️ Daily summary failed: {e}")


def _daily_summary_loop() -> None:
    """Daemon thread: sends a summary every day at 08:00 Panama time."""
    while True:
        now    = datetime.now(PANAMA_TZ)
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            send_daily_summary()
        except Exception as e:
            print(f"⚠️ Daily summary loop error: {e}")
