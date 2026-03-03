"""
beta_discovery.py
-----------------
Zeli Beta Discovery Mode

Handles a whitelist of phone numbers in a completely separate
conversational flow. Purpose: capture raw use-case signals from
real people before we over-engineer solutions.

Flow per user:
  1. Open-ended intake — Claude listens, asks clarifying questions
     like an attentive support agent (not a form).
  2. Once the problem/situation is clear, Claude acknowledges,
     tells them Zeli is building toward this, and that the team
     will follow up.
  3. Owner gets an instant WhatsApp alert + the full session is
     logged with a structured summary.

Add numbers to BETA_WHITELIST via the BETA_WHITELIST_NUMBERS
environment variable (comma-separated, e.g. "+50764000001,+50764000002")
or hardcode them below for now.
"""

import os
import json
import anthropic
from datetime import datetime
from agent.approval import send_whatsapp   # reuse existing send helper
from utils.logger import log_request       # reuse existing logger


# ── WHITELIST ──────────────────────────────────────────────────────────────────
# Load from env so you can add numbers without redeploying.
# Format in Railway: BETA_WHITELIST_NUMBERS=+50764000001,+50764000002
def _normalize_number(number: str) -> str:
    """Normalize numbers for robust comparisons across +/whatsapp:/spaces formats."""
    if not number:
        return ""
    n = number.strip()
    n = n.replace("whatsapp:", "").replace("+", "")
    n = n.replace(" ", "").replace("-", "")
    return n


def get_beta_whitelist() -> set:
    raw = os.getenv("BETA_WHITELIST_NUMBERS", "")
    # Accept comma/newline/semicolon separators to avoid formatting mistakes.
    for sep in ("\n", ";"):
        raw = raw.replace(sep, ",")
    numbers = {n.strip() for n in raw.split(",") if n.strip()}
    return {_normalize_number(n) for n in numbers if _normalize_number(n)}


def is_beta_user(number: str) -> bool:
    return _normalize_number(number) in get_beta_whitelist()


# ── IN-MEMORY SESSION STORE ───────────────────────────────────────────────────
# Keyed by phone number. Each session is a list of {"role": ..., "content": ...}
# messages plus metadata.
#
# Structure:
# {
#   "whatsapp:+507...": {
#       "messages": [...],          # full conversation for Claude context
#       "started_at": "2026-...",
#       "signal_captured": False,   # flips to True once we've logged + alerted
#   }
# }
_beta_sessions: dict = {}


def _get_session(number: str) -> dict:
    if number not in _beta_sessions:
        _beta_sessions[number] = {
            "messages": [],
            "started_at": datetime.utcnow().isoformat(),
            "signal_captured": False,
        }
    return _beta_sessions[number]


def _reset_session(number: str):
    """Call this if you want the user to start fresh (e.g. after a long gap)."""
    if number in _beta_sessions:
        del _beta_sessions[number]


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
BETA_SYSTEM_PROMPT = """
Eres el asistente de *Zeli*, una plataforma que ayuda a personas y negocios
a resolver problemas del día a día de forma más fácil y rápida.

Estás hablando con un usuario beta seleccionado personalmente. Tu misión en
esta conversación tiene DOS etapas:

---
ETAPA 1 — ENTENDER EL PROBLEMA (tu prioridad ahora)

Escucha con atención lo que el usuario necesita o quiere resolver.
Haz preguntas de seguimiento naturales, como lo haría un buen agente de
atención al cliente: una pregunta a la vez, sin abrumar, con curiosidad genuina.

Quieres entender:
- ¿Qué problema o situación tiene?
- ¿Con qué frecuencia le pasa?
- ¿Cómo lo resuelve hoy (si es que lo resuelve)?
- ¿Qué haría que la solución fuera perfecta para él/ella?

No tienes que hacer TODAS estas preguntas explícitamente. Déjalo fluir de
manera conversacional. Cuando sientas que tienes un panorama claro del
problema, pasa a la Etapa 2.

---
ETAPA 2 — CERRAR CON VALOR (cuando ya entendiste el problema)

Una vez que el problema esté claro:
1. Resume brevemente lo que entendiste, para que el usuario sienta que
   lo escuchaste bien.
2. Dile honestamente que Zeli está trabajando en soluciones para este
   tipo de situaciones.
3. Dile que el equipo de Zeli le va a dar seguimiento personalmente.
4. Despídete de forma cálida.

IMPORTANTE: En la Etapa 2, incluye al FINAL de tu mensaje, en una línea
separada, el siguiente bloque JSON (invisible para el usuario en
producción, pero necesario para el sistema):

<<SIGNAL>>
{
  "problema_resumido": "...",
  "frecuencia": "...",
  "solucion_actual": "...",
  "nivel_dolor": "alto/medio/bajo",
  "categoria_potencial": "..."
}
<</SIGNAL>>

---
REGLAS GENERALES:
- Responde siempre en español, tono amigable y natural (tuteo panameño).
- Nunca uses jerga extranjera que suene artificial ("al toque", "che", etc.).
- Nunca prometas una fecha o funcionalidad específica.
- Nunca menciones que estás capturando datos o que eres parte de un
  experimento. Eres simplemente el asistente de Zeli.
- Sé breve. Máximo 3-4 oraciones por mensaje.
"""


