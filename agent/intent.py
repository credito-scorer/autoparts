import os
from anthropic import Anthropic

_client = Anthropic()

_SYSTEM = """\
Classify the incoming WhatsApp message into exactly one of these categories:

autoparts   — mentions car parts, "pieza", "repuesto", "alternador", "filtro",
              "bujía", "frenos", "aceite", vehicle makes/models/years, or any
              auto-repair or vehicle-part topic.

realestate  — mentions "terreno", "lote", "lotificación", "propiedad",
              "metro cuadrado", "m2", "construir", "título de propiedad",
              "Encuentra24", property prices, or interest in buying land/lots.

social      — pure greetings ("hola", "buenos días", "buenas"), thanks,
              acknowledgments, or one-word affirmations with no other content.

exploratory — anything else: ambiguous messages, asking what Zeli does,
              unrelated services (plumbing, electricity, food, etc.), or
              anything that doesn't clearly fit the above.

Reply with exactly one word — no punctuation, no explanation.\
"""


def classify_intent(message: str) -> str:
    """Return 'autoparts', 'realestate', 'social', or 'exploratory'."""
    try:
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=_SYSTEM,
            messages=[{"role": "user", "content": message}],
        )
        result = resp.content[0].text.strip().lower()
        if result in ("autoparts", "realestate", "social", "exploratory"):
            return result
        # Partial match fallback
        for label in ("autoparts", "realestate", "social", "exploratory"):
            if label in result:
                return label
        return "exploratory"
    except Exception as e:
        print(f"⚠️ classify_intent error: {e}")
        return "autoparts"  # safe default — existing flow handles it
