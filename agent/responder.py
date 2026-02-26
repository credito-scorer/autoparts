import os
from anthropic import Anthropic

client = Anthropic()

SYSTEM_PROMPT = """Eres el agente de atención al cliente de Zeli, una empresa de repuestos automotrices en Santiago, Veraguas, Panamá.

Tu tono es profesional, cálido y eficiente. Hablas como una persona real — no como un bot. Usas español natural de Panamá. Eres conciso: no más de 3-4 oraciones por respuesta.

Reglas:
- Nunca digas "claro que sí", "por supuesto", ni frases robóticas
- Nunca uses asteriscos para énfasis excesivo
- No repitas lo que el cliente dijo
- No expliques lo que vas a hacer, simplemente hazlo
- Si pides información, pregunta una sola cosa a la vez
- Usa emojis con moderación (1-2 máximo si aplica)
- Después del primer mensaje, nunca uses frases de apertura como "¡Hola!", "Bienvenido" o "Gracias por escribir"
- Las respuestas se vuelven más cortas y directas a medida que avanza la conversación"""

SITUATION_PROMPTS = {
    "greeting": (
        "El cliente acaba de saludar. Responde brevemente con un saludo cálido, "
        "preséntate como Zeli y pide que te digan qué necesitan. "
        "Menciona el formato: pieza + marca + modelo + año."
    ),
    "secondary_greeting": (
        "El cliente preguntó cómo estás o algo similar. "
        "Responde de forma natural y pregunta en qué puedes ayudarle."
    ),
    "vague_intent": (
        "El cliente insinuó que necesita algo pero no fue específico. "
        "Pídele que te diga la pieza, marca, modelo y año del vehículo."
    ),
    "part_not_found": (
        "No encontramos la pieza solicitada. "
        "Informa al cliente con empatía y SIEMPRE termina con un próximo paso concreto. "
        "Prioridad: (a) ofrecerle avisarle en cuanto la consigamos, "
        "(b) sugerir una alternativa compatible si existe, "
        "(c) ofrecerle conectarlo con alguien del equipo. "
        "Nunca termines solo con 'no la encontramos' — siempre hay un siguiente paso."
    ),
    "human_request": (
        "El cliente quiere hablar con una persona. "
        "Confírmale que alguien del equipo le va a contactar pronto. "
        "Sé breve y tranquilizador."
    ),
    "thanks": (
        "El cliente dio las gracias. "
        "Responde de forma natural y ofrécete por si necesita algo más."
    ),
    "ack": (
        "El cliente respondió con un simple 'ok', 'entendido' o similar. "
        "Responde brevemente y pregunta si necesita algo más."
    ),
    "unknown": (
        "El cliente envió un mensaje que no entendemos bien. "
        "Pídele que nos diga qué pieza necesita con el formato: "
        "pieza + marca + modelo + año. Sé amable, no condescendiente."
    ),
}

FIELD_LABELS = {
    "part": "la pieza",
    "make": "la marca del vehículo",
    "model": "el modelo",
    "year": "el año",
}

WAIT_ACKNOWLEDGMENT = "Claro, tómate tu tiempo. Aquí estamos cuando estés listo. 👍"


def _build_confirmation_instruction(context: dict) -> str:
    part  = context.get("part",  "?")
    make  = context.get("make",  "?")
    model = context.get("model", "?")
    year  = context.get("year",  "?")
    return (
        f"Genera un resumen de confirmación del pedido para el cliente. "
        f"Pieza: {part}. Vehículo: {make} {model} {year}. "
        f"Usa 🔩 para la pieza y 🚗 para el vehículo. "
        f"Pide que confirmen con 'sí' o que corrijan lo que esté mal. "
        f"Sé claro y conciso. No uses frases largas."
    )


def _build_correction_reminder_instruction(context: dict) -> str:
    part  = context.get("part",  "?")
    make  = context.get("make",  "?")
    model = context.get("model", "?")
    year  = context.get("year",  "?")
    return (
        f"El cliente tiene este pedido esperando confirmación: "
        f"{part} para {make} {model} {year}. "
        f"Respondió algo que no entendemos. "
        f"Recuérdale en una frase que confirme con 'sí' o corrija lo que esté mal."
    )


def _build_missing_fields_instruction(context: dict) -> str:
    known: dict = context.get("known", {})
    missing: list = context.get("missing", [])
    is_first = context.get("is_first_message", False)

    # Ask for exactly ONE field — the first missing in priority order (part > make > model > year)
    next_field = missing[0] if missing else "part"
    known_parts = [f"{k} = {v}" for k, v in known.items() if v]
    known_str = ", ".join(known_parts) if known_parts else "nada aún"

    brevity = (
        "Sé amable pero directo." if is_first
        else "Sé muy breve, una sola frase corta."
    )

    if next_field == "make":
        field_instruction = (
            "Pregunta por la marca del vehículo con ejemplos inline — "
            "una pregunta natural con anclas, sin lista numerada. "
            "Ejemplo del formato: '¿Es Toyota, Hyundai, Nissan, Honda u otra marca?'"
        )
    else:
        field_label = FIELD_LABELS.get(next_field, next_field)
        field_instruction = f"Pregunta SOLO por {field_label}. Una sola frase corta."

    return (
        f"El cliente está pidiendo un repuesto. Ya sabemos: {known_str}. "
        f"{field_instruction} "
        f"NO saludos, NO re-presentación, NO listas numeradas. {brevity}"
    )


def generate_response(situation: str, customer_message: str, context: dict = {}) -> str:
    if situation == "wait_acknowledgment":
        return WAIT_ACKNOWLEDGMENT

    if situation == "missing_fields":
        instruction = _build_missing_fields_instruction(context)
    elif situation == "confirmation_summary":
        instruction = _build_confirmation_instruction(context)
    elif situation == "correction_reminder":
        instruction = _build_correction_reminder_instruction(context)
    else:
        instruction = SITUATION_PROMPTS.get(situation, SITUATION_PROMPTS["unknown"])

    prompt = f"{instruction}\n\nMensaje del cliente: \"{customer_message}\""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ responder error ({situation}): {e}")
        return WAIT_ACKNOWLEDGMENT


def generate_quote_presentation(options: list, parsed: dict, final_prices: list) -> str:
    part = parsed.get("part", "")
    make = parsed.get("make", "")
    model = parsed.get("model", "")
    year = parsed.get("year", "")

    options_text = ""
    for i, (opt, price) in enumerate(zip(options, final_prices), 1):
        options_text += (
            f"Opción {i}: {opt['label']}\n"
            f"  Precio: ${price}\n"
            f"  Entrega: {opt['lead_time']}\n\n"
        )

    prompt = (
        f"Presenta estas opciones de repuesto al cliente de forma natural y profesional. "
        f"Pieza: {part} para {make} {model} {year}.\n\n"
        f"{options_text}"
        f"Recomienda la mejor opción si hay una clara. "
        f"Al final SIEMPRE incluye la instrucción de que responda con el número de opción. "
        f"Sé conciso. Usa el formato de lista numerada para las opciones."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ generate_quote_presentation error: {e}")
        # Fallback to structured format
        msg = f"🔩 *{part} — {make} {model} {year}*\n\nOpciones disponibles:\n\n"
        for i, (opt, price) in enumerate(zip(options, final_prices), 1):
            msg += f"*{i}.* {opt['label']} — ${price} · {opt['lead_time']}\n\n"
        if len(options) == 1:
            msg += "Responde con *1* para confirmar."
        else:
            nums = " o ".join(str(i) for i in range(1, len(options) + 1))
            msg += f"Responde con el número de opción ({nums})."
        return msg
