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
- Usa emojis con moderación (1-2 máximo si aplica)"""

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
        "No encontramos la pieza que buscó el cliente. "
        "Dale la noticia con empatía, ofrece avisarle si aparece algo, "
        "y sugiere que nos dé más detalles si aplica."
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


def _build_missing_fields_instruction(context: dict) -> str:
    known: dict = context.get("known", {})
    missing: list = context.get("missing", [])
    is_first = context.get("is_first_message", False)

    known_parts = [f"{k} = {v}" for k, v in known.items() if v]
    missing_labels = [FIELD_LABELS.get(f, f) for f in missing]

    known_str = ", ".join(known_parts) if known_parts else "nada aún"
    missing_str = " y ".join(missing_labels)

    brevity = (
        "Es el primer intercambio — sé amable pero directo."
        if is_first
        else "Ya estamos en conversación. Sé muy breve, una sola frase."
    )

    return (
        f"El cliente está pidiendo un repuesto. "
        f"Ya sabemos: {known_str}. "
        f"Aún nos falta: {missing_str}. "
        f"Pregunta SOLO lo que falta. "
        f"NO saludos, NO re-presentación, NO listas. {brevity}"
    )


def generate_response(situation: str, customer_message: str, context: dict = {}) -> str:
    if situation == "wait_acknowledgment":
        return WAIT_ACKNOWLEDGMENT

    if situation == "missing_fields":
        instruction = _build_missing_fields_instruction(context)
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
