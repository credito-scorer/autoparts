"""
Exploratory handler for Zeli — unknown or ambiguous intent.
Collects unserved-need signals and reroutes when the customer clarifies.
"""
import json
from datetime import datetime

from anthropic import Anthropic

from agent.approval import send_whatsapp
from utils.logger import log_event

_client = Anthropic()

# ── In-memory state ────────────────────────────────────────────────────────────

exploratory_conversations: dict = {}
# number → {"history": [...], "created_at": isoformat, "last_message_at": isoformat}

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are the general assistant for Zeli, a company in Santiago, Veraguas, Panama.

Zeli currently offers two services:
  1. Auto parts sourcing — we find and deliver car parts to your workshop or home.
  2. Residential lots for sale in Santiago, Veraguas (Lotificación Lotes La Coloradita).

YOUR JOB:
- Have a warm, helpful conversation in Panamanian Spanish using tuteo (tú).
- If you can figure out what the person needs, tell them whether Zeli can help.
- If they need something Zeli doesn't offer yet, acknowledge it honestly and warmly,
  let them know Zeli doesn't handle that yet, and ask if there's anything else.
- If their message suggests they want auto parts or real estate, set should_reroute
  to true with the correct detected_need — do NOT explain the service, just reroute.
- Keep replies short — this is WhatsApp.

RESPONSE FORMAT — return valid JSON only, no markdown fences:
{
  "reply": "Your message in Spanish (empty string if should_reroute is true)",
  "detected_need": "autoparts|realestate|other",
  "other_need_description": "brief description of what they needed if other",
  "should_reroute": false
}\
"""


# ── Public API ─────────────────────────────────────────────────────────────────

def process_exploratory(number: str, message: str) -> None:
    """Handle unknown/ambiguous intent. Reroutes or replies as Zeli."""
    now = datetime.now().isoformat()

    conv = exploratory_conversations.setdefault(number, {
        "history":         [],
        "created_at":      now,
        "last_message_at": now,
    })
    conv["last_message_at"] = now

    messages = list(conv["history"]) + [{"role": "user", "content": message}]

    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_SYSTEM,
            messages=messages,
        )
        data = json.loads(resp.content[0].text.strip())
    except Exception as e:
        print(f"⚠️ process_exploratory error: {e}")
        send_whatsapp(number, "¡Hola! Somos Zeli. ¿En qué te puedo ayudar?")
        return

    should_reroute = data.get("should_reroute", False)
    detected       = data.get("detected_need", "other")
    reply          = data.get("reply", "")
    need_desc      = data.get("other_need_description", "")

    if should_reroute and detected in ("autoparts", "realestate"):
        # Clear exploratory state before rerouting
        exploratory_conversations.pop(number, None)

        if detected == "realestate":
            from agent.realestate import process_realestate_lead
            process_realestate_lead(number, message)
        else:
            # Late import avoids circular dependency at module load time
            import app as _app
            _app.process_customer_request(number, message)
        return

    # Not rerouting — send reply and update history
    if reply:
        conv["history"].append({"role": "user",      "content": message})
        conv["history"].append({"role": "assistant",  "content": reply})
        send_whatsapp(number, reply)

    # Log unserved needs for opportunity tracking
    if detected == "other" and need_desc:
        log_event("exploratory_unserved_need", {
            "customer_number":    number,
            "raw_message":        message,
            "need_description":   need_desc,
        })
        print(f"📊 Unserved need: '{need_desc}' from {number}")
