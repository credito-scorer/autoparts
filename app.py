import os
import time
import traceback
import hmac
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from enum import Enum
from flask import Flask, request, jsonify, make_response, redirect
from dotenv import load_dotenv
from agent.parser import (
    parse_request_multi, extract_partial, extract_vehicle_for_part,
    parse_correction, interpret_option_choice, detect_needs_human,
    MODEL_TO_MAKE, resolve_make_model
)
from agent.sourcing import source_parts
from agent.recommender import build_options
from agent.approval import send_for_approval, handle_approval, send_whatsapp, send_whatsapp_image
from agent.responder import (
    generate_response, generate_quote_presentation,
    generate_queue_confirmation, generate_multi_sourcing_summary,
    GOODBYE_COMPLETED, GOODBYE_MID_FLOW,
)
from utils.logger import log_request
from utils.dashboard import render_dashboard
from connectors.sheets import get_order_log
from utils.followup import (
    schedule_followup, cancel_followup,
    schedule_long_wait_alert, cancel_long_wait_alert,
)
from utils import monitor
from connectors.whatsapp_supplier import (
    handle_supplier_response,
    get_registered_suppliers
)
from connectors.local_store import (
    get_store_numbers,
    handle_store_message,
    handle_owner_reply_to_store,
    store_message_map,
    store_message_map_lock
)
from beta_discovery import is_beta_user, handle_beta_message

load_dotenv()

STARTUP_TIME = monitor.panama_now()

app = Flask(__name__)

# Thread-safe state
_state_lock = threading.RLock()

pending_approvals      = {}
pending_selections     = {}
approval_message_map   = {}
escalation_message_map = {}
live_sessions          = {}
pending_live_offers    = {}
# TTL cache: 24h retention, ~10k slots — prevents unbounded growth + idempotency gap
try:
    from cachetools import TTLCache
    processing_messages = TTLCache(maxsize=10000, ttl=86400)
except ImportError:
    processing_messages = {}  # fallback: plain dict (same interface as TTLCache)
_startup_notified      = False   # send deploy notification once on boot


def _verify_meta_signature(raw_body: bytes, header_sig: str | None, app_secret: str) -> bool:
    """Verify Meta webhook X-Hub-Signature-256 HMAC. Returns True if valid."""
    if not header_sig or not header_sig.startswith("sha256="):
        return False
    if not app_secret:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_sig)


def _send_error_alert(endpoint: str, exc: Exception) -> None:
    """Delegate webhook crash alerts to the central monitor module."""
    msg = (
        f"⚠️ *Zeli Bot Error*\n\n"
        f"📍 Endpoint: {endpoint}\n"
        f"❌ Error: {type(exc).__name__}: {exc}\n"
        f"🕐 Hora: {monitor.panama_now()}\n\n"
        f"Revisa Railway logs para el traceback completo."
    )
    monitor.send_alert(f"webhook_error_{type(exc).__name__}", msg, cooldown=60)
    monitor.increment_stat("errors")


def _send_startup_notification_once() -> None:
    """Send deploy/online notification immediately after boot."""
    global _startup_notified
    with _state_lock:
        if _startup_notified:
            return
        _startup_notified = True

    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP", "")
    if not owner_number:
        return

    send_whatsapp(
        owner_number,
        f"✅ *Zeli Bot Online*\n"
        f"🕐 {STARTUP_TIME}\n"
        f"🚀 Producción activa — {os.getenv('RAILWAY_PUBLIC_URL', 'autoparts-production.up.railway.app')}"
    )


# ── Conversation state machine ─────────────────────────────────────────────────

class ConversationState(Enum):
    ACTIVE    = "active"    # Building request queue
    WAITING   = "waiting"   # Sourcing / approval / option selection in progress
    COMPLETED = "completed" # Session ended (purchase confirmed or goodbye)


conversations:    dict = {}
CONVERSATION_TTL  = 10800   # 3 hours
CLEANUP_INTERVAL  = 900     # 15 minutes


def _new_conversation() -> dict:
    return {
        "state":                 ConversationState.ACTIVE,
        "request_queue":         [],
        "confirming":            False,
        "asking_shared_vehicle": False,
        "asking_per_item":       False,
        "current_item_index":    0,
        "dead_end_count":        0,
        "same_field_count":      0,
        "last_missing_field":    None,
        "last_seen":             time.time(),
    }


def _get_or_create_conversation(number: str) -> dict:
    with _state_lock:
        conv = conversations.get(number)
        # Treat expired or completed conversations as fresh starts
        if conv is None or conv["state"] == ConversationState.COMPLETED:
            conv = _new_conversation()
            conversations[number] = conv
            monitor.increment_stat("conversations")
    return conv


def _close_conversation(number: str, mid_flow: bool) -> None:
    """End a conversation cleanly, notify customer, and remove from dict."""
    with _state_lock:
        conversations.pop(number, None)
    cancel_followup(number)
    send_whatsapp(number, GOODBYE_MID_FLOW if mid_flow else GOODBYE_COMPLETED)