# ── CLAUDE CALL ───────────────────────────────────────────────────────────────
def _call_claude(messages: list) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=400,
        system=BETA_SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text.strip()


# ── SIGNAL EXTRACTION ─────────────────────────────────────────────────────────
def _extract_signal(text: str) -> dict | None:
    """
    Parses the <<SIGNAL>>...<</ SIGNAL>> block Claude appends when
    it decides the problem is well understood.
    Returns the dict if found, None otherwise.
    """
    start = text.find("<<SIGNAL>>")
    end = text.find("<</SIGNAL>>")
    if start == -1 or end == -1:
        return None
    raw_json = text[start + len("<<SIGNAL>>"):end].strip()
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return None


def _strip_signal_block(text: str) -> str:
    """Remove the signal block before sending to user."""
    start = text.find("<<SIGNAL>>")
    if start == -1:
        return text
    return text[:start].strip()


# ── OWNER ALERT ───────────────────────────────────────────────────────────────
def _alert_owner(number: str, signal: dict, conversation: list):
    owner = os.getenv("YOUR_PERSONAL_WHATSAPP")
    if not owner:
        return

    clean_number = number.replace("whatsapp:", "")

    # Build a readable transcript (last 10 turns max to keep it tight)
    recent = conversation[-10:]
    transcript_lines = []
    for msg in recent:
        role_label = "👤 Usuario" if msg["role"] == "user" else "🤖 Zeli"
        transcript_lines.append(f"{role_label}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    alert = (
        f"🧪 *Nueva señal beta capturada*\n"
        f"Número: {clean_number}\n\n"
        f"📋 *Problema:* {signal.get('problema_resumido', 'N/A')}\n"
        f"🔁 *Frecuencia:* {signal.get('frecuencia', 'N/A')}\n"
        f"🛠️ *Solución actual:* {signal.get('solucion_actual', 'N/A')}\n"
        f"🌡️ *Nivel de dolor:* {signal.get('nivel_dolor', 'N/A')}\n"
        f"📂 *Categoría potencial:* {signal.get('categoria_potencial', 'N/A')}\n\n"
        f"💬 *Conversación reciente:*\n{transcript}"
    )

    send_whatsapp(owner, alert)
    print(f"📡 Beta signal alerted to owner for {clean_number}")


# ── STRUCTURED LOG ────────────────────────────────────────────────────────────
def _log_signal(number: str, signal: dict, session: dict):
    log_request({
        "type": "beta_signal",
        "beta_number": number.replace("whatsapp:", ""),
        "started_at": session["started_at"],
        "captured_at": datetime.utcnow().isoformat(),
        "signal": signal,
        "message_count": len(session["messages"]),
    })


# ── MAIN ENTRY POINT ──────────────────────────────────────────────────────────
def handle_beta_message(number: str, message: str) -> None:
    """
    Called from app.py webhook for any whitelisted beta number.
    Manages the full session lifecycle and sends WhatsApp responses.
    """
    session = _get_session(number)

    # Append user message to history
    session["messages"].append({"role": "user", "content": message})

    # Get Claude's response
    try:
        raw_reply = _call_claude(session["messages"])
    except Exception as e:
        print(f"❌ Beta Claude error for {number}: {e}")
        send_whatsapp(
            number,
            "Disculpa, tuve un problema técnico. ¿Me repites lo que necesitas? 🙏"
        )
        return

    # Check if Claude decided it has enough signal
    signal = _extract_signal(raw_reply)
    clean_reply = _strip_signal_block(raw_reply)

    # Append assistant turn to history (store clean version)
    session["messages"].append({"role": "assistant", "content": clean_reply})

    # Send reply to user
    send_whatsapp(number, clean_reply)

    # If signal captured for the first time, alert owner + log
    if signal and not session["signal_captured"]:
        session["signal_captured"] = True
        _alert_owner(number, signal, session["messages"])
        _log_signal(number, signal, session)
        print(f"✅ Beta signal captured and logged for {number}")
