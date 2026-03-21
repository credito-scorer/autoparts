"""
Real estate lead qualifier for Zeli — Lotes La Coloradita, Santiago, Veraguas.
"""
import os
import json
import time
import re
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
- On follow-up messages, acknowledge the user's latest answer and move the conversation
  forward with the next specific question.
- Never repeat your previous assistant message verbatim.

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


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\wáéíóúñü]+", " ", (s or "").lower())).strip()


def _looks_generic_inventory_intro(reply: str) -> bool:
    text = _norm_text(reply)
    return (
        "tenemos 9 lotes disponibles" in text
        and "la coloradita" in text
        and "buscas para construir" in text
    )


def _last_assistant_message(history: list) -> str:
    for item in reversed(history):
        if item.get("role") == "assistant":
            return item.get("content", "")
    return ""


def _looks_repetitive(reply: str, history: list) -> bool:
    last_assistant = _last_assistant_message(history)
    if not last_assistant:
        return False
    return _norm_text(reply) == _norm_text(last_assistant)


def _extract_budget_from_message(message: str) -> str | None:
    msg = (message or "").lower()
    # Examples handled: "10k", "$15000", "15,000", "10 mil"
    m = re.search(r"(?:\$?\s*)?(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|mil)?\b", msg)
    if not m:
        return None
    raw_num = m.group(1).replace(",", "").replace(" ", "")
    suffix = m.group(2)
    try:
        amount = float(raw_num)
    except ValueError:
        return None
    if suffix == "k":
        amount *= 1000
    elif suffix == "mil" and amount < 1000:
        amount *= 1000
    amount_i = int(round(amount))
    if amount_i < 1000:
        return None
    return f"${amount_i:,.0f}"


def _extract_financing_from_message(message: str) -> str | None:
    msg = (message or "").lower()
    if any(k in msg for k in ("banco", "prestamo", "préstamo", "financ", "letra")):
        return "con banco"
    if any(k in msg for k in ("contado", "cash", "efectivo")):
        return "al contado"
    return None


def _extract_timeline_from_message(message: str) -> str | None:
    msg = (message or "").lower()
    if any(k in msg for k in (
        "ya", "pronto", "esta semana", "esta quincena", "este mes",
        "la proxima semana", "la próxima semana", "proxima semana", "próxima semana",
        "la semana que viene", "semana que viene", "la otra semana", "puede ser"
    )):
        return "corto plazo"
    if any(k in msg for k in ("mes que viene", "próximo mes", "proximo mes", "más adelante", "mas adelante")):
        return "mediano plazo"
    return None


def _has_visit_or_buy_intent(message: str) -> bool:
    msg = (message or "").lower()
    return any(k in msg for k in (
        "visita", "visitar", "ver el lote", "quiero verlo", "quiero comprar",
        "comprar", "separar", "apartar", "negociar", "vamos"
    ))


def _compute_lead_score(extracted: dict, message: str) -> tuple[int, bool]:
    score = 0
    if extracted.get("budget"):
        score += 2
    if extracted.get("financing"):
        score += 1
    if extracted.get("timeline"):
        score += 1
    visit_intent = _has_visit_or_buy_intent(message)
    if visit_intent:
        score += 2
    return score, visit_intent


def _progressive_followup_reply(message: str, extracted: dict) -> str:
    msg = (message or "").lower()
    if "constru" in msg:
        return (
            "¡Excelente, para construir te puede funcionar muy bien! "
            "Tenemos lotes de 600-700 m² con título de propiedad. "
            "¿Qué presupuesto manejas y comprarías al contado o con banco?"
        )
    if "invers" in msg:
        return (
            "Perfecto, como inversión es una zona con buen acceso en La Coloradita. "
            "Hoy tenemos 9 lotes disponibles desde $15,004. "
            "¿Qué rango de presupuesto te gustaría evaluar?"
        )
    if any(k in msg for k in ("cuanto", "cuánto", "precio", "precios", "costo", "cuesta")):
        return (
            "Los lotes están entre $15,004 y $17,502 según metraje (600-700 m²). "
            "Si quieres, te paso las mejores opciones según tu presupuesto."
        )
    if any(k in msg for k in ("agua", "luz", "electric", "internet", "servicio")):
        return (
            "Sí: los lotes tienen agua y electricidad; internet aún no. "
            "¿Te interesa más para construir pronto o para inversión?"
        )
    if extracted.get("budget") and not extracted.get("financing"):
        return (
            f"Perfecto, con {extracted.get('budget')} hay opciones para evaluar. "
            "¿Comprarías al contado o con banco?"
        )
    if extracted.get("budget") and extracted.get("financing") and not extracted.get("timeline"):
        return (
            "Buenísimo, con esa información te puedo orientar mejor. "
            "¿Para cuándo quisieras concretar compra o visita?"
        )

    if not extracted.get("budget"):
        return "Perfecto. Para orientarte mejor, ¿qué presupuesto tienes en mente para el lote?"
    if not extracted.get("financing"):
        return "Buenísimo. ¿Planeas comprar al contado o con financiamiento bancario?"
    if not extracted.get("timeline"):
        return "Entendido. ¿Para cuándo te gustaría concretar la compra?"
    return (
        "Perfecto. Si quieres, te comparto las opciones que mejor encajan con tu presupuesto "
        "y coordinamos visita. ¿Me compartes tu nombre?"
    )


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
        "lead_score":      0,
        "last_notified_score": 0,
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

    # Deterministic extraction to reduce misses on short WhatsApp follow-ups.
    det_budget = _extract_budget_from_message(message)
    det_financing = _extract_financing_from_message(message)
    det_timeline = _extract_timeline_from_message(message)
    if det_budget:
        stored["budget"] = det_budget
    if det_financing:
        stored["financing"] = det_financing
    if det_timeline:
        stored["timeline"] = det_timeline

    lead_score, visit_intent = _compute_lead_score(stored, message)
    conv["lead_score"] = max(conv.get("lead_score", 0), lead_score)

    # Upgrade intent deterministically when we already have meaningful buyer signals.
    if visit_intent:
        score = "ready_to_visit"
    elif lead_score >= 3 and score == "browsing":
        score = "considering"

    has_prior_assistant = bool(_last_assistant_message(conv["history"]))
    if _looks_repetitive(reply, conv["history"]) or (
        has_prior_assistant and _looks_generic_inventory_intro(reply)
    ):
        reply = _progressive_followup_reply(message, stored)

    prev_score        = conv["intent_score"]
    conv["intent_score"] = score
    conv["history"].append({"role": "user",      "content": message})
    conv["history"].append({"role": "assistant", "content": reply})

    # Update conversation store metadata
    try:
        from utils.conversation_store import update_metadata as _update_metadata
        _update_metadata(number, vertical="realestate", intent_score=score)
        if stored.get("name"):
            _update_metadata(number, customer_name=stored["name"])
    except Exception:
        pass

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

    deterministic_notify = lead_score >= 3
    should_notify = notify or score_escalated or (
        deterministic_notify and lead_score > conv.get("last_notified_score", 0)
    )
    if should_notify:
        send_owner_re_briefing(number, {"intent_score": score, "extracted": stored})
        conv["last_notified_score"] = lead_score
        log_event("realestate_lead_briefed", {
            "customer_number": number,
            "intent_score":    score,
            "lead_score":      lead_score,
            "extracted":       stored,
            "raw_message":     message,
        })

    print(f"🏠 RE lead {number}: score={score}, lead_score={lead_score}, notify={should_notify}")
