import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

def parse_request(message: str) -> dict:
    """
    Takes a raw Spanish WhatsApp message from a mechanic
    and extracts structured part request data.
    """
    
    prompt = f"""Eres un asistente especializado en repuestos de autos en Panamá.
    
Un mecánico te envió este mensaje por WhatsApp:
"{message}"

Extrae la información del repuesto solicitado y responde ÚNICAMENTE con un JSON válido con esta estructura:
{{
    "part": "nombre del repuesto en español",
    "make": "marca del vehículo",
    "model": "modelo del vehículo", 
    "year": "año del vehículo",
    "part_number": "número de parte si fue mencionado, sino null",
    "additional_specs": "especificaciones adicionales si fueron mencionadas, sino null",
    "confidence": "high/medium/low según qué tan clara fue la solicitud"
}}

Si no puedes extraer la información mínima (repuesto + vehículo), responde con null.
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
