from datetime import datetime, timedelta
import threading

_lock = threading.Lock()
_conversations = {}


def log_message(number, direction, body, message_id=None, metadata=None):
    """Log a single message (inbound or outbound)."""
    with _lock:
        number = _normalize_number(number)
        if number not in _conversations:
            _conversations[number] = {
                "messages": [],
                "vertical": "unknown",
                "first_message_at": datetime.utcnow().isoformat() + "Z",
                "last_message_at": None,
                "intent_score": None,
                "customer_name": None,
            }

        _conversations[number]["messages"].append({
            "direction": direction,
            "body": body,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "message_id": message_id,
        })
        _conversations[number]["last_message_at"] = datetime.utcnow().isoformat() + "Z"

        if metadata:
            if "vertical" in metadata:
                _conversations[number]["vertical"] = metadata["vertical"]
            if "intent_score" in metadata:
                _conversations[number]["intent_score"] = metadata["intent_score"]
            if "customer_name" in metadata:
                _conversations[number]["customer_name"] = metadata["customer_name"]


def update_metadata(number, **kwargs):
    """Update conversation metadata (vertical, intent_score, customer_name)."""
    with _lock:
        number = _normalize_number(number)
        if number in _conversations:
            for key, value in kwargs.items():
                if key in _conversations[number]:
                    _conversations[number][key] = value


def get_all_conversations(max_age_hours=24):
    """Return all conversations from the last N hours, sorted by last_message_at desc."""
    with _lock:
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat() + "Z"
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


def _normalize_number(number):
    """Strip 'whatsapp:' prefix if present."""
    return number.replace("whatsapp:", "").strip()
