"""
Real estate lead qualifier for Zeli — Lotes La Coloradita, Santiago, Veraguas.
"""
import os
import json
import time
import threading
import re
import sys
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

_last_message_time: dict = {}   # number → timestamp of last received message
_debounce_lock = threading.Lock()
DEBOUNCE_SECONDS = 3.0

_sheets_lock = threading.Lock()   # serialise all Sheets writes from multiple threads
_RE_STATE_TTL = 86400             # seconds — matches _is_vertical_conv_stale TTL


def _get_re_state_sheet():
    """Open (or create) the re_state worksheet in the Zeli Sheets workbook."""
    import gspread
    from connectors.sheets import get_client
    client = get_client()
    spreadsheet = client.open_by_key(os.getenv("GOOGLE_SHEETS_ID", ""))
    try:
        return spreadsheet.worksheet("re_state")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="re_state", rows=500, cols=3)
        ws.append_row(["number", "state", "updated_at"])
        return ws


def _persist_state(number: str, conv: dict) -> None:
    """Upsert conversation state to re_state sheet. Row = [number, state_json, updated_at]."""
    try:
        with _sheets_lock:
            sheet = _get_re_state_sheet()
            records = sheet.get_all_records()
            numbers = [str(r.get("number", "")) for r in records]
            row_data = [number, json.dumps(conv), datetime.utcnow().isoformat()]
            if number in numbers:
                row_idx = numbers.index(number) + 2  # 1-indexed + header row
                sheet.update(f"A{row_idx}:C{row_idx}", [row_data])
            else:
                sheet.append_row(row_data)
    except Exception as e:
        print(f"⚠️ State persist failed for {number}: {e}")


def _rehydrate_state() -> None:
    """Load persisted conversation state from re_state sheet into re_conversations."""
    try:
        sheet = _get_re_state_sheet()
        records = sheet.get_all_records()
        cutoff = time.time() - _RE_STATE_TTL
        loaded = 0
        for row in records:
            number     = str(row.get("number", "")).strip()
            state_json = str(row.get("state", "")).strip()
            if not number or not state_json:
                continue
            try:
                conv = json.loads(state_json)
            except Exception:
                continue
            # Skip entries older than the stale TTL — they'd be discarded anyway.
            last = conv.get("last_message_at") or conv.get("updated_at", "")
            try:
                from datetime import timezone
                last_ts = datetime.fromisoformat(str(last)).replace(
                    tzinfo=timezone.utc
                ).timestamp()
                if last_ts < cutoff:
                    continue
            except Exception:
                pass
            re_conversations[number] = conv
            loaded += 1
        print(f"♻️ Rehydrated {loaded} RE conversations from Sheets.")
    except Exception as e:
        print(f"⚠️ State rehydration failed: {e}")


_rehydrate_state()


def _should_process(number: str) -> bool:
    """Return True only if no newer message arrived for this number within DEBOUNCE_SECONDS."""
    with _debounce_lock:
        _last_message_time[number] = time.time()

    time.sleep(DEBOUNCE_SECONDS)

    with _debounce_lock:
        return _last_message_time.get(number) <= time.time() - DEBOUNCE_SECONDS + 0.05

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are Zeli's real estate assistant for residential lots in Santiago, Veraguas, Panama.

INVENTORY — Lotificación Lotes La Coloradita, Vía La Coloradita, Corregimiento de La Peña, Santiago, Veraguas:
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
  Title: Título de propiedad individual for all lots.
  Utilities: Water ✓  Electricity ✓  Internet ✗
  Road access: Asphalt to the property; paved material road inside the lotificación (in progress).
  Price: $25/m² — payment in máximo 2 installments (cash or bank loan; no owner financing).
  Financing: Banco Nacional offers land purchase loans.
  Features: River views and surrounding hills (cerros). Nearby green areas.
  Nearby: 2 min from Carretera Panamericana, ~20 min from Santiago Mall,
          25 min from Hospital Chicho Fábrega.
  Status: Segregación en proceso.

YOUR JOB:
1. Have a natural conversation in Panamanian Spanish using tuteo (tú).
2. Answer questions about the lots. If you don't know a specific detail, say
   "te confirmo con el dueño" — never make up data.
