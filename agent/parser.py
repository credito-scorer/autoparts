import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

def parse_request(message: str) -> dict | None:
    """
    Takes a raw Spanish WhatsApp message from a mechanic
    and extracts structured part request data.
    Returns a partial dict (some fields may be null) if any part/vehicle info is found.
    Returns None only for purely conversational messages with no part/vehicle content.
    """

    prompt = f"""Eres un asistente especializado en repuestos de autos en Panamá.

Un mecánico te envió este mensaje por WhatsApp:
"{message}"

Extrae TODA la información de repuesto o vehículo que encuentres y responde ÚNICAMENTE con un JSON válido:
{{
    "part": "nombre del repuesto en español, o null si no se menciona",
    "make": "marca del vehículo, o null si no se menciona",
    "model": "modelo del vehículo, o null si no se menciona",
    "year": "año del vehículo, o null si no se menciona",
    "part_number": "número de parte si fue mencionado, sino null",
    "additional_specs": "especificaciones adicionales si fueron mencionadas, sino null",
    "confidence": "high/medium/low según qué tan clara fue la solicitud"
}}

Regla crítica: Si un campo no está claramente indicado en el mensaje, devuelve null. Nunca inferas ni adivines. Solo extrae lo que el cliente dijo explícitamente.
Devuelve el JSON con los campos que puedas extraer (los demás en null).
Responde con null SOLO si el mensaje es puramente conversacional (saludo, ok, gracias, etc.) sin ninguna mención de piezas o vehículos.
No incluyas explicaciones, solo el JSON."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    raw = response.content[0].text.strip()
    
    # Clean markdown if Claude wrapped it in ```json
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    raw = raw.strip()

    if not raw or raw == "null":
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

def parse_request_multi(message: str) -> list[dict]:
    """
    Detect all part requests in a single message.
    Returns a list of partial request dicts (each may have null fields).
    Returns [] for purely conversational messages.
    """
    prompt = f"""Eres un asistente de repuestos de autos en Panamá.

Un mecánico envió este mensaje:
"{message}"

Detecta TODAS las piezas solicitadas. Para cada una extrae:
- part: nombre del repuesto en español
- make: marca del vehículo (o null)
- model: modelo del vehículo (o null)
- year: año del vehículo (o null)

Regla crítica: Si un campo no está claramente indicado, devuelve null. Nunca inferas ni adivines.
Si el vehículo es compartido entre piezas, repite los campos de vehículo en cada objeto.

Responde ÚNICAMENTE con un array JSON:
[{{"part": "...", "make": "...", "model": "...", "year": "..."}}, ...]

Si hay un solo repuesto, devuelve un array de un elemento.
Responde con null si el mensaje es puramente conversacional sin mención de piezas o vehículos.
No incluyas explicaciones."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        if not raw or raw == "null":
            return []
        result = json.loads(raw)
        if isinstance(result, list):
            return [r for r in result if r.get("part")]
        if isinstance(result, dict) and result.get("part"):
            return [result]
        return []
    except Exception:
        return []


