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
        f"Extrae SOLO los campos faltantes que el cliente mencione. "
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
