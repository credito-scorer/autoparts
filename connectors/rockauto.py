import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

def search_rockauto(parsed: dict) -> dict | None:
    """
    Uses Claude to estimate US market pricing for a part.
    In production this would connect to a real parts API.
    """
    
    part = parsed.get("part", "")
    make = parsed.get("make", "")
    model = parsed.get("model", "")
    year = parsed.get("year", "")
    
    prompt = f"""Eres un experto en precios de repuestos de autos en el mercado estadounidense.

Necesito el precio estimado en USD para:
Pieza: {part}
Marca: {make}
Modelo: {model}
Año: {year}

Basado en precios típicos de RockAuto, Amazon, y distribuidores americanos, responde ÚNICAMENTE con JSON:
{{
    "found": true,
    "part_name": "nombre exacto de la pieza en inglés",
    "brand": "marca recomendada (calidad media-alta)",
    "price_usd": precio numérico sin simbolos,
    "part_number": "número de parte típico si lo conoces",
    "notes": "notas relevantes sobre la pieza"
}}

Si genuinamente no puedes estimar el precio, responde con {{"found": false}}.
Solo responde con el JSON, sin explicaciones."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw = response.content[0].text.strip()
        
        # Clean markdown if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        
        result = json.loads(raw.strip())
        
        if not result.get("found"):
            return None
        
        # Standard shipping estimate to Panama via forwarder
        shipping_estimate = 25.0
        
        return {
            "supplier_name": "USA (via Miami forwarder)",
            "part_name": result.get("part_name"),
            "brand": result.get("brand"),
            "price": result.get("price_usd", 0),
            "shipping": shipping_estimate,
            "total_cost": result.get("price_usd", 0) + shipping_estimate,
            "lead_time": "5-7 días",
            "part_number": result.get("part_number"),
            "notes": result.get("notes", ""),
            "source": "usa_supplier"
        }
        
    except Exception as e:
        print(f"USA supplier search error: {e}")
        return None


if __name__ == "__main__":
    test_parsed = {
        "part": "Alternador",
        "make": "Toyota",
        "model": "Hilux",
        "year": "2008"
    }
    
    print("Searching USA suppliers...")
    result = search_rockauto(test_parsed)
    
    if result:
        print(f"✅ Found: {result['part_name']}")
        print(f"   Brand: {result['brand']}")
        print(f"   Price: ${result['price']} + ${result['shipping']} shipping")
        print(f"   Total cost to us: ${result['total_cost']}")
        print(f"   Lead time: {result['lead_time']}")
    else:
        print("❌ Not found")