def _cleanup_loop() -> None:
    """Background daemon: remove stale conversations, check memory, alert on abandoned."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()

        # Expire stale conversations — alert if they abandoned at confirmation
        with _state_lock:
            expired = [
                (n, c) for n, c in list(conversations.items())
                if now - c["last_seen"] > CONVERSATION_TTL
            ]
            for n, _ in expired:
                conversations.pop(n, None)
        for n, conv in expired:
            if conv.get("confirming") and conv.get("request_queue"):
                req = conv["request_queue"][0]
                try:
                    monitor.alert_abandoned_confirmation(
                        n,
                        req.get("part", "?"), req.get("make", "?"),
                        req.get("model", "?"), req.get("year", "?"),
                    )
                except Exception:
                    pass
        if expired:
            print(f"🧹 Cleaned up {len(expired)} stale conversation(s)")

        # Memory check (Alert 10)
        try:
            mb = monitor.check_memory_mb()
            if mb > 400:
                monitor.alert_high_memory(mb)
        except Exception:
            pass


_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()


# ── Queue helpers ──────────────────────────────────────────────────────────────

def _req_complete(req: dict) -> bool:
    return all(req.get(f) for f in ("part", "make", "model", "year"))


def _queue_all_complete(queue: list) -> bool:
    return bool(queue) and all(_req_complete(r) for r in queue)


def _apply_to_queue(queue: list, update: dict) -> None:
    """Apply field updates to all incomplete items (vehicle info is shared)."""
    for item in queue:
        if not _req_complete(item):
            for k, v in update.items():
                if k in ("part", "make", "model", "year") and v and not item.get(k):
                    item[k] = str(v).strip()


def _known_from_queue(queue: list) -> dict:
    """Return known fields from the first incomplete item."""
    for item in queue:
        if not _req_complete(item):
            return {k: v for k, v in item.items() if v and k in ("part", "make", "model", "year")}
    return {}


def _missing_from_queue(queue: list) -> list:
    """Return missing fields from the first incomplete item."""
    for item in queue:
        if not _req_complete(item):
            return [k for k in ("part", "make", "model", "year") if not item.get(k)]
    return []


def _vehicle_str(queue: list) -> str:
    """Build a vehicle description string from queue items."""
    for req in queue:
        parts = [req.get("make"), req.get("model"), req.get("year")]
        if any(parts):
            return " ".join(p for p in parts if p)
    return ""


def _enqueue_requests(conv: dict, new_requests: list) -> None:
    """Append new parsed requests to the conversation queue."""
    for new_req in new_requests:
        item = {k: str(new_req.get(k) or "").strip() for k in ("part", "make", "model", "year")}
        conv["request_queue"].append(item)
    conv["last_seen"] = time.time()


# ── Phrase detection ───────────────────────────────────────────────────────────

GREETINGS = ["hola", "buenas", "buenos dias", "buenos días", "buenas tardes",
             "buenas noches", "hi", "hello", "hey"]

SECONDARY_GREETINGS = ["que tal", "qué tal", "como estas", "cómo estás",
                       "como estás", "cómo estas", "todo bien", "que hay"]

WAIT_PHRASES = [
    "dame un segundo", "un momento", "un seg", "espera", "espérate",
    "ahorita te digo", "ahorita", "déjame revisar", "dejame revisar",
    "déjame ver", "dejame ver", "ya vuelvo", "un momentito"
]

ACK_PHRASES = [
    "ok", "okey", "okay", "entendido", "perfecto", "listo", "bueno",
    "ah ok", "ah okey", "ya veo", "ya", "claro", "dale", "va",
    "de acuerdo", "10 puntos", "excelente", "genial"
]

VAGUE_INTENT = [
    "si necesito", "sí necesito", "necesito unas", "necesito algo",
    "busco unas", "quiero unas", "quiero una", "quiero un",
    "tengo que buscar", "necesito piezas", "necesito repuestos",
    "necesito varios", "si tengo", "sí tengo", "tengo varios",
    "tengo unas", "no entiendo", "no sé cómo", "no se como",
    "si", "sí"
]

PART_KEYWORDS = [
    "pieza", "repuesto", "parte", "necesito", "neceisto", "nececito",
    "busco", "quiero", "tienen", "tienes", "hay ", "consiguen"
]

HUMAN_REQUEST = [
    "con alguien", "hablar con", "un agente", "una persona", "con una persona",
    "con un humano", "con el dueño", "con el encargado", "me pueden llamar",
    "me pueden contactar", "quiero hablar", "necesito hablar", "llamenme",
    "llámenme", "me llaman", "por favor alguien", "alguien me ayude",
    "alguien que trabaje"
]

VAGUE_PARTS = {
    "motor", "pieza", "parte", "cosa", "repuesto",
    "eso", "una pieza", "algo", "una parte",
}

GOODBYE_PHRASES = {
    "gracias", "muchas gracias", "mil gracias", "ok gracias", "okey gracias",
    "gracias!", "gracias!!", "ty", "thanks", "thank you",
    "hasta luego", "hasta pronto", "bye", "chao", "chau", "adios", "adiós",
    "nos vemos", "cuídate", "cuídate", "que te vaya bien",
    "ya no necesito", "no gracias", "dejalo", "déjalo", "olvídalo", "olvidalo",
}

FRUSTRATION_PHRASES = [
    "no me entiendes", "no entiende", "esto no funciona", "qué clase de bot",
    "mal bot", "no sirve", "inútil", "no te entiendo", "estás loco",
    "estas loco", "no sé qué", "no se que", "ayuda", "auxilio",
    "qué es esto", "que es esto", "no funciona", "pésimo", "pesimo",
    "no entiendo", "no entiendo esto", "no entiendo nada", "no comprendo",
    "que cosa", "qué cosa", "q cosa", "de que hablas", "de qué hablas",
    "q es esto", "qué es eso", "que es eso", "esto que es", "esto qué es",
    "no tiene sentido", "no sé", "no se", "perdido", "confundido",
]


def is_greeting(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(g) for g in GREETINGS)


def is_secondary_greeting(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(g) for g in SECONDARY_GREETINGS)


def is_wait(message: str) -> bool:
    msg = message.lower().strip()
    return any(msg.startswith(w) for w in WAIT_PHRASES)


def is_ack(message: str) -> bool:
    msg = message.lower().strip()
    return msg in ACK_PHRASES


def is_vague_intent(message: str) -> bool:
    msg = message.lower().strip()
    if any(msg.startswith(v) for v in VAGUE_INTENT):
        return True
    return any(keyword in msg for keyword in PART_KEYWORDS)


def is_human_request(message: str) -> bool:
    msg = message.lower().strip()
    return any(phrase in msg for phrase in HUMAN_REQUEST)


def is_goodbye(message: str) -> bool:
    msg = message.lower().strip()
    return msg in GOODBYE_PHRASES


def is_frustration(message: str) -> bool:
    msg = message.lower().strip()
    return any(phrase in msg for phrase in FRUSTRATION_PHRASES)


def _is_affirmative(message: str) -> bool:
    """
    Detect yes/confirmation in a WhatsApp message.
    Handles exact matches, si+i variants (sii, siii, síii…), and
    affirmation-prefixed phrases (dale pues, ok perfecto, sí claro, etc.).
    """
    msg = message.lower().strip().rstrip("!")
    # Exact matches
    if msg in {
        "sí", "si", "dale", "ok", "okey", "correcto", "listo",
        "yes", "sip", "claro", "bueno", "bien", "va", "exacto", "eso", "ese",
        "ta bien", "está bien", "esta bien",
        "perfecto", "excelente", "genial",
    }:
        return True
    # "sii", "siii", "síi", "síii", etc. — strip trailing i's
    base = msg.rstrip("i")
    if base in ("s", "si", "sí"):
        return True
    # Affirmation + trailing words: "dale pues", "ok perfecto", "sí correcto"
    return any(msg.startswith(p + " ") for p in (
        "sí", "si", "dale", "ok", "okey", "claro", "correcto", "sip", "listo",
        "bien", "ta bien", "está bien", "esta bien"
    ))


# ── Escalation helper ──────────────────────────────────────────────────────────

def _handle_human_escalation(
    number: str, message: str,
    reason: str = "cliente solicitó hablar con una persona"
) -> None:
    """Start a live session and notify the owner."""
    print(f"🔴 _handle_human_escalation called for {number}")
    with _state_lock:
        conversations.pop(number, None)
        live_sessions[number] = True
    cancel_followup(number)
    print(f"🔴 Setting live_sessions[{number}] = True")
    print(f"🔴 live_sessions after set: {list(live_sessions.keys())}")

    owner_number = os.getenv("YOUR_PERSONAL_WHATSAPP")
    print(f"🔴 Owner number: {owner_number}")
    if owner_number:
        msg_sid = send_whatsapp(
            owner_number,
            f"⚠️ *Escalación automática*\n"
            f"Cliente: {number}\n"
            f"Motivo: {reason}\n"
            f"Último mensaje: \"{message}\"\n"
            f"🕐 {monitor.panama_now()}\n\n"
            f"Responde a este mensaje para hablarle directamente. "
            f"Escribe *fin* para terminar la sesión."
        )
        print(f"🔴 msg_sid returned: {msg_sid}")
        if msg_sid:
            escalation_message_map[msg_sid] = number
            print(f"📋 Live session mapped: {msg_sid} → {number}")

    send_whatsapp(number, "Un momento, ya te contacta alguien del equipo. 👍")


# ── Missing-fields prompt ──────────────────────────────────────────────────────

def _send_queue_missing_prompt(number: str, message: str, conv: dict) -> None:
    """Ask for the next missing field in the queue."""
    queue   = conv["request_queue"]
    known   = _known_from_queue(queue)
    missing = _missing_from_queue(queue)
    if not missing:
        return

    # Track how many times we've asked for the same field; escalate after 3
    next_field = missing[0]
    if next_field == conv.get("last_missing_field"):
        conv["same_field_count"] = conv.get("same_field_count", 0) + 1
    else:
        conv["same_field_count"]   = 0
        conv["last_missing_field"] = next_field
    if conv["same_field_count"] >= 3:
        _handle_human_escalation(
            number, message, reason="mismo campo repetido 3 veces"
        )
        return

    is_first = (
        len(queue) == 1
        and not known.get("make")
        and not known.get("model")
        and not known.get("year")
    )
    send_whatsapp(number, generate_response("missing_fields", message, {
        "known":            known,
        "missing":          missing,
        "is_first_message": is_first,
    }))


# ── Multi-sourcing ─────────────────────────────────────────────────────────────

def _run_multi_sourcing(number: str, message: str, queue: list) -> None:
    """Source all parts in the queue in parallel, then send approvals / not-found notices."""
    count = len(queue)
    noun  = "pieza" if count == 1 else "piezas"
    send_whatsapp(
        number,
        f"🔩 Recibido. Estamos buscando {'tu' if count == 1 else 'tus'} {count} {noun}, "
        f"te confirmamos en unos minutos. ⏳"
    )
    schedule_followup(number, delay=300)

    for req in queue:
        log_request({
            "customer_number": number,
            "raw_message":     message,
            "parsed":          req,
            "status":          "received",
        })

    found     = []   # [(req, options)]
    not_found = []   # [req]

    with ThreadPoolExecutor(max_workers=min(count, 4)) as ex:
        future_to_req = {ex.submit(source_parts, req): req for req in queue}
        try:
            for fut in as_completed(future_to_req, timeout=35):  # global wait window
                req = future_to_req[fut]
                try:
                    results = fut.result()
                    if results:
                        found.append((req, build_options(results, req)))
                    else:
                        not_found.append(req)
                except Exception as e:
                    print(f"⚠️ source_parts error for {req.get('part')}: {e}")
                    not_found.append(req)
        except FutureTimeoutError:
            # Handle unfinished futures without aborting the full sourcing flow
            for fut, req in future_to_req.items():
                if not fut.done():
                    print(f"⏱️ Sourcing timeout: {req.get('part')}")
                    monitor.alert_sourcing_timeout(
                        req.get("part", "?"), req.get("make", "?"),
                        req.get("model", "?"), req.get("year", "?"),
                        number,
                    )
                    not_found.append(req)

    cancel_followup(number)

    # Notify about parts not found (Alert 6) + customer summary
    if not_found:
        vehicle = _vehicle_str(queue)
        send_whatsapp(number, generate_multi_sourcing_summary(found, not_found, vehicle))
        for req in not_found:
            log_request({
                "customer_number": number,
                "raw_message":     message,
                "parsed":          req,
                "status":          "not_found",
            })
            monitor.alert_part_not_found(
                req.get("part", "?"), req.get("make", "?"),
                req.get("model", "?"), req.get("year", "?"),
                number,
            )

    # Send approval requests for found parts; schedule 10-min waiting alert
    for req, options in found:
        send_for_approval(options, req, number, pending_approvals, approval_message_map)
        schedule_long_wait_alert(number, req, delay=600)
        log_request({
            "customer_number": number,
            "raw_message":     message,
            "parsed":          req,
            "options":         options,
            "status":          "pending_approval",
        })

    # If nothing was found at all, reset the conversation so customer can try again
    if not found:
        conv = conversations.get(number)
        if conv:
            conv["state"]                 = ConversationState.ACTIVE
            conv["request_queue"]         = []
            conv["confirming"]            = False
            conv["asking_shared_vehicle"] = False
            conv["asking_per_item"]       = False
            conv["current_item_index"]    = 0
            conv["dead_end_count"]        = 0
            conv["same_field_count"]      = 0
            conv["last_missing_field"]    = None
        send_whatsapp(number, "¿Necesitas algo más? Si tienes otra pieza o vehículo, cuéntame.")


# ── Main customer request handler ──────────────────────────────────────────────

def process_customer_request(number: str, message: str) -> None:
    conv  = _get_or_create_conversation(number)
    queue = conv["request_queue"]

    print(f"💬 State for {number}: state={conv['state'].value} queue={len(queue)}")

    # ── Goodbye detection ──────────────────────────────────────────────────────
    if is_goodbye(message):
        _close_conversation(number, mid_flow=bool(queue))
        return

    # ── WAITING guard — sourcing / approval in progress ────────────────────────
    if conv["state"] == ConversationState.WAITING:
        send_whatsapp(
            number,
            "Un momento, ya estamos buscando tu pieza. 🔍\n"
            "Te avisamos en cuanto tengamos la cotización. ⏳"
        )
        return

    # ── Frustration detection ───────────────────────────────────────────────────
    if is_frustration(message):
        if queue or conv.get("dead_end_count", 0) > 0:
            _handle_human_escalation(number, message, reason="señales de frustración")
            return
        # Empty queue, no history — treat as confused new contact, fall through to normal routing

    # ── Human request — must be checked BEFORE parse_request_multi ─────────────
    if is_human_request(message):
        _handle_human_escalation(number, message)
        return

    # ── Try to parse as part request ───────────────────────────────────────────
    new_requests = parse_request_multi(message)

    # Catch overly broad part names before they enter the queue
    if new_requests:
        specific = [r for r in new_requests
                    if (r.get("part") or "").lower().strip() not in VAGUE_PARTS]
        if len(specific) < len(new_requests) and not specific and not queue:
            # Save any vehicle info from the vague message before rejecting it
            for r in new_requests:
                vehicle = {k: v for k, v in r.items()
                           if k in ("make", "model", "year") and v}
                if vehicle:
                    _enqueue_requests(conv, [{"part": None, "make": vehicle.get("make"),
                                              "model": vehicle.get("model"),
                                              "year": vehicle.get("year")}])
                    break
            send_whatsapp(
                number,
                "¿Qué parte específica necesitas? Por ejemplo: "
                "alternador, filtro de aceite, pastillas de freno, bomba de agua."
            )
            return
        new_requests = specific

    # Infer make from model for every parsed item before grouping
    # (e.g. Claude returns make=null + model=Yaris → resolves to Toyota Yaris)
    for item in new_requests:
        resolve_make_model(item, message)

    # Raw-message model scan fallback — handles "hilux", "una corolla", etc.
    # parse_request_multi returns [] when there is no part mentioned, so Claude
    # never produces an item for resolve_make_model to operate on.  Scan the
    # raw message directly for any known vehicle model and create a part-less
    # queue entry so the bot asks for the part rather than treating the message
    # as conversational.
    if not new_requests and not queue:
        raw_lower = message.lower().strip()
        raw_words = set(raw_lower.split())
        for key in sorted(MODEL_TO_MAKE, key=len, reverse=True):
            key_lower = key.lower()
            matched = (key_lower in raw_lower) if " " in key_lower else (key_lower in raw_words)
            if matched:
                new_requests = [{"part": None, "make": MODEL_TO_MAKE[key],
                                 "model": key, "year": None}]
                print(f"🚗 Model scan fallback: found '{key}' in message")
                break

    # ── Single-vehicle guard: keep only the first vehicle group ───────────────
    # If the customer mentioned parts for multiple vehicles in one message,
    # only take the first vehicle group now. They will bring up the next
    # vehicle naturally after the first is handled.
    if new_requests and len(new_requests) > 1:
        def _vehicle_key(r):
            make  = (r.get("make")  or "").lower().strip()
            model = (r.get("model") or "").lower().strip()
            return (make, model) if (make or model) else None

        first_key = None
        filtered  = []
        for r in new_requests:
            k = _vehicle_key(r)
            if k is None or first_key is None or k == first_key:
                if first_key is None and k is not None:
                    first_key = k
                filtered.append(r)
        if len(filtered) < len(new_requests):
            discarded = len(new_requests) - len(filtered)
            print(f"🚗 Multi-vehicle message: keeping first group, discarding {discarded} item(s)")
        new_requests = filtered

    if new_requests:
        if any(not _req_complete(r) for r in queue):
            # Incomplete items already in queue — merge, don't blindly append.
            # This prevents duplicate entries when the customer repeats a part name.
            queued_parts = {(r.get("part") or "").lower() for r in queue}
            for new_req in new_requests:
                new_part = (new_req.get("part") or "").lower()
                updates = {k: v for k, v in new_req.items()
                           if v and k in ("part", "make", "model", "year")}
                # Always merge any new field values into existing incomplete items
                if updates:
                    _apply_to_queue(queue, updates)
                # Only append if this is a part that isn't already in the queue
                if new_part and new_part not in queued_parts:
                    item = {k: str(new_req.get(k) or "").strip()
                            for k in ("part", "make", "model", "year")}
                    queue.append(item)
                    queued_parts.add(new_part)
            for entry in queue:
                if not _req_complete(entry):
                    print(f"📝 Updated queue entry: {entry}")
                    break
            conv["last_seen"]          = time.time()
            conv["dead_end_count"]     = 0
            conv["same_field_count"]   = 0
            conv["last_missing_field"] = None
        else:
            _enqueue_requests(conv, new_requests)
            conv["dead_end_count"]     = 0
            conv["same_field_count"]   = 0
            conv["last_missing_field"] = None

    elif queue:
        # No new part found — try to extract vehicle/part fields from this message
        known   = _known_from_queue(queue)
        partial = extract_partial(message, known) if known else None

        if partial:
            _apply_to_queue(queue, partial)
            for item in queue:
                if not item.get("make") and item.get("model"):
                    resolve_make_model(item, message)
            conv["last_seen"]          = time.time()
            conv["dead_end_count"]     = 0
            conv["same_field_count"]   = 0
            conv["last_missing_field"] = None
            for entry in queue:
                if not _req_complete(entry):
                    print(f"📝 Updated queue entry: {entry}")
                    break
        else:
            # Conversational message while mid-request
            if is_wait(message):
                send_whatsapp(number, generate_response("wait_acknowledgment", message))
                return
            # Re-prompt for the missing field
            _send_queue_missing_prompt(number, message, conv)
            return

    else:
        # No queue — purely conversational
        if is_human_request(message):
            _handle_human_escalation(number, message)
        elif is_greeting(message):
            send_whatsapp(number, generate_response("greeting", message))
        elif is_secondary_greeting(message):
            send_whatsapp(number, generate_response("secondary_greeting", message))
        elif is_wait(message):
            send_whatsapp(number, generate_response("wait_acknowledgment", message))
        elif is_ack(message):
            send_whatsapp(number, generate_response("ack", message))
        elif is_vague_intent(message):
            send_whatsapp(number, generate_response("vague_intent", message))
        elif detect_needs_human(message):
            _handle_human_escalation(
                number, message, reason="señales de frustración/confusión"
            )
        else:
            conv["dead_end_count"] = conv.get("dead_end_count", 0) + 1
            if conv["dead_end_count"] >= 2:
                _handle_human_escalation(
                    number, message, reason="mensajes sin respuesta útil"
                )
            else:
                send_whatsapp(number, generate_response("unknown", message))
        return

    # ── Check if all queue items are complete ──────────────────────────────────
    if _queue_all_complete(conv["request_queue"]):
        conv["confirming"] = True
        conv["last_seen"]  = time.time()
        send_whatsapp(number, generate_queue_confirmation(conv["request_queue"]))
    elif (
        len(conv["request_queue"]) >= 2
        and not conv.get("asking_shared_vehicle")
        and not conv.get("asking_per_item")
        and all(
            not item.get("make") and not item.get("model") and not item.get("year")
            for item in conv["request_queue"]
        )
    ):
        # 2+ parts, all missing vehicle — ask whether they share a vehicle
        conv["asking_shared_vehicle"] = True
        send_whatsapp(
            number,
            "¿Son todas las piezas para el mismo vehículo, o son para carros diferentes? 🚗"
        )
    else:
        _send_queue_missing_prompt(number, message, conv)


# ── Image relay ────────────────────────────────────────────────────────────────

def _normalize_number(n: str) -> str:
    """Strip all non-digit characters for safe phone number comparison."""
    return n.replace("whatsapp:", "").replace("+", "").replace(" ", "").replace("-", "").strip()


def _handle_image_relay(message: dict) -> None:
    """Relay image messages bidirectionally between customers/stores and the owner."""
    import traceback as _tb
    from utils.media import download_meta_media, upload_meta_media

    incoming_number     = "+" + message["from"]
    replied_to_sid      = message.get("context", {}).get("id")
    owner_raw           = os.getenv("YOUR_PERSONAL_WHATSAPP", "")
    owner_number        = "+" + _normalize_number(owner_raw)
    incoming_normalized = _normalize_number(incoming_number)
    owner_normalized    = _normalize_number(owner_number)

    image_info = message.get("image", {})
    media_id   = image_info.get("id")
    mime_type  = image_info.get("mime_type", "image/jpeg")
    caption    = image_info.get("caption", "")

    print(f"📸 Image relay triggered")
    print(f"📸 From: {incoming_number}")
    print(f"📸 Media ID: {media_id}")
    print(f"📸 Owner number: {owner_number}")
    print(f"📸 Caption: {repr(caption)}")
    print(f"📸 Reply-to SID: {replied_to_sid}")

    if not media_id:
        print("📸 No media_id found — skipping relay")
        return

    # ── OWNER → forward image to customer or store ──────────────────────────
    if incoming_normalized == owner_normalized:
        with store_message_map_lock:
            store_number = store_message_map.get(replied_to_sid)
        if replied_to_sid and store_number:
            print(f"📸 Owner→store relay to {store_number}")
            try:
                img_bytes, _ = download_meta_media(media_id)
                new_id = upload_meta_media(img_bytes, mime_type)
                send_whatsapp_image(store_number, new_id, caption)
                send_whatsapp(owner_number, "✅ Imagen enviada a la tienda.")
            except Exception as e:
                _tb.print_exc()
                print(f"❌ Image relay owner→store failed: {e}")
                send_whatsapp(owner_number, f"⚠️ No se pudo enviar la imagen a la tienda: {e}")
            return

        if replied_to_sid and replied_to_sid in escalation_message_map:
            customer_number = escalation_message_map[replied_to_sid]
            print(f"📸 Owner→customer relay to {customer_number}")
            try:
                img_bytes, _ = download_meta_media(media_id)
                new_id = upload_meta_media(img_bytes, mime_type)
                send_whatsapp_image(customer_number, new_id, caption)
                escalation_message_map.pop(replied_to_sid, None)
                send_whatsapp(owner_number, "✅ Imagen enviada al cliente.")
            except Exception as e:
                _tb.print_exc()
                print(f"❌ Image relay owner→customer failed: {e}")
                send_whatsapp(owner_number, f"⚠️ No se pudo enviar la imagen al cliente: {e}")
            return

        # Owner image with no recognized reply-to context — ignore
        print("📸 Owner image with no mapped reply-to — ignored")
        return

    # ── STORE → forward image to owner ─────────────────────────────────────
    if incoming_number in get_store_numbers():
        label = f"🏪 *Imagen de tienda {incoming_number}*"
        if caption:
            label += f"\n_{caption}_"
        print(f"📸 Store→owner relay from {incoming_number}")
        try:
            img_bytes, _ = download_meta_media(media_id)
            new_id = upload_meta_media(img_bytes, mime_type)
            msg_sid = send_whatsapp_image(owner_number, new_id, label)
            if msg_sid:
                with store_message_map_lock:
                    store_message_map[msg_sid] = incoming_number
                print(f"📸 Store image relayed → owner (sid={msg_sid})")
        except Exception as e:
            _tb.print_exc()
            print(f"❌ Image relay store→owner failed: {e}")
            send_whatsapp(
                owner_number,
                f"⚠️ La tienda {incoming_number} envió una imagen pero no pudo retransmitirse: {e}"
            )
        return

    # ── CUSTOMER → forward image to owner ──────────────────────────────────
    label = f"📸 *Imagen de cliente {incoming_number}*"
    if caption:
        label += f"\n_{caption}_"
    print(f"📸 Customer→owner relay from {incoming_number}")
    try:
        img_bytes, _ = download_meta_media(media_id)
        new_id = upload_meta_media(img_bytes, mime_type)
        msg_sid = send_whatsapp_image(owner_number, new_id, label)
        if msg_sid:
            escalation_message_map[msg_sid] = incoming_number
            print(f"📸 Customer image relayed → owner (sid={msg_sid})")
        with _state_lock:
            was_live = incoming_number in live_sessions
            live_sessions[incoming_number] = True
        # Only announce/start live mode once; avoid duplicate owner alerts on later images.
        if not was_live:
            send_whatsapp(incoming_number, "Un momento, ya te contacta alguien del equipo. 👍")
            send_whatsapp(
                owner_number,
                f"💬 Cliente en modo en vivo: {incoming_number}. "
                f"Sus mensajes te llegan directo. Responde aquí para hablarle."
            )
            print(f"🔴 Live session started via image for {incoming_number}")
    except Exception as e:
        _tb.print_exc()
        print(f"❌ Image relay customer→owner failed: {e}")


# ── Webhook ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Meta webhook verification handshake."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == os.getenv("META_VERIFY_TOKEN"):
        print("✅ Webhook verified by Meta")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        return _webhook_handler()
    except Exception as exc:
        traceback.print_exc()
        _send_error_alert("/webhook", exc)
        return jsonify({"status": "ok"}), 200


def _webhook_handler():
    # Verify Meta webhook signature before processing
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_secret:
        print("⚠️ META_APP_SECRET missing; rejecting webhook")
        return jsonify({"status": "forbidden"}), 403
    raw_body = request.get_data()
    header_sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_meta_signature(raw_body, header_sig, app_secret):
        print("⚠️ Webhook signature verification failed")
        return jsonify({"status": "forbidden"}), 403
    data = request.get_json()

    try:
        value = data["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return jsonify({"status": "ok"}), 200

    if "messages" not in value:
        return jsonify({"status": "ok"}), 200

    # Alert 9 — high volume spike
    msg_count = monitor.track_message()
    if msg_count > 20:
        monitor.alert_high_volume(msg_count)

    message = value["messages"][0]

    msg_id = message.get("id")
    with _state_lock:
        if msg_id and msg_id in processing_messages:
            print(f"🔁 Duplicate message blocked: {msg_id}")
            return jsonify({"status": "ok"}), 200
        if msg_id:
            processing_messages[msg_id] = True  # TTLCache or dict

    if message.get("type") == "image":
        threading.Thread(target=_handle_image_relay, args=(message,), daemon=True).start()
        return jsonify({"status": "ok"}), 200

    if message.get("type") != "text":
        return jsonify({"status": "ok"}), 200

    incoming_number  = "+" + message["from"]
    incoming_message = message.get("text", {}).get("body", "").strip()
    replied_to_sid   = message.get("context", {}).get("id")

    print(f"\n📨 Message from {incoming_number}: {incoming_message}")

    owner_number        = os.getenv("YOUR_PERSONAL_WHATSAPP", "").replace("whatsapp:", "").replace("+", "").strip()
    owner_number        = "+" + owner_number
    incoming_normalized = incoming_number.replace("+", "").strip()

    # 1. OWNER → Approval or reply-forwarding flow
    if incoming_normalized == owner_number.replace("+", ""):
        # Reply to a store message → route back to store
        if replied_to_sid:
            with store_message_map_lock:
                store_number = store_message_map.get(replied_to_sid)
            if store_number:
                handle_owner_reply_to_store(store_number, incoming_message, replied_to_sid)
                send_whatsapp(owner_number, "✅ Mensaje enviado a la tienda.")
                return jsonify({"status": "ok"}), 200

        # Reply to a live session / escalation message
        customer_number = None
        if replied_to_sid:
            with _state_lock:
                customer_number = escalation_message_map.get(replied_to_sid)
        if customer_number:

            if incoming_message.strip().lower() == "fin":
                with _state_lock:
                    live_sessions.pop(customer_number, None)
                send_whatsapp(
                    customer_number,
                    "Fue un gusto atenderte. Si necesitas algo más, aquí estamos. 👋\n\n"
                    "Para buscar un repuesto: Pieza + marca + modelo + año"
                )
                print(f"🟢 Live session ended for {customer_number}")
                send_whatsapp(owner_number, f"✅ Sesión terminada. Bot activo para {customer_number}.")
                return jsonify({"status": "ok"}), 200

            send_whatsapp(customer_number, f"💬 *Zeli:*\n{incoming_message}")
            with _state_lock:
                escalation_message_map.pop(replied_to_sid, None)
                live_sessions[customer_number] = True  # keep routing to owner
            print(f"📤 Forwarded owner reply to {customer_number}: {incoming_message}")
            send_whatsapp(owner_number, "✅ Mensaje enviado al cliente.")
            return jsonify({"status": "ok"}), 200

        # Manual live session command: "tomar +56912345678"
        if incoming_message.lower().startswith("tomar "):
            parts       = incoming_message.strip().split()
            raw_number  = parts[1] if len(parts) > 1 else ""
            if not raw_number.startswith("+"):
                raw_number = "+" + raw_number
            with _state_lock:
                live_sessions[raw_number] = True
            send_whatsapp(
                raw_number,
                "Hola, en un momento alguien del equipo de Zeli te contacta. 👋"
            )
            print(f"🔴 Manual live session started for {raw_number}")
            send_whatsapp(owner_number, f"🔴 Sesión en vivo iniciada con {raw_number}.")
            return jsonify({"status": "ok"}), 200

        # Manual end live session command: "terminar +56912345678"
        if incoming_message.lower().startswith("terminar "):
            parts      = incoming_message.strip().split()
            raw_number = parts[1] if len(parts) > 1 else ""
            if not raw_number.startswith("+"):
                raw_number = "+" + raw_number
            with _state_lock:
                in_session = raw_number in live_sessions
                if in_session:
                    live_sessions.pop(raw_number, None)
            if not in_session:
                return jsonify({"status": "ok"}), 200
            send_whatsapp(raw_number, "Listo, cualquier otra cosa me avisas. 👋")
            print(f"🟢 Manual live session ended for {raw_number}")
            send_whatsapp(owner_number, "✅ Sesión terminada. El bot retoma el control.")
            return jsonify({"status": "ok"}), 200

        # Normal approval handling
        def _on_cancel_reset(customer_number: str) -> None:
            """Reset customer state on owner cancelar — prevents stuck WAITING."""
            cancel_followup(customer_number)
            cancel_long_wait_alert(customer_number)
            with _state_lock:
                conv = conversations.get(customer_number)
                if conv:
                    conv["state"]                 = ConversationState.ACTIVE
                    conv["request_queue"]         = []
                    conv["confirming"]            = False
                    conv["asking_shared_vehicle"] = False
                    conv["asking_per_item"]       = False
                    conv["current_item_index"]    = 0
                    conv["dead_end_count"]        = 0
                    conv["same_field_count"]      = 0
                    conv["last_missing_field"]    = None
        result = handle_approval(
            incoming_message,
            pending_approvals,
            pending_selections,
            approval_message_map,
            replied_to_sid,
            on_cancel_reset=_on_cancel_reset
        )
        send_whatsapp(owner_number, result)
        return jsonify({"status": "ok"}), 200

    # 2. SUPPLIER → Supplier response flow
    registered_suppliers = get_registered_suppliers()
    supplier_numbers     = [s["number"] for s in registered_suppliers]

    if incoming_number in supplier_numbers:
        result = handle_supplier_response(
            incoming_number, incoming_message,
            replied_to_sid=replied_to_sid if replied_to_sid else None
        )
        if result:
            print(f"✅ Supplier response: {result['supplier_name']}")
            # Notify owner — integrate supplier result into flow
            parsed = result.get("parsed", {})
            part_str = f"{parsed.get('part', '?')} {parsed.get('make', '?')} " \
                       f"{parsed.get('model', '?')} {parsed.get('year', '?')}"
            price = result.get("price") or result.get("total_cost")
            lead = result.get("lead_time", "?")
            send_whatsapp(
                owner_number,
                f"📩 *Proveedor WhatsApp respondió*\n"
                f"{result['supplier_name']}: ${price}, {lead}\n"
                f"Pieza: {part_str}\n"
                f"Notas: {result.get('notes', '—')}"
            )
        return jsonify({"status": "ok"}), 200

    # 3. LOCAL STORE → forward message to owner, never treat as customer
    if incoming_number in get_store_numbers():
        handle_store_message(incoming_number, incoming_message)
        return jsonify({"status": "ok"}), 200

    # 3.5 BETA DISCOVERY MODE → handle before any regular customer/session routing
    print(f"🔍 Beta check: '{incoming_number}' | whitelist: {get_beta_whitelist()}")
    if is_beta_user(incoming_number):
        print(f"🧪 Beta route active for {incoming_number}")
        thread = threading.Thread(
            target=handle_beta_message,
            args=(incoming_number, incoming_message),
            daemon=True,
        )
        thread.start()
        return jsonify({"status": "ok"}), 200

    # 4. PENDING LIVE OFFER → customer responding to live session offer
    with _state_lock:
        has_pending_live_offer = incoming_number in pending_live_offers
        if has_pending_live_offer:
            pending_live_offers.pop(incoming_number, None)
    if has_pending_live_offer:
        affirmative = incoming_message.strip().lower() in [
            "sí", "si", "yes", "dale", "ok", "okey", "sip", "claro", "bueno", "va"
        ]
        if affirmative:
            with _state_lock:
                live_sessions[incoming_number] = True
            if owner_number:
                msg_sid = send_whatsapp(
                    owner_number,
                    f"🔴 *Sesión en vivo iniciada*\n"
                    f"Cliente: {incoming_number}\n\n"
                    f"_El cliente aceptó conectarse con el equipo. "
                    f"Escribe *fin* para terminar la sesión._"
                )
                if msg_sid:
                    with _state_lock:
                        escalation_message_map[msg_sid] = incoming_number
            send_whatsapp(incoming_number, "Perfecto, en un momento te contacta alguien del equipo. 👍")
        else:
            send_whatsapp(
                incoming_number,
                "Entendido, aquí estamos si necesitas algo. "
                "Para buscar un repuesto envíanos: Pieza + marca + modelo + año"
            )
        return jsonify({"status": "ok"}), 200

    # 5. LIVE SESSION → forward to owner, skip the bot
    with _state_lock:
        in_live_session = incoming_number in live_sessions
    if in_live_session:
        if owner_number:
            msg_sid = send_whatsapp(
                owner_number,
                f"💬 *{incoming_number}:*\n{incoming_message}"
            )
            if msg_sid:
                with _state_lock:
                    escalation_message_map[msg_sid] = incoming_number
                print(f"📨 Forwarded live message from {incoming_number} → owner")
        return jsonify({"status": "ok"}), 200

    # 6. CUSTOMER SELECTING AN OPTION
    if incoming_number in pending_selections:
        pending      = pending_selections.get(incoming_number)
        options      = pending["options"]
        final_prices = pending["final_prices"]
        parsed       = pending["parsed"]

        choice = interpret_option_choice(incoming_message, options, final_prices)

        if choice is not None:
            chosen = options[choice]
            price  = final_prices[choice]

            cancel_followup(incoming_number)
            send_whatsapp(
                incoming_number,
                f"✅ Confirmado. Tu {parsed.get('part')} para "
                f"{parsed.get('make')} {parsed.get('model')} {parsed.get('year')} "
                f"está apartado — *${price}*, entrega {chosen['lead_time']}. "
                f"Te contactamos para coordinar. 🙌"
            )

            send_whatsapp(
                owner_number,
                f"🎯 *Cliente confirmó opción {choice + 1}*\n"
                f"Pieza: {parsed.get('part')} "
                f"{parsed.get('make')} {parsed.get('model')} "
                f"{parsed.get('year')}\n"
                f"Precio: ${price}\n"
                f"Proveedor: {chosen['supplier_name']}\n"
                f"Entrega: {chosen['lead_time']}\n"
                f"Cliente: {incoming_number}"
            )

            log_request({
                "customer_number": incoming_number,
                "raw_message":     incoming_message,
                "parsed":          parsed,
                "options":         options,
                "final_prices":    final_prices,
                "chosen_option":   choice + 1,
                "status":          "confirmed",
            })

            del pending_selections[incoming_number]
            # Clear conversation — fresh start for their next request
            conversations.pop(incoming_number, None)
            cancel_long_wait_alert(incoming_number)
            monitor.increment_stat("orders_confirmed")

        else:
            if len(options) == 1:
                send_whatsapp(incoming_number, "¿Te sirve esta opción? Responde *sí* o *no*.")
            else:
                nums = " o ".join(str(i) for i in range(1, len(options) + 1))
                send_whatsapp(incoming_number, f"¿Cuál opción prefieres? Responde con el número ({nums}).")

        return jsonify({"status": "ok"}), 200

    # 6.5 CONFIRMING → customer confirming or correcting their queued request
    conv = conversations.get(incoming_number)
    if conv and conv.get("confirming"):
        affirmative = _is_affirmative(incoming_message)

        if affirmative:
            conv["confirming"] = False
            conv["state"]      = ConversationState.WAITING
            queue = list(conv["request_queue"])
            conv["request_queue"] = []

            threading.Thread(
                target=_run_multi_sourcing,
                args=(incoming_number, incoming_message, queue),
                daemon=True,
            ).start()
            return jsonify({"status": "ok"}), 200

        else:
            # If the message references 2+ distinct known models the customer
            # is likely mixing vehicles — escalate rather than guess a correction.
            _msg_lower = incoming_message.lower()
            _msg_words = set(_msg_lower.split())
            _matched_models = [
                key for key in MODEL_TO_MAKE
                if (key.lower() in _msg_lower if " " in key.lower()
                    else key.lower() in _msg_words)
            ]
            if len(_matched_models) >= 2:
                _handle_human_escalation(incoming_number, incoming_message)
                return jsonify({"status": "ok"}), 200

            representative = conv["request_queue"][0] if conv["request_queue"] else {}
            correction = parse_correction(incoming_message, representative)

            if correction:
                _apply_to_queue(conv["request_queue"], correction)
                send_whatsapp(
                    incoming_number,
                    generate_queue_confirmation(conv["request_queue"])
                )
            else:
                send_whatsapp(
                    incoming_number,
                    generate_response("correction_reminder", incoming_message, context={
                        "part":  representative.get("part"),
                        "make":  representative.get("make"),
                        "model": representative.get("model"),
                        "year":  representative.get("year"),
                    })
                )

        return jsonify({"status": "ok"}), 200

    # 6.6 SHARED VEHICLE QUESTION → yes = same vehicle, no = per-item
    conv = conversations.get(incoming_number)
    if conv and conv.get("asking_shared_vehicle"):
        if is_goodbye(incoming_message):
            _close_conversation(incoming_number, mid_flow=True)
            return jsonify({"status": "ok"}), 200
        conv["asking_shared_vehicle"] = False
        if _is_affirmative(incoming_message):
            # Same vehicle for all — proceed to normal single vehicle prompting
            _send_queue_missing_prompt(incoming_number, incoming_message, conv)
        else:
            # Different vehicles — collect one by one
            conv["asking_per_item"]    = True
            conv["current_item_index"] = 0
            queue = conv["request_queue"]
            if queue:
                part = queue[0].get("part") or "la primera pieza"
                send_whatsapp(
                    incoming_number,
                    f"Claro, vamos uno por uno. ¿Para qué vehículo es el *{part}*? "
                    f"Dime marca, modelo y año."
                )
        return jsonify({"status": "ok"}), 200

    # 6.7 PER-ITEM VEHICLE COLLECTION → one vehicle per part
    conv = conversations.get(incoming_number)
    if conv and conv.get("asking_per_item"):
        if is_goodbye(incoming_message):
            _close_conversation(incoming_number, mid_flow=True)
            return jsonify({"status": "ok"}), 200
        queue = conv["request_queue"]
        idx   = conv.get("current_item_index", 0)

        if idx < len(queue):
            current_item = queue[idx]
            part         = current_item.get("part") or "la pieza"
            partial      = extract_vehicle_for_part(incoming_message, part)

            if partial and any(partial.get(k) for k in ("make", "model", "year")):
                for k, v in partial.items():
                    if k in ("make", "model", "year") and v:
                        current_item[k] = str(v).strip()
                resolve_make_model(current_item, incoming_message)

                # Advance past any already-complete items
                next_idx = idx + 1
                while next_idx < len(queue) and _req_complete(queue[next_idx]):
                    next_idx += 1
                conv["current_item_index"] = next_idx

                if next_idx >= len(queue):
                    # All items visited — wrap up
                    conv["asking_per_item"]    = False
                    conv["current_item_index"] = 0
                    if _queue_all_complete(queue):
                        conv["confirming"] = True
                        send_whatsapp(incoming_number, generate_queue_confirmation(queue))
                    else:
                        _send_queue_missing_prompt(incoming_number, incoming_message, conv)
                else:
                    next_part = queue[next_idx].get("part") or "la siguiente pieza"
                    send_whatsapp(
                        incoming_number,
                        f"¿Y el *{next_part}*? ¿Para qué vehículo? Marca, modelo y año."
                    )
            else:
                # Nothing extracted — re-ask for same item
                send_whatsapp(
                    incoming_number,
                    f"No entendí el vehículo para el *{part}*. "
                    f"¿Me das marca, modelo y año? Por ejemplo: Toyota Hilux 2019."
                )

        return jsonify({"status": "ok"}), 200

    # 7. ALL OTHER MESSAGES → process in background
    def _run_and_untrack():
        process_customer_request(incoming_number, incoming_message)

    thread = threading.Thread(target=_run_and_untrack, daemon=True)
    thread.start()

    return jsonify({"status": "ok"}), 200


_ai_cache: dict = {"text": None, "generated_at": None}


@app.route("/dashboard", methods=["GET"])
def dashboard():
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if request.args.get("key") != password:
        return redirect("/?failed=1", 302)
    return make_response(render_dashboard(), 200)


@app.route("/dashboard/deliver", methods=["POST"])
def dashboard_deliver():
    from datetime import datetime as _dt
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if request.form.get("key") != password:
        return jsonify({"error": "unauthorized"}), 401
    row_ts   = request.form.get("row_ts", "")
    customer = request.form.get("customer", "")
    if not row_ts or not customer:
        return jsonify({"error": "missing params"}), 400
    try:
        sheet    = get_order_log()
        all_rows = sheet.get_all_values()
        for i, row in enumerate(all_rows, 1):
            if len(row) > 2 and row[0] == row_ts and row[2] == customer:
                ts_now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.update_cell(i, 20, ts_now)   # col 20 = index 19
                return jsonify({"ok": True, "ts": ts_now})
        return jsonify({"error": "row not found"}), 404
    except Exception as e:
        print(f"⚠️ deliver error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard/ai-insights", methods=["GET"])
def dashboard_ai_insights():
    from datetime import datetime as _dt, timedelta as _td
    import json as _json
    from collections import defaultdict as _dd
    from anthropic import Anthropic as _Anthropic

    password = os.getenv("DASHBOARD_PASSWORD", "")
    if request.args.get("key") != password:
        return jsonify({"error": "unauthorized"}), 401

    force = request.args.get("force", "0") == "1"
    now   = _dt.now()

    # Return cached if fresh (< 7 days) and not forced
    if _ai_cache["text"] and _ai_cache["generated_at"]:
        gen_dt = _dt.fromisoformat(_ai_cache["generated_at"])
        age_s  = (now - gen_dt).total_seconds()
        if not force and age_s < 7 * 86400:
            return jsonify(_ai_cache)
        if force and age_s < 3600:
            return jsonify({"error": "cooldown", "next_in": int(3600 - age_s)})

    # Build summary from last 7 days
    try:
        sheet    = get_order_log()
        all_rows = sheet.get_all_values()
    except Exception as e:
        return jsonify({"error": f"sheet error: {e}"}), 500

    week_ago = now - _td(days=7)
    def _parse(ts):
        try:
            return _dt.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    data = [r for r in all_rows
            if r and r[0].lower() not in ("timestamp", "fecha", "date")
            and _parse(r[0] if r else "") and _parse(r[0]) >= week_ago]

    def _g(row, i): return row[i] if len(row) > i else ""

    statuses   = _dd(int)
    top_parts  = _dd(int)
    top_makes  = _dd(int)
    no_result  = _dd(int)
    for r in data:
        statuses[_g(r, 18)] += 1
        if _g(r, 3): top_parts[_g(r, 3).strip().lower()] += 1
        if _g(r, 4): top_makes[_g(r, 4).strip()] += 1
        if _g(r, 18) == "not_found" and _g(r, 3):
            no_result[_g(r, 3).strip().lower()] += 1

    summary = _json.dumps({
        "periodo": "últimos 7 días",
        "total_transacciones": len(data),
        "estados": dict(statuses),
        "piezas_mas_solicitadas": dict(
            sorted(top_parts.items(), key=lambda x: -x[1])[:10]),
        "marcas_mas_solicitadas": dict(
            sorted(top_makes.items(), key=lambda x: -x[1])[:5]),
        "piezas_sin_resultado": dict(
            sorted(no_result.items(), key=lambda x: -x[1])[:10]),
    }, ensure_ascii=False, indent=2)

    prompt = (
        "Eres un analista de operaciones para Zeli, un servicio de repuestos "
        "automotrices en Santiago, Panamá. Analiza estos datos de la última semana y entrega:\n"
        "1. Top 3 hallazgos más importantes\n"
        "2. Gaps de sourcing críticos (piezas sin cobertura)\n"
        "3. Problemas en el flujo de conversación si los hay\n"
        "4. 3 recomendaciones concretas y accionables\n"
        "5. Una métrica positiva para celebrar\n\n"
        "Sé directo y específico. Sin preamble. Máximo 200 palabras.\n"
        f"Datos:\n{summary}"
    )

    try:
        client   = _Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        _ai_cache["text"]         = text
        _ai_cache["generated_at"] = now.isoformat()
        return jsonify(_ai_cache)
    except Exception as e:
        print(f"⚠️ AI insights error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    failed = request.args.get("failed")
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoParts Dashboard</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f0f2f5; display: flex; align-items: center;
            justify-content: center; height: 100vh;
        }}
        .card {{
            background: white; border-radius: 14px;
            box-shadow: 0 4px 20px rgba(0,0,0,.1);
            padding: 40px; width: 100%; max-width: 360px; text-align: center;
        }}
        h1 {{ font-size: 1.3rem; color: #1a1a2e; margin-bottom: 6px; }}
        p {{ color: #888; font-size: 0.85rem; margin-bottom: 24px; }}
        input {{
            width: 100%; padding: 12px 14px; border: 1px solid #ddd;
            border-radius: 8px; font-size: 0.95rem; outline: none;
            margin-bottom: 12px; transition: border .2s;
        }}
        input:focus {{ border-color: #1a1a2e; }}
        button {{
            width: 100%; padding: 12px; background: #1a1a2e; color: white;
            border: none; border-radius: 8px; font-size: 0.95rem;
            font-weight: 600; cursor: pointer; transition: opacity .2s;
        }}
        button:hover {{ opacity: 0.85; }}
        .error {{ color: #c62828; font-size: 0.82rem; margin-bottom: 12px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>📊 AutoParts Dashboard</h1>
        <p>Ingresa tu contraseña para continuar</p>
        {'<div class="error">Contraseña incorrecta. Intenta de nuevo.</div>' if failed else ''}
        <form action="/dashboard" method="get">
            <input type="password" name="key" placeholder="Contraseña" autofocus required>
            <button type="submit">Entrar</button>
        </form>
    </div>
</body>
</html>"""
    return make_response(html, 200)


@app.route("/health", methods=["GET"])
def health():
    stats = monitor.get_stats()
    return {
        "status":               "running",
        "service":              "AutoParts Trading Co.",
        "active_conversations": len(conversations),
        "pending_approvals":    len(pending_approvals),
        "uptime_since":         STARTUP_TIME,
        "today": {
            "conversations":    stats.get("conversations", 0),
            "quotes_sent":      stats.get("quotes_sent", 0),
            "orders_confirmed": stats.get("orders_confirmed", 0),
            "parts_not_found":  stats.get("parts_not_found", 0),
            "errors":           stats.get("errors", 0),
        },
    }, 200


# ── Startup notification + daily summary daemon ────────────────────────────────

threading.Thread(target=_send_startup_notification_once, daemon=True).start()
threading.Thread(target=monitor._daily_summary_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, port=port)
