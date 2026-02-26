import os
from agent.approval import send_whatsapp

# Maps forwarded message SID → store number (for owner reply routing)
store_message_map = {}

# In-memory store registry
# Each entry: number, name, contact, specialty (list), tier (1|2), active (bool)
STORE_REGISTRY = [
    # Example — add real stores here:
    # {
    #     "number": "+50712345678",
    #     "name": "Repuestos El Toro",
    #     "contact": "Juan",
    #     "specialty": ["frenos", "suspensión", "motor"],
    #     "tier": 1,
    #     "active": True
    # },
]


def get_registered_stores() -> list:
    return [s for s in STORE_REGISTRY if s.get("active", True)]


def get_store_numbers() -> list:
    return [s["number"] for s in get_registered_stores()]


def get_store_by_number(number: str) -> dict | None:
    for s in STORE_REGISTRY:
        if s["number"] == number:
            return s
    return None


def add_store(number: str, name: str, contact: str,
              specialty: list, tier: int = 1) -> dict:
    store = {
        "number": number,
        "name": name,
        "contact": contact,
        "specialty": specialty,
        "tier": tier,
        "active": True
    }
    STORE_REGISTRY.append(store)
    print(f"✅ Store added: {name} ({number})")
    return store


def remove_store(number: str) -> bool:
    for s in STORE_REGISTRY:
        if s["number"] == number:
            s["active"] = False
            print(f"🗑️ Store deactivated: {s['name']}")
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
