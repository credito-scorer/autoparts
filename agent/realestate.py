"""
Real estate lead qualifier for Zeli — Lotes La Coloradita, Santiago, Veraguas.
"""
import os
import json
import time
from datetime import datetime

from anthropic import Anthropic

from agent.approval import send_whatsapp
from utils.logger import log_event

_client = Anthropic()

# ── In-memory state ────────────────────────────────────────────────────────────

re_conversations: dict = {}
# number → {
#   "history":           [{"role": "user"|"assistant", "content": "..."}],
#   "intent_score":      "browsing" | "considering" | "ready_to_visit",
#   "extracted":         {name, budget, financing, timeline, specific_questions},
#   "created_at":        datetime isoformat,
#   "last_message_at":   datetime isoformat,
# }

re_briefing_map: dict = {}
# outbound briefing SID → customer_number (kept alive across replies, Option A)

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are Zeli's real estate assistant for residential lots in Santiago, Veraguas, Panama.

INVENTORY — Lotificación Lotes La Coloradita, Vía La Coloradita, Santiago, Veraguas:
  Lot 1  — 600.17 m²  — $15,004.25
  Lot 2  — 601.20 m²  — $15,030.00
  Lot 3  — 601.46 m²  — $15,036.50
  Lot 4  — 621.28 m²  — $15,532.00
  Lot 5  — 605.24 m²  — $15,131.00
  Lot 6  — 605.40 m²  — $15,135.00
  Lot 7  — 601.07 m²  — $15,026.75
  Lot 8  — 700.08 m²  — $17,502.00
  Lot 9  — 695.93 m²  — $17,398.25
  Sold: none — all 9 available.
  Title: Título de propiedad for all lots.
  Utilities: Water ✓ Electricity ✓ Internet ✗
  Road access: Asphalt to the property; crushed stone (tosca) inside the lotificación.
  Financing: Cash or bank loan (no owner financing). Banco Nacional offers land purchase loans.
             Payment in 1 or 2 installments.
  Nearby: 2 min from Panamericana, 25 min from Hospital Chicho Fábrega, 23 min from Santiago Mall.

YOUR JOB:
1. Have a natural conversation in Panamanian Spanish using tuteo (tú).
2. Answer questions about the lots. If you don't know a specific detail, say
   "te confirmo con el dueño" — never make up data.
3. Extract: name, budget, financing situation, timeline, specific questions.
4. Score intent as browsing, considering, or ready_to_visit.

CRITICAL BEHAVIOR:
- When the customer first mentions lots/terrenos/property, your FIRST reply must
  include concrete value from inventory: location, price range, available lot count,
  and at least one standout feature (size range, title, utilities, or access).
- Do NOT respond with only an open question like "¿Qué te gustaría saber?" on first contact.
- Inform first, then ask a focused qualifier question (e.g., budget, financing, timeline).

SCORING:
  browsing       — general curiosity, no urgency
  considering    — asking specifics (price, title, financing, size), comparing options
  ready_to_visit — wants to see the lot, asks how to buy, mentions budget/financing,
                   says "quiero visitarlo" or similar

TONE: Warm, helpful, like a knowledgeable friend. Not salesy. Not robotic.
Use tuteo. Keep replies concise — this is WhatsApp, not email.