3. Extract: name, purpose (construir/invertir/ambas), financing (contado/banco), timeline,
   specific questions.
4. Score intent as browsing, considering, or ready_to_visit.

QUALIFYING QUESTION FLOW — after delivering initial listing info, ask these one at a time:
  Q1 (purpose): "¿Estás buscando el lote para construir, para invertir, o para ambas cosas?"
  Q2 (payment): "¿Estás pensando en pagar de contado o con financiamiento / préstamo bancario?"
  Do NOT ask budget as a direct question — infer from context if mentioned.

CRITICAL BEHAVIOR:
- On first contact, your reply must include concrete value: location, price range ($15,004–$17,502),
  available count (9 lots), and at least one standout feature (title, utilities, river views, access).
  Then ask Q1.
- Do NOT respond with only an open question like "¿Qué te gustaría saber?" on first contact.
- On follow-up messages, acknowledge the customer's answer and move the conversation forward.
- Never repeat your previous assistant message verbatim.

SIMPLE ACKNOWLEDGMENTS — do NOT escalate on these. Re-engage instead:
  Phrases: "Ok", "Okey", "Claro", "Entendido", "Gracias", "Dale", "Va", "Bueno", "Sí",
           "Si me interesa", "Me interesa", "Suena bien", "Buenas tardes", "Buenas",
           "Aremos el análisis", "Voy a pensar", "Voy a consultarlo", "Lo voy a pensar"
  Re-engagement response: "¿Tienes alguna otra pregunta sobre los lotes o querés coordinar una visita?"
  Set handoff=false for these.

SPECIFIC TRIGGERS — use these exact responses:

LOCATION QUESTIONS (→ escalate; NEVER share GPS coordinates or Google Maps links):
  Triggers: "ubicación exacta", "en el mapa", "dónde queda", "cómo llego", "exactamente dónde",
            "dirección específica", "a lado de qué", "cerca de qué", "cómo se llega", "dónde están"
  Response: "Con gusto te doy más detalles sobre la ubicación. Te comunico con alguien del equipo ahora mismo. 👍"
  Set handoff=true, handoff_reason="location_question"

IMAGE REQUESTS (→ escalate):
  Triggers: "fotos", "foto", "imagen", "imágenes", "me mandas", "quisiera ver", "cómo se ve",
            "más imágenes", "fotos del lote", "imágenes del terreno"
  Response: "Claro, para compartirte imágenes del terreno y las áreas aledañas, ya te comunico con alguien del equipo."
  Set handoff=true, handoff_reason="photos_requested"

PAYMENT TERMS (→ answer autonomously, do NOT escalate):
  Triggers: "condición de pago", "cómo se paga", "cuántos pagos", "forma de pago",
            "se puede financiar", "cuántas cuotas", "en cuánto tiempo se paga"
  Response: "Los lotes se cancelan en máximo dos pagos. El precio actual es de $25 por metro cuadrado, y está disponible por tiempo limitado. ¿Te interesa coordinar una visita para conocer el terreno?"
  Set handoff=false

LEGITIMACY QUESTIONS (→ answer autonomously, do NOT escalate):
  Triggers: "es real", "es cierto", "es verdad", "no es estafa", "es legítimo", "es confiable",
            "cómo sé que es real", "de ser real"
  Response: "Sí, es completamente real. 😊 Los lotes cuentan con título de propiedad individual, acceso por asfalto hasta el terreno, agua potable y electricidad. Estamos en proceso de segregación oficial. ¿Querés más información o coordinar una visita?"
  Set handoff=false

VISIT INTENT (→ escalate):
  Triggers: "quiero verlo", "quiero visitarlo", "quiero ver el terreno", "cómo puedo visitarlo",
            "podemos visitar", "sí me interesa verlo", "agendaría visita", "quisiera ir"
  Response: "Perfecto, podemos coordinar una visita al terreno. Te comunico con alguien del equipo para confirmar la fecha y hora."
  Set handoff=true, handoff_reason="visit_intent"