def extract_partial(message: str, known: dict) -> dict | None:
    """
    Given a follow-up message and what we already know, extract any new
    part/vehicle fields the customer is providing. Uses Haiku for speed.
    Returns a dict with newly found fields, or None if nothing new found.
    """
    missing = [k for k in ("part", "make", "model", "year") if not known.get(k)]
    if not missing:
        return None

    known_str = ", ".join(f"{k}: {v}" for k, v in known.items() if v and k in ("part", "make", "model", "year"))
    missing_str = ", ".join(missing)

    prompt = (
        f"Un cliente está pidiendo un repuesto. Ya sabemos: {known_str}. "
        f"Aún nos falta: {missing_str}.\n\n"
        f"El cliente envió este nuevo mensaje: \"{message}\"\n\n"
        f"Extrae SOLO los campos faltantes que el cliente mencione explícitamente. "
        f"Regla crítica: Si un campo no está claramente indicado, no lo incluyas. Solo extrae lo que el cliente dijo. "
        f"Responde con JSON con solo esos campos (ej: {{\"model\": \"Hilux\", \"year\": \"2008\"}}). "
        f"Si el mensaje no aporta ningún campo nuevo, responde con null. "
        f"No incluyas campos ya conocidos. Solo el JSON."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except Exception:
        return None


_AFFIRMATIONS = {
    "sí", "si", "dale", "ok", "okey", "ese", "ese mismo", "ese está bien",
    "ese me sirve", "ese jale", "ese ta bien", "ese tá bien", "ese pues",
    "bueno", "perfecto", "listo", "claro", "va", "yes", "sip", "eso",
    "correcto", "excelente", "genial",
}


def interpret_option_choice(message: str, options: list, final_prices: list) -> int | None:
    """
    Interprets a customer's natural language option selection.
    Returns a 0-indexed option number, or None if ambiguous.
    """
    msg = message.strip()
    msg_lower = msg.lower()

    # Fast path: numeric
    if msg in ["1", "2", "3"]:
        idx = int(msg) - 1
        return idx if idx < len(options) else None

    # Single option + any affirmation → auto-select
    if len(options) == 1:
        if msg_lower in _AFFIRMATIONS or any(msg_lower.startswith(a + " ") for a in _AFFIRMATIONS):
            return 0

    # Use Haiku to interpret natural language
    options_text = "\n".join(
        f"Opción {i}: {opt['label']} — ${price}, entrega {opt['lead_time']}"
        for i, (opt, price) in enumerate(zip(options, final_prices), 1)
    )
    prompt = (
        f"El cliente está eligiendo entre estas opciones de repuesto:\n{options_text}\n\n"
        f"El cliente respondió: \"{message}\"\n\n"
        f"¿A qué número de opción se refiere? "
        f"Responde SOLO con el número (1, 2, o 3). "
        f"Si no está claro, responde null."
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if not raw.isdigit():
            return None
        idx = int(raw) - 1
        return idx if 0 <= idx < len(options) else None
    except Exception:
        return None


def parse_correction(message: str, current: dict) -> dict | None:
    """
    Given a correction message and the current complete request,
    return which field(s) changed and their new values.
    """
    prompt = (
        f"Tenemos este pedido de repuesto:\n"
        f"Pieza: {current.get('part')}\n"
        f"Marca: {current.get('make')}\n"
        f"Modelo: {current.get('model')}\n"
        f"Año: {current.get('year')}\n\n"
        f"El cliente está corrigiendo algo: \"{message}\"\n\n"
        f"Identifica qué campo(s) cambia y el nuevo valor. "
        f"Responde SOLO con JSON de los campos corregidos. "
        f"Ejemplo: {{\"year\": \"2000\"}} o {{\"part\": \"pastillas de freno\"}}. "
        f"Si no es una corrección clara, responde con null."
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except Exception:
        return None


def detect_needs_human(message: str) -> bool:
    """
    Uses Claude to detect if a customer is frustrated, confused,
    lost, or would benefit from talking to a real person.
    """
    prompt = f"""Un cliente envió este mensaje a un bot de repuestos de autos:
"{message}"

¿El mensaje indica que el cliente está frustrado, molesto, confundido,
perdido, no está recibiendo ayuda adecuada, o necesita hablar con una persona?

Responde ÚNICAMENTE con true o false."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip().lower() == "true"
    except Exception:
        return False


if __name__ == "__main__":
    # Quick test
    test_messages = [
        "necesito el alternador del hilux 08",
        "tienes filtro de aceite para corolla 2015",
        "busco pastillas de freno toyota land cruiser 2010 delanteras",
        "hola buenas"
    ]
    
    for msg in test_messages:
        print(f"\nInput: {msg}")
        result = parse_request(msg)
        print(f"Output: {json.dumps(result, ensure_ascii=False, indent=2)}")
