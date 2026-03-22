from datetime import datetime, timedelta, UTC
import json
import os
import threading

_lock = threading.Lock()
_conversations = {}
_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "conversation_store.json")


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ensure_parent_dir() -> None:
    parent = os.path.dirname(_STORE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _save_to_disk() -> None:
    _ensure_parent_dir()
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_conversations, f, ensure_ascii=False, indent=2)


def _load_from_disk() -> None:
    global _conversations
    try:
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _conversations = data
    except Exception as e:
        print(f"⚠️ conversation_store load failed: {e}")


def _ensure_conversation(number: str) -> None:
    if number not in _conversations:
        _conversations[number] = {
            "messages": [],
            "vertical": "unknown",
            "first_message_at": _utc_iso(),
            "last_message_at": None,
            "intent_score": None,
            "customer_name": None,
            "re_profile": {},
        }


def log_message(number, direction, body, message_id=None, metadata=None):
    """Log a single message (inbound or outbound)."""
    with _lock:
        number = _normalize_number(number)
        _ensure_conversation(number)

        _conversations[number]["messages"].append({
            "direction": direction,
            "body": body,
            "timestamp": _utc_iso(),
            "message_id": message_id,
        })
        _conversations[number]["last_message_at"] = _utc_iso()

        if metadata:
            _conversations[number].update(metadata)
        _save_to_disk()


def update_metadata(number, **kwargs):
    """Update conversation metadata (vertical, intent_score, customer_name)."""
    with _lock:
        number = _normalize_number(number)
        _ensure_conversation(number)
        for key, value in kwargs.items():
            _conversations[number][key] = value
        _conversations[number]["last_message_at"] = _utc_iso()
        _save_to_disk()


def get_all_conversations(max_age_hours=24):
    """Return all conversations from the last N hours, sorted by last_message_at desc."""
    with _lock:
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat().replace("+00:00", "Z")
        result = {}
        for number, convo in _conversations.items():
            if convo["last_message_at"] and convo["last_message_at"] > cutoff:
                result[number] = convo
        return dict(
            sorted(result.items(), key=lambda x: x[1]["last_message_at"] or "", reverse=True)
        )


def get_conversation(number):
    """Return a single conversation by number."""
    with _lock:
        number = _normalize_number(number)
        return _conversations.get(number)


def delete_conversation(number):
    """Delete a conversation and persist the updated store."""
    with _lock:
        number = _normalize_number(number)
        if number in _conversations:
            _conversations.pop(number, None)
            _save_to_disk()


def clear_all():
    """Clear all conversations (intended for test setup)."""
    with _lock:
        _conversations.clear()
        _save_to_disk()


def _normalize_number(number):
    """Strip 'whatsapp:' prefix if present."""
    return number.replace("whatsapp:", "").strip()


_load_from_disk()
