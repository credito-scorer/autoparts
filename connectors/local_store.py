import os
import time
from agent.approval import send_whatsapp
from connectors.sheets import get_stores_sheet

# Maps forwarded message SID → store number (for owner reply routing)
store_message_map = {}

# Simple cache to avoid hitting Sheets on every webhook request
_store_cache: list = []
_cache_ts: float = 0
CACHE_TTL = 300  # seconds (5 minutes)


def _load_stores() -> list:
    """Read all rows from the Stores sheet and parse into dicts."""
    sheet = get_stores_sheet()
    records = sheet.get_all_records()
    stores = []
    for r in records:
        number = str(r.get("number", "")).strip()
        if not number:
            continue
        stores.append({
            "number": number,
            "name": str(r.get("name", "")).strip(),
            "contact": str(r.get("contact", "")).strip(),
            "specialty": [s.strip() for s in str(r.get("specialty", "")).split(",") if s.strip()],
            "tier": int(r.get("tier", 1) or 1),
            "active": str(r.get("active", "TRUE")).strip().upper() == "TRUE",
        })
    return stores


def _invalidate_cache():
    global _cache_ts
    _cache_ts = 0


def get_registered_stores() -> list:
    """Return all active stores, using cache where possible."""
    global _store_cache, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL:
        return [s for s in _store_cache if s.get("active")]
    try:
        _store_cache = _load_stores()
        _cache_ts = time.time()
    except Exception as e:
        print(f"⚠️ Could not load stores from sheet: {e}")
    return [s for s in _store_cache if s.get("active")]


def get_store_numbers() -> list:
    return [s["number"] for s in get_registered_stores()]


def get_store_by_number(number: str) -> dict | None:
    for s in get_registered_stores():
        if s["number"] == number:
            return s
    return None


def add_store(number: str, name: str, contact: str,
              specialty: list, tier: int = 1) -> dict:
    specialty_str = ",".join(specialty) if isinstance(specialty, list) else specialty
    sheet = get_stores_sheet()
    sheet.append_row([number, name, contact, specialty_str, tier, "TRUE"])
    _invalidate_cache()
    print(f"✅ Store added: {name} ({number})")
    return {
        "number": number, "name": name, "contact": contact,
        "specialty": specialty, "tier": tier, "active": True
    }


def remove_store(number: str) -> bool:
    sheet = get_stores_sheet()
    rows = sheet.get_all_values()
    for i, row in enumerate(rows):
        if row and str(row[0]).strip() == number:
            sheet.update_cell(i + 1, 6, "FALSE")
            _invalidate_cache()
            print(f"🗑️ Store deactivated: {number}")
            return True
    return False


def handle_store_message(store_number: str, message: str) -> None:
    """Forward a store's inbound message to the owner, tagged with store name."""
    store = get_store_by_number(store_number)
    store_name = store["name"] if store else store_number

    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")
    if not owner_number:
        return

    msg_sid = send_whatsapp(
        owner_number,
        f"🏪 *{store_name}:*\n{message}"
    )
    if msg_sid:
        store_message_map[msg_sid] = store_number
        print(f"📨 Store message forwarded: {store_name} → owner (SID: {msg_sid})")


def handle_owner_reply_to_store(store_number: str, message: str,
                                replied_sid: str) -> None:
    """Route owner's reply back to the store."""
    store = get_store_by_number(store_number)
    store_name = store["name"] if store else store_number

    send_whatsapp(store_number, f"💬 *AutoParts Santiago:*\n{message}")
    store_message_map.pop(replied_sid, None)
    print(f"📤 Owner reply sent to {store_name}: {message}")


def notify_store_pickup(store_number: str, part: str, make: str,
                        model: str, year: str, pickup_time: str) -> None:
    """Send a pickup heads-up to a local store."""
    store = get_store_by_number(store_number)
    store_name = store["name"] if store else store_number

    send_whatsapp(
        store_number,
        f"🚗 *AutoParts Santiago — Aviso de recogida*\n\n"
        f"Pieza: {part}\n"
        f"Vehículo: {make} {model} {year}\n"
        f"Hora de recogida: {pickup_time}\n\n"
        f"Gracias, {store_name}. 🙏"
    )
    print(f"📦 Pickup notification sent to {store_name}")


def list_stores_summary() -> str:
    """Readable text summary of all active registered stores."""
    stores = get_registered_stores()
    if not stores:
        return "No hay tiendas locales registradas."

    lines = [f"🏪 *Tiendas locales registradas ({len(stores)}):*\n"]
    for s in stores:
        specialty_str = ", ".join(s.get("specialty", [])) or "General"
        lines.append(
            f"• *{s['name']}* (Tier {s.get('tier', 1)})\n"
            f"  Contacto: {s.get('contact', '—')}\n"
            f"  Especialidad: {specialty_str}\n"
            f"  Número: {s['number']}"
        )
    return "\n\n".join(lines)
