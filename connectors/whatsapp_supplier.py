import os
import json
import time
import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1016895944841092")
API_URL = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"


def _send_whatsapp(to: str, message: str) -> None:
    number = to.replace("whatsapp:", "").replace("+", "")
    headers = {
        "Authorization": f"Bearer {os.getenv('META_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "text",
        "text": {"body": message}
    }
    try:
        resp = requests.post(API_URL, json=payload, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Failed to send WhatsApp to {to}: {e}")

# In-memory store for pending supplier responses
# Format: {supplier_number: {"parsed": parsed, "timestamp": time, "response": None}}
pending_supplier_queries = {}

def query_whatsapp_supplier(supplier: dict, parsed: dict) -> dict | None:
    """
    Send a structured WhatsApp message to a supplier
    and register the query as pending.
    
    supplier format:
    {
        "name": "Distribuidora Panama",
        "number": "whatsapp:+507XXXXXXXX",
        "lead_time": "1-2 días"
    }
    """
    
    part = parsed.get("part", "")
    make = parsed.get("make", "")
    model = parsed.get("model", "")
    year = parsed.get("year", "")
    part_number = parsed.get("part_number", "")
    
    # Build a clean structured query in Spanish
    message = f"🔍 Consulta de disponibilidad:\n"
    message += f"Pieza: {part}\n"
    message += f"Vehículo: {make} {model} {year}\n"
    if part_number:
        message += f"N° de parte: {part_number}\n"
    message += f"\n¿Tienen disponible? ¿Precio y tiempo de entrega a Santiago?"
    
    try:
        _send_whatsapp(supplier["number"], message)
        
        # Register as pending
        pending_supplier_queries[supplier["number"]] = {
            "supplier_name": supplier["name"],
            "parsed": parsed,
            "timestamp": time.time(),
            "response": None,
            "lead_time_default": supplier.get("lead_time", "1-2 días")
        }
        
        print(f"✅ Query sent to {supplier['name']}")
        return {"status": "pending", "supplier": supplier["name"]}
        
    except Exception as e:
        print(f"Error querying {supplier['name']}: {e}")
        return None

def handle_supplier_response(supplier_number: str, response_text: str) -> dict | None:
    """
    Called when a supplier replies to our WhatsApp query.
    Uses Claude to parse their Spanish response.
    """
    
    if supplier_number not in pending_supplier_queries:
        return None
    
    query = pending_supplier_queries[supplier_number]
    parsed = query["parsed"]
    
    prompt = f"""Un proveedor de repuestos en Panamá respondió a nuestra consulta.

Consultamos por: {parsed.get('part')} para {parsed.get('make')} {parsed.get('model')} {parsed.get('year')}

Su respuesta fue:
"{response_text}"

Extrae la información y responde ÚNICAMENTE con JSON:
{{
    "available": true/false,
    "price": precio numérico en USD o null,
    "lead_time": "tiempo de entrega como string",
    "notes": "notas adicionales relevantes"
}}

Si no hay precio claro, estima basado en el contexto o pon null.
Solo responde con el JSON."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw = response.content[0].text.strip()
        
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        
        result = json.loads(raw.strip())
        
        if not result.get("available"):
            del pending_supplier_queries[supplier_number]
            return None
        
        supplier_result = {
            "supplier_name": query["supplier_name"],
            "price": result.get("price"),
            "total_cost": result.get("price"),
            "lead_time": result.get("lead_time", query["lead_time_default"]),
            "notes": result.get("notes", ""),
            "source": "whatsapp_supplier"
        }
        
        # Clear from pending
        del pending_supplier_queries[supplier_number]
        
        return supplier_result
        
    except Exception as e:
        print(f"Error parsing supplier response: {e}")
        return None

def get_registered_suppliers() -> list:
    """
    Returns the list of active WhatsApp suppliers.
    In production this would come from a config file or database.
    Add your Panama City distributors here as you onboard them.
    """
    suppliers_raw = os.getenv("WHATSAPP_SUPPLIERS", "")
    
    if not suppliers_raw:
        return []
    
    # Format in .env:
    # WHATSAPP_SUPPLIERS=Name1|number1|leadtime1,Name2|number2|leadtime2
    suppliers = []
    for entry in suppliers_raw.split(","):
        parts = entry.strip().split("|")
        if len(parts) == 3:
            suppliers.append({
                "name": parts[0],
                "number": parts[1],
                "lead_time": parts[2]
            })
    
    return suppliers


if __name__ == "__main__":
    # Test parsing a typical supplier response
    test_response = "Sí tenemos el alternador para Hilux 2008, precio $95, " \
                   "lo tenemos en stock, entrega a Santiago mañana."
    
    # Simulate a pending query
    pending_supplier_queries["+50712345678"] = {
        "supplier_name": "Distribuidora Test PTY",
        "parsed": {
            "part": "Alternador",
            "make": "Toyota", 
            "model": "Hilux",
            "year": "2008"
        },
        "timestamp": time.time(),
        "response": None,
        "lead_time_default": "1-2 días"
    }
    
    print("Testing supplier response parser...")
    result = handle_supplier_response("+50712345678", test_response)
    
    if result:
        print(f"✅ Parsed response:")
        print(f"   Supplier: {result['supplier_name']}")
        print(f"   Price: ${result['price']}")
        print(f"   Lead time: {result['lead_time']}")
        print(f"   Notes: {result['notes']}")
    else:
        print("❌ Could not parse response")