SCORING:
  browsing       — general curiosity, no urgency
  considering    — asking specifics (price, title, financing, size), comparing options
  ready_to_visit — wants to see the lot, asks how to buy, confirms visit interest

TONE: Warm, professional, approachable — like the best customer service agent. Not salesy.
Use tuteo. Keep replies concise — this is WhatsApp, not email.
Match Panamanian closing energy. Reciprocate "Bendiciones" warmly.
Closing phrases: "Que tenga buen día", "A la orden", "Con gusto". No foreign slang.

CRITICAL — LIVE HANDOFF RULES:
Include a "handoff" field in your JSON. Set it to true ONLY for:
1. Location/directions questions (see LOCATION QUESTIONS above)
2. Image/photo requests (see IMAGE REQUESTS above)
3. Visit intent confirmed (see VISIT INTENT above)
4. Customer explicitly asks to speak to a person ("quiero hablar con alguien", "me pueden llamar")
5. Customer seems frustrated, confused, or repeating themselves
6. Something you genuinely cannot do (send a document, call them, send a pin)

Do NOT escalate on: simple acknowledgments, payment terms questions, legitimacy questions.
When handoff is true, include a "handoff_reason" field.

RESPONSE FORMAT — return valid JSON only, no markdown fences:
{
  "reply": "Your WhatsApp message to the customer in Spanish",
  "intent_score": "browsing|considering|ready_to_visit",
  "extracted": {
    "name": null,
    "purpose": null,
    "budget": null,
    "financing": null,
    "timeline": null,
    "specific_questions": []
  },
  "should_notify_owner": false,
  "handoff": false,
  "handoff_reason": "location_question" | "photos_requested" | "visit_intent" | "bot_confused" | "customer_frustrated" | "human_requested" | "capability_limit" | null
}\
"""

_OWNER_NUMBER = os.getenv("YOUR_PERSONAL_WHATSAPP", "")
_QUAL_FIELDS = ("purpose", "financing", "timeline", "name")
_READY_LEAD_SCORE = 3

HANDOFF_REASONS = {
    "photos_requested":   "Cliente pidió fotos de los lotes",
    "location_question":  "Cliente preguntó por ubicación exacta o cómo llegar",
    "visit_intent":       "Cliente quiere coordinar una visita",
    "bot_confused":       "Bot no entendió la pregunta",
    "customer_frustrated":"Cliente parece frustrado o confundido",
    "human_requested":    "Cliente pidió hablar con una persona",
    "capability_limit":   "Cliente pidió algo que el bot no puede hacer",
    "repetitive":         "Bot estaba repitiendo respuestas",
}

SKEPTICISM_PHRASES = [
    "es cierto", "de ser real", "es verdad", "no es estafa",
    "es legítimo", "es legitimo", "es confiable", "cómo sé que es real",
    "como se que es real", "es real", "existe", "es serio",
]


def _is_skepticism(message: str) -> bool:
    msg = (message or "").lower().strip()
    return any(phrase in msg for phrase in SKEPTICISM_PHRASES)


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


def _forced_handoff_reason(message: str, repetitive: bool = False) -> str | None:
    """Deterministically force handoff for high-risk/owner-needed scenarios."""
    msg = (message or "").lower()

    # Photos/media requests.
    if any(k in msg for k in (
        "foto", "fotos", "imagen", "imagenes", "imágenes", "video", "videos",
        "cómo se ve", "como se ve", "me puedes enviar", "me manda", "mándame", "mandame"
    )):
        return "photos_requested"

    # Location / directions questions.
    if any(k in msg for k in (
        "ubicación exacta", "ubicacion exacta", "en el mapa", "dónde queda", "donde queda",
        "cómo llego", "como llego", "exactamente dónde", "exactamente donde",
        "dirección específica", "a lado de qué", "cerca de qué", "cómo se llega",
        "como se llega", "dónde están", "donde estan", "dónde está", "donde esta el lote",
        "pin", "coordenadas", "google maps", "maps", "ubicación del terreno"
    )):
        return "location_question"

    # Visit intent confirmed.
    if any(k in msg for k in (
        "quiero verlo", "quiero visitarlo", "quiero ver el terreno", "cómo puedo visitarlo",
        "como puedo visitarlo", "podemos visitar", "sí me interesa verlo", "si me interesa verlo",
        "agendaría visita", "agendar visita", "quisiera ir", "cuando puedo ir", "cuándo puedo ir"
    )):
        return "visit_intent"

    # Explicit human request.
    if any(k in msg for k in (
        "hablar con alguien", "hablar con una persona", "una persona", "un humano",
        "con el dueño", "con el asesor", "con un agente", "me pueden llamar", "me llaman"
    )):
        return "human_requested"

    # Confusion/frustration signals.
    if any(k in msg for k in (
        "no entiendo", "no me entiendes", "confund", "esto no funciona",
        "qué?", "que?", "no tiene sentido", "mejor alguien"
    )):
        return "customer_frustrated"

    return None


def _is_photo_request(message: str) -> bool:
    msg = (message or "").lower()
    return any(k in msg for k in (
        "foto", "fotos", "imagen", "imagenes", "imágenes", "video", "videos",
        "cómo se ve", "como se ve", "me puedes enviar", "me manda", "mándame", "mandame"
    ))


def _get_runtime_app_module():
    """Resolve the currently running app module (app or __main__)."""
    for mod_name in ("app", "__main__"):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "live_sessions") and hasattr(mod, "_state_lock"):
            return mod
    return None


def _safe_parse_json(text: str):
    """Robust JSON parsing — handles preamble, markdown fences, control chars."""
    if not text or not text.strip():
        return None

    cleaned = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        extracted = cleaned[start:end + 1]
        extracted = re.sub(r'[\x00-\x1f\x7f]', lambda m: {
            '\n': '\\n', '\r': '\\r', '\t': '\\t'
        }.get(m.group(), ''), extracted)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    return None


def _extract_name_from_message(message: str) -> str | None:
    raw = (message or "").strip()
    msg = raw.lower()
    patterns = [
        r"(?:me llamo|soy|mi nombre es)\s+([a-záéíóúñü][a-záéíóúñü\s]{1,40})$",
        r"^([a-záéíóúñü]{2,20})(?:\s+[a-záéíóúñü]{2,20})?$",
    ]
    stop_words = {
        "hola", "gracias", "ok", "okey", "si", "sí", "dale", "buenas", "buenos dias",
        "buenos días", "interesado", "construir", "inversion", "inversión",
    }
    if any(ch.isdigit() for ch in msg):
        return None
    for idx, pat in enumerate(patterns):
        m = re.search(pat, msg)
        if not m:
            continue
        candidate = m.group(1).strip()
        if candidate in stop_words:
            return None
        parts = [p.capitalize() for p in candidate.split() if p]
        if not parts:
            return None
        # Pattern 2 is permissive; reject known non-name content.
        if idx == 1 and any(p.lower() in stop_words for p in parts):
            return None
        return " ".join(parts)
    return None


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


def _extract_purpose_from_message(message: str) -> str | None:
    msg = (message or "").lower()
    if any(k in msg for k in (
        "ambas", "ambos", "las dos", "los dos", "construir e invertir", "invertir y construir",
        "para las dos", "para los dos"
    )):
        return "construir e invertir"
    if any(k in msg for k in (
        "construir", "casa", "vivienda", "residencia", "habitacion", "habitación"
    )):
        return "construir"
    if any(k in msg for k in (
        "invertir", "inversion", "inversión", "retorno", "valoriz", "vender", "negocio"
    )):
        return "invertir"
    return None


def _extract_timeline_from_message(message: str) -> str | None:
    msg = (message or "").lower()
    weekdays = (
        "lunes", "martes", "miercoles", "miércoles", "jueves",
        "viernes", "sabado", "sábado", "domingo"
    )
    if any(k in msg for k in (
        "ya", "pronto", "esta semana", "esta quincena", "este mes",
        "la proxima semana", "la próxima semana", "proxima semana", "próxima semana",
        "la semana que viene", "semana que viene", "la otra semana", "puede ser",
        "el lunes", "el martes", "el miercoles", "el miércoles", "el jueves",
        "el viernes", "el sabado", "el sábado", "el domingo",
    )):
        return "corto plazo"
    if msg.strip() in weekdays:
        return "corto plazo"
    if re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", msg):
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
    if extracted.get("purpose"):
        score += 1
    if extracted.get("financing"):
        score += 1
    if extracted.get("budget"):
        score += 1
    if extracted.get("timeline"):
        score += 1
    visit_intent = _has_visit_or_buy_intent(message)
    if visit_intent:
        score += 2
    return score, visit_intent


def _next_missing_field(extracted: dict) -> str | None:
    for field in _QUAL_FIELDS:
        if not extracted.get(field):
            return field
    return None


def _next_prompt_for_field(field: str, extracted: dict) -> str:
    if field == "purpose":
        return "¿Estás buscando el lote para construir, para invertir, o para ambas cosas?"
    if field == "financing":
        return "¿Estás pensando en pagar de contado o con financiamiento / préstamo bancario?"
    if field == "timeline":
        return "¿Para cuándo te gustaría concretar la compra o visita?"
    if field == "name":
        return "¿Me compartes tu nombre para seguir en contacto?"
    return "¿Tienes alguna otra pregunta o querés coordinar una visita?"


def _progressive_followup_reply(message: str, extracted: dict) -> str:
    msg = (message or "").lower()
    if "constru" in msg:
        return (
            "¡Perfecto, para construir es una excelente opción! "
            "Tenemos lotes de 600-700 m² con título de propiedad, agua, luz y vistas al río. "
            "¿Estás pensando en pagar de contado o con financiamiento bancario?"
        )
    if "invers" in msg:
        return (
            "Perfecto como inversión — zona de buen acceso y valorización en La Peña, Santiago. "
            "Hoy tenemos 9 lotes disponibles desde $15,004. "
            "¿Estás pensando en pagar de contado o con banco?"
        )
    if any(k in msg for k in ("cuanto", "cuánto", "precio", "precios", "costo", "cuesta")):
        return (
            "Los lotes están entre $15,004 y $17,502 según metraje (600-700 m², a $25/m²). "
            "¿Los buscas para construir, para invertir, o para ambas cosas?"
        )
    if any(k in msg for k in ("agua", "luz", "electric", "internet", "servicio")):
        return (
            "Sí: los lotes tienen agua potable y electricidad; internet aún no. "
            "¿Los buscas para construir o como inversión?"
        )
    if extracted.get("purpose") and not extracted.get("financing"):
        return "¿Estás pensando en pagar de contado o con financiamiento / préstamo bancario?"
    if extracted.get("financing") and not extracted.get("timeline"):
        return "¿Para cuándo te gustaría concretar la compra o visita?"
    if not extracted.get("purpose"):
        return "¿Estás buscando el lote para construir, para invertir, o para ambas cosas?"
    if not extracted.get("financing"):
        return "¿Estás pensando en pagar de contado o con financiamiento / préstamo bancario?"
    return "¿Tienes alguna otra pregunta o querés coordinar una visita?"


def _start_live_handoff(number: str, extracted: dict, score: str, lead_score: int) -> None:
    """Start live session in app for qualified RE lead."""
    _app = _get_runtime_app_module()
    if _app is None:
        print("⚠️ RE live handoff: runtime app module not found")
        return

    owner_digits = re.sub(r"\D", "", os.getenv("YOUR_PERSONAL_WHATSAPP", "") or "")
    owner_number = f"+{owner_digits}" if owner_digits else ""
    with _app._state_lock:
        already_live = number in _app.live_sessions
        _app.live_sessions[number] = True

    if already_live or not owner_number:
        return

    msg_sid = send_whatsapp(
        owner_number,
        f"🔴 *Lead RE listo para atención en vivo*\n"
        f"Cliente: {number}\n"
        f"Nombre: {_fmt(extracted.get('name'), 'No proporcionado')}\n"
        f"Presupuesto: {_fmt(extracted.get('budget'))}\n"
        f"Financiamiento: {_fmt(extracted.get('financing'))}\n"
        f"Timeline: {_fmt(extracted.get('timeline'))}\n"
        f"Intent: {score} | Lead score: {lead_score}\n\n"
        f"_Responde aquí para hablarle directamente. Escribe fin para cerrar._"
    )
    if msg_sid:
        with _app._state_lock:
            _app.escalation_message_map[msg_sid] = number


def _send_handoff_briefing(
    number: str,
    reason_key: str,
    last_customer_message: str,
    extracted: dict,
    score: str,
    history: list,
) -> None:
    """Send transitional message to customer + escalation briefing to owner."""
    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP", "")

    # 1. Notify customer
    send_whatsapp(number, "Un momento, te comunico con alguien del equipo para ayudarte mejor. 👍")

    if not owner_number:
        return

    reason_text = HANDOFF_REASONS.get(reason_key, reason_key)
    name = _fmt(extracted.get("name"), "No proporcionado")
    lead_score_val = extracted.get("_lead_score", "—")

    # Build a brief conversation summary (last 4 turns max)
    recent = history[-8:] if len(history) > 8 else history
    summary_lines = []
    for m in recent:
        role_label = "Cliente" if m.get("role") == "user" else "Bot"
        snippet = m.get("content", "")[:120]
        summary_lines.append(f"  {role_label}: {snippet}")
    summary = "\n".join(summary_lines) if summary_lines else "  (sin historial)"

    clean_number = number.replace("whatsapp:", "").replace("+", "").strip()

    body = (
        f"🏠🔴 *Handoff de terreno — necesita atención*\n\n"
        f"Número: {number}\n"
        f"Nombre: {name}\n"
        f"Razón: {reason_text}\n"
        f"Score: {score}\n\n"
        f"Último mensaje del cliente: \"{last_customer_message}\"\n\n"
        f"Resumen de la conversación:\n{summary}\n\n"
        f"_Responde a este mensaje para hablarle directamente._"
    )

    msg_sid = send_whatsapp(owner_number, body)
    if msg_sid:
        re_briefing_map[msg_sid] = number
        print(f"🏠🔴 Handoff briefing sent → owner (sid={msg_sid}, lead={number}, reason={reason_key})")
    else:
        print(f"⚠️ Handoff briefing failed for {number}")


# ── Public API ─────────────────────────────────────────────────────────────────

_HANDOFF_TRIGGER = {
    "reply": "",
    "intent_score": "browsing",
    "extracted": {"name": None, "budget": None, "financing": None,
                  "timeline": None, "specific_questions": []},
    "should_notify_owner": False,
    "handoff": True,
    "handoff_reason": "bot_confused",
}


def qualify_lead(number: str, message: str, history: list) -> dict:
    """Call Claude and return the parsed JSON response, or a handoff trigger on failure."""
    messages = list(history) + [{"role": "user", "content": message}]
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=_SYSTEM,
            messages=messages,
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        result = _safe_parse_json(raw)
        if result is None:
            print(f"⚠️ qualify_lead JSON parse failed for {number}. Raw (first 200): {raw[:200]!r}")
            return dict(_HANDOFF_TRIGGER)
        return result
    except Exception as e:
        print(f"⚠️ qualify_lead error for {number}: {e}")
        return dict(_HANDOFF_TRIGGER)


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
        f"Propósito: {_fmt(extracted.get('purpose'))}\n"
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
    if not _should_process(number):
        print(f"⏭️ Debounced duplicate from {number}, skipping.")
        return

    now = datetime.now().isoformat()
    normalized_number = number.replace("whatsapp:", "").strip()

    # Hydrate persistent profile memory if available.
    persisted_profile = {}
    try:
        from utils.conversation_store import get_conversation as _get_conversation
        persisted = _get_conversation(normalized_number) or {}
        persisted_profile = dict((persisted.get("re_profile") or {}))
        if persisted.get("customer_name") and not persisted_profile.get("name"):
            persisted_profile["name"] = persisted.get("customer_name")
    except Exception:
        persisted_profile = {}

    conv = re_conversations.setdefault(number, {
        "history":         [],
        "intent_score":    "browsing",
        "extracted":       {"name": None, "purpose": None, "budget": None,
                            "financing": None, "timeline": None, "specific_questions": []},
        "lead_score":      0,
        "last_notified_score": 0,
        "qualification_stage": "collect_purpose",
        "live_handoff_started": False,
        "repeat_count": 0,
        "created_at":      now,
        "last_message_at": now,
    })
    conv["last_message_at"] = now

    # Defensive guard: if already in live_handoff, forward directly and skip qualifier
    if conv.get("state") == "live_handoff":
        owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP", "")
        if owner_number:
            send_whatsapp(
                owner_number,
                f"💬 *RE Lead ({number}):*\n{message}",
            )
        print(f"🏠 Live handoff guard (process_realestate_lead): forwarded from {number} to owner")
        return
    if persisted_profile:
        for field in ("name", "purpose", "budget", "financing", "timeline"):
            if persisted_profile.get(field) and not conv["extracted"].get(field):
                conv["extracted"][field] = persisted_profile[field]

    # Call qualifier
    result = qualify_lead(number, message, conv["history"])

    reply          = result.get("reply", "¿En qué te puedo ayudar con los lotes?")
    score          = result.get("intent_score", conv["intent_score"])
    extracted      = result.get("extracted") or {}
    notify         = result.get("should_notify_owner", False)
    bot_handoff    = result.get("handoff", False)
    handoff_reason = result.get("handoff_reason") or "bot_confused"

    # ── Auto-handoff: qualifier signal OR deterministic guardrails ─────────────
    repetitive = _looks_repetitive(reply, conv["history"])
    if repetitive:
        conv["repeat_count"] = int(conv.get("repeat_count", 0)) + 1
    else:
        conv["repeat_count"] = 0

    # Skepticism short-circuit: let Claude handle autonomously, never force handoff.
    if _is_skepticism(message):
        forced_reason = None
        bot_handoff = False
    else:
        forced_reason = _forced_handoff_reason(message, repetitive=False)
        # Guardrail: if model marks photos_requested but message does not ask for media,
        # downgrade reason to avoid misleading owner context.
        if (
            bot_handoff
            and handoff_reason == "photos_requested"
            and not _is_photo_request(message)
        ):
            handoff_reason = "bot_confused"
        # Escalate repetitive loops only after repeated recurrence.
        if not forced_reason and conv["repeat_count"] >= 2:
            forced_reason = "repetitive"
        if forced_reason:
            bot_handoff = True
            handoff_reason = forced_reason

    if bot_handoff and not conv.get("state") == "live_handoff":
        # Merge what we have before briefing
        stored_pre = conv["extracted"]
        for field in ("name", "purpose", "budget", "financing", "timeline"):
            if extracted.get(field):
                stored_pre[field] = extracted[field]
        conv["history"].append({"role": "user", "content": message})
        conv["state"] = "live_handoff"
        conv["live_handoff_started"] = True
        # Pass lead_score into extracted for briefing display
        stored_pre["_lead_score"] = conv.get("lead_score", 0)
        _send_handoff_briefing(
            number=number,
            reason_key=handoff_reason,
            last_customer_message=message,
            extracted=stored_pre,
            score=score,
            history=conv["history"],
        )
        del stored_pre["_lead_score"]
        _start_live_handoff(number, stored_pre, score, conv.get("lead_score", 0))
        try:
            from utils.conversation_store import update_metadata as _update_metadata
            _update_metadata(
                normalized_number,
                vertical="realestate",
                intent_score=score,
                customer_name=stored_pre.get("name"),
                re_profile={
                    "name": stored_pre.get("name"),
                    "budget": stored_pre.get("budget"),
                    "financing": stored_pre.get("financing"),
                    "timeline": stored_pre.get("timeline"),
                    "lead_score": conv.get("lead_score", 0),
                    "qualification_stage": "live_handoff",
                    "live_handoff_started": True,
                    "handoff_reason": handoff_reason,
                    "state": "live_handoff",
                    "updated_at": now,
                },
            )
        except Exception:
            pass
        print(f"🏠🔴 RE handoff triggered for {number}: reason={handoff_reason}")
        _persist_state(number, conv)
        return

    # Merge extracted fields — don't overwrite non-null with null
    stored = conv["extracted"]
    for field in ("name", "purpose", "budget", "financing", "timeline"):
        if extracted.get(field):
            stored[field] = extracted[field]
    if extracted.get("specific_questions"):
        existing = stored.get("specific_questions") or []
        for q in extracted["specific_questions"]:
            if q not in existing:
                existing.append(q)
        stored["specific_questions"] = existing

    # Deterministic extraction to reduce misses on short WhatsApp follow-ups.
    det_purpose   = _extract_purpose_from_message(message)
    det_budget    = _extract_budget_from_message(message)
    det_financing = _extract_financing_from_message(message)
    det_timeline  = _extract_timeline_from_message(message)
    det_name      = _extract_name_from_message(message)
    if det_purpose:
        stored["purpose"] = det_purpose
    if det_budget:
        stored["budget"] = det_budget
    if det_financing:
        stored["financing"] = det_financing
    if det_timeline:
        stored["timeline"] = det_timeline
    if det_name:
        stored["name"] = det_name

    lead_score, visit_intent = _compute_lead_score(stored, message)
    conv["lead_score"] = max(conv.get("lead_score", 0), lead_score)
    next_missing = _next_missing_field(stored)
    conv["qualification_stage"] = f"collect_{next_missing}" if next_missing else "handoff_ready"

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
        _update_metadata(
            normalized_number,
            vertical="realestate",
            intent_score=score,
            customer_name=stored.get("name"),
            re_profile={
                "name": stored.get("name"),
                "budget": stored.get("budget"),
                "financing": stored.get("financing"),
                "timeline": stored.get("timeline"),
                "lead_score": conv.get("lead_score", 0),
                "qualification_stage": conv.get("qualification_stage"),
                "live_handoff_started": conv.get("live_handoff_started", False),
                "updated_at": now,
            },
        )
    except Exception:
        pass

    # Brief owner when: explicitly flagged, intent jumped to ready_to_visit,
    # or first time we hit considering+ with a name
    score_escalated = (
        score == "ready_to_visit" and prev_score != "ready_to_visit"
    ) or (
        score in ("considering", "ready_to_visit")
        and prev_score == "browsing"
        and bool(stored.get("name"))
    )

    deterministic_notify = lead_score >= 3
    should_notify = notify or score_escalated or (
        deterministic_notify and lead_score > conv.get("last_notified_score", 0)
    )
    should_start_live = (
        not next_missing
        and lead_score >= _READY_LEAD_SCORE
        and not conv.get("live_handoff_started", False)
    )

    # Send reply to customer
    if should_start_live:
        handoff_reply = (
            f"Perfecto{f' {stored.get('name')}' if stored.get('name') else ''}. "
            "En un momento te escribe alguien del equipo de Zeli para coordinar visita y siguientes pasos. 👍"
        )
        conv["history"][-1]["content"] = handoff_reply
        send_whatsapp(number, handoff_reply)
    else:
        send_whatsapp(number, reply)

    if should_notify or should_start_live:
        send_owner_re_briefing(number, {"intent_score": score, "extracted": stored})
        conv["last_notified_score"] = lead_score
        if should_start_live:
            conv["live_handoff_started"] = True
            _start_live_handoff(number, stored, score, lead_score)
        log_event("realestate_lead_briefed", {
            "customer_number": number,
            "intent_score":    score,
            "lead_score":      lead_score,
            "extracted":       stored,
            "raw_message":     message,
            "live_handoff_started": should_start_live,
        })

    try:
        from utils.conversation_store import update_metadata as _update_metadata
        _update_metadata(
            normalized_number,
            re_profile={
                "name": stored.get("name"),
                "budget": stored.get("budget"),
                "financing": stored.get("financing"),
                "timeline": stored.get("timeline"),
                "lead_score": conv.get("lead_score", 0),
                "qualification_stage": conv.get("qualification_stage"),
                "live_handoff_started": conv.get("live_handoff_started", False),
                "updated_at": now,
            },
        )
    except Exception:
        pass

    _persist_state(number, conv)
    print(f"🏠 RE lead {number}: score={score}, lead_score={lead_score}, notify={should_notify}")