RESPONSE FORMAT — return valid JSON only, no markdown fences:
{
  "reply": "Your WhatsApp message to the customer in Spanish",
  "intent_score": "browsing|considering|ready_to_visit",
  "extracted": {
    "name": null,
    "budget": null,
    "financing": null,
    "timeline": null,
    "specific_questions": []
  },
  "should_notify_owner": false
}\
"""

_OWNER_NUMBER = os.getenv("YOUR_PERSONAL_WHATSAPP", "")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt(val, fallback: str = "No mencionado") -> str:
    if not val:
        return fallback
    if isinstance(val, list):
        return ", ".join(val) if val else fallback
    return str(val)


# ── Public API ─────────────────────────────────────────────────────────────────

def qualify_lead(number: str, message: str, history: list) -> dict:
    """Call Claude and return the parsed JSON response, or a safe fallback."""
    messages = list(history) + [{"role": "user", "content": message}]
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM,
            messages=messages,
        )
        raw = resp.content[0].text.strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️ qualify_lead JSON decode error: {e}")
        return {
            "reply": (
                "¡Claro! Tenemos 9 lotes disponibles en La Coloradita, Santiago, "
                "desde $15,004 hasta $17,502 (600-700 m²), con título de propiedad "
                "y acceso asfaltado. ¿Buscas para construir pronto o como inversión?"
            ),
            "intent_score": "browsing",
            "extracted": {"name": None, "budget": None, "financing": None,
                          "timeline": None, "specific_questions": []},
            "should_notify_owner": False,
        }
    except Exception as e:
        print(f"⚠️ qualify_lead error: {e}")
        return {
            "reply": (
                "¡Claro! Tenemos 9 lotes disponibles en La Coloradita, Santiago, "
                "desde $15,004 hasta $17,502 (600-700 m²), con título de propiedad "
                "y acceso asfaltado. ¿Buscas para construir pronto o como inversión?"
            ),
            "intent_score": "browsing",
            "extracted": {"name": None, "budget": None, "financing": None,
                          "timeline": None, "specific_questions": []},
            "should_notify_owner": False,
        }


def send_owner_re_briefing(number: str, lead_data: dict) -> None:
    """Send owner a WhatsApp briefing and register the SID for reply forwarding."""
    if not _OWNER_NUMBER:
        print("⚠️ send_owner_re_briefing: YOUR_PERSONAL_WHATSAPP not set")
        return

    extracted = lead_data.get("extracted") or {}
    score     = lead_data.get("intent_score", "browsing")

    score_emoji = {"browsing": "👀", "considering": "🤔", "ready_to_visit": "🔥"}.get(score, "👀")

    body = (
        f"🏠 *Lead de terreno — {score_emoji} {score}*\n\n"
        f"Número: {number}\n"
        f"Nombre: {_fmt(extracted.get('name'), 'No proporcionado')}\n"
        f"Presupuesto: {_fmt(extracted.get('budget'))}\n"
        f"Financiamiento: {_fmt(extracted.get('financing'))}\n"
        f"Timeline: {_fmt(extracted.get('timeline'))}\n"
        f"Preguntas: {_fmt(extracted.get('specific_questions'))}\n\n"
        f"_Responde a este mensaje para hablarle directamente._"
    )

    msg_sid = send_whatsapp(_OWNER_NUMBER, body)
    if msg_sid:
        re_briefing_map[msg_sid] = number
        print(f"🏠 RE briefing sent → owner (sid={msg_sid}, lead={number})")
    else:
        print(f"⚠️ RE briefing failed to send for lead {number}")


def process_realestate_lead(number: str, message: str) -> None:
    """Main handler — maintain conversation state, qualify lead, brief owner."""
    now = datetime.now().isoformat()

    conv = re_conversations.setdefault(number, {
        "history":         [],
        "intent_score":    "browsing",
        "extracted":       {"name": None, "budget": None, "financing": None,
                            "timeline": None, "specific_questions": []},
        "created_at":      now,
        "last_message_at": now,
    })
    conv["last_message_at"] = now

    # Call qualifier
    result = qualify_lead(number, message, conv["history"])

    reply       = result.get("reply", "¿En qué te puedo ayudar con los lotes?")
    score       = result.get("intent_score", conv["intent_score"])
    extracted   = result.get("extracted") or {}
    notify      = result.get("should_notify_owner", False)

    # Merge extracted fields — don't overwrite non-null with null
    stored = conv["extracted"]
    for field in ("name", "budget", "financing", "timeline"):
        if extracted.get(field):
            stored[field] = extracted[field]
    if extracted.get("specific_questions"):
        existing = stored.get("specific_questions") or []
        for q in extracted["specific_questions"]:
            if q not in existing:
                existing.append(q)
        stored["specific_questions"] = existing

    prev_score        = conv["intent_score"]
    conv["intent_score"] = score
    conv["history"].append({"role": "user",      "content": message})
    conv["history"].append({"role": "assistant", "content": reply})

    # Send reply to customer
    send_whatsapp(number, reply)

    # Brief owner when: explicitly flagged, intent jumped to ready_to_visit,
    # or first time we hit considering+ with a name
    score_escalated = (
        score == "ready_to_visit" and prev_score != "ready_to_visit"
    ) or (
        score in ("considering", "ready_to_visit")
        and prev_score == "browsing"
        and stored.get("name")
    )

    if notify or score_escalated:
        send_owner_re_briefing(number, {"intent_score": score, "extracted": stored})
        log_event("realestate_lead_briefed", {
            "customer_number": number,
            "intent_score":    score,
            "extracted":       stored,
            "raw_message":     message,
        })

    print(f"🏠 RE lead {number}: score={score}, notify={notify or score_escalated}")
