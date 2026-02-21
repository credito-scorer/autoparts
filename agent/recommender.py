import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

DEFAULT_MARKUP = 0.35

def calculate_price(cost: float, markup: float = DEFAULT_MARKUP) -> float:
    return round(cost * (1 + markup), 2)

def calculate_margin(cost: float, price: float) -> float:
    return round(price - cost, 2)

def build_options(results: list, parsed: dict) -> list:
    if not results:
        return []
    
    seen = set()
    unique_results = []
    for r in results:
        key = r["supplier_name"]
        if key not in seen:
            seen.add(key)
            unique_results.append(r)
    
    by_cost = sorted(
        unique_results,
        key=lambda x: x.get("total_cost") or x.get("price") or 999
    )
    
    def parse_lead_days(lead_time: str) -> int:
        import re
        numbers = re.findall(r'\d+', lead_time)
        return int(numbers[0]) if numbers else 99
    
    by_speed = sorted(
        unique_results,
        key=lambda x: parse_lead_days(x.get("lead_time", "99"))
    )
    
    candidates = []
    seen_suppliers = set()
    
    if by_cost:
        cheapest = by_cost[0]
        cost = cheapest.get("total_cost") or cheapest.get("price") or 0
        suggested_price = calculate_price(cost)
        candidates.append({
            "label": "💰 Más económica",
            "supplier_name": cheapest["supplier_name"],
            "cost": cost,
            "suggested_price": suggested_price,
            "margin": calculate_margin(cost, suggested_price),
            "lead_time": cheapest["lead_time"],
            "source": cheapest["source"],
            "notes": cheapest.get("notes", "")
        })
        seen_suppliers.add(cheapest["supplier_name"])
    
    if by_speed and by_speed[0]["supplier_name"] not in seen_suppliers:
        fastest = by_speed[0]
        cost = fastest.get("total_cost") or fastest.get("price") or 0
        suggested_price = calculate_price(cost)
        candidates.append({
            "label": "⚡ Más rápida",
            "supplier_name": fastest["supplier_name"],
            "cost": cost,
            "suggested_price": suggested_price,
            "margin": calculate_margin(cost, suggested_price),
            "lead_time": fastest["lead_time"],
            "source": fastest["source"],
            "notes": fastest.get("notes", "")
        })
        seen_suppliers.add(fastest["supplier_name"])
    
    for r in by_cost:
        if r["supplier_name"] not in seen_suppliers:
            cost = r.get("total_cost") or r.get("price") or 0
            suggested_price = calculate_price(cost)
            candidates.append({
                "label": "🔄 Alternativa",
                "supplier_name": r["supplier_name"],
                "cost": cost,
                "suggested_price": suggested_price,
                "margin": calculate_margin(cost, suggested_price),
                "lead_time": r["lead_time"],
                "source": r["source"],
                "notes": r.get("notes", "")
            })
            seen_suppliers.add(r["supplier_name"])
            break
    
    return candidates


def format_approval_message(options: list, parsed: dict, customer_number: str) -> str:
    part = parsed.get("part", "")
    make = parsed.get("make", "")
    model = parsed.get("model", "")
    year = parsed.get("year", "")
    display_number = customer_number.replace("whatsapp:", "")
    
    msg = f"🔩 *Nueva solicitud*\n"
    msg += f"Cliente: {display_number}\n"
    msg += f"Pieza: {part} — {make} {model} {year}\n\n"
    
    for i, opt in enumerate(options, 1):
        msg += f"*Opción {i}* {opt['label']}\n"
        msg += f"Proveedor: {opt['supplier_name']}\n"
        msg += f"Costo: ${opt['cost']}\n"
        msg += f"Precio sugerido: ${opt['suggested_price']}\n"
        msg += f"Margen: ${opt['margin']}\n"
        msg += f"Entrega: {opt['lead_time']}\n\n"
    
    msg += "━━━━━━━━━━━━━━━━\n"
    msg += "Responde con precios finales separados por coma:\n"
    example_prices = ",".join([str(opt["suggested_price"]) for opt in options])
    msg += f"Ejemplo: *{example_prices}*\n"
    msg += "\nO escribe *cancelar* para no cotizar."
    
    return msg


def format_customer_quote(options: list, parsed: dict, final_prices: list) -> str:
    part = parsed.get("part", "")
    make = parsed.get("make", "")
    model = parsed.get("model", "")
    year = parsed.get("year", "")
    
    msg = f"🔩 *{part} — {make} {model} {year}*\n\n"
    msg += "Encontramos estas opciones:\n\n"
    
    for i, (opt, price) in enumerate(zip(options, final_prices), 1):
        msg += f"*{i}️⃣ {opt['label']}*\n"
        msg += f"💵 Precio: *${price}*\n"
        msg += f"🚚 Entrega: {opt['lead_time']}\n\n"
    
    if len(options) == 1:
        msg += "Responde con *1* para confirmar."
    else:
        options_str = " o ".join([str(i) for i in range(1, len(options) + 1)])
        msg += f"¿Cuál prefieres? Responde con el número de opción ({options_str})."
    
    return msg
