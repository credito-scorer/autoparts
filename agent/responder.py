import os
from anthropic import Anthropic
from utils.monitor import alert_claude_error

client = Anthropic()

SYSTEM_PROMPT = """Eres el agente de atención al cliente de Zeli, un servicio de repuestos automotrices en Santiago, Veraguas, Panamá.

PERSONALIDAD:
- Profesional, cálido y eficiente
- Suenas como un panameño real, no como un bot genérico
- Te adaptas al tono del cliente — si es formal, eres formal; si es casual, eres más relajado pero siempre profesional

IDIOMA — REGLAS ESTRICTAS:

USA siempre:
- Tuteo (tú): 'necesitas', 'tienes', 'puedes', 'dices'
  NUNCA voseo: jamás uses 'necesitás', 'tenés', 'decís', 'pasás'
- 'ya' para confirmar acción inmediata: 'ya te busco', 'ya te confirmo', 'ya lo tengo'
- 'ahorita' para indicar pronto: 'ahorita te digo'
- 'con gusto' como respuesta de cortesía
- 'listo' para confirmar
- 'dale' para asentir casualmente
- 'un momento' o 'un segundito' para pedir espera
- 'está bien' para confirmar que entendiste

EVITA completamente:
- 'al toque' — es argentino, nadie lo dice en Panamá
- 'che', 'boludo', 're-' como prefijo — argentino
- 'órale', 'chido', 'güey', 'ahorita' como 'ahora mismo' — mexicano
- 'te late', 'está cañón', 'órale pues' — mexicano/centroamericano, NO se usa en Panamá
- 'tío', 'tronco', 'macho', 'hostia' — español de España
- 'pana' como amigo — venezolano/colombiano
- 'bacano', 'parce', 'chévere' — colombiano/venezolano
- Voseo de cualquier tipo: PROHIBIDO 'vos', 'decime', 'mandame', 'avisame', 'contame'
  En su lugar usa tuteo: 'dime', 'mándame', 'avísame', 'cuéntame'
- Frases corporativas genéricas como 'estamos para servirle'
- Exceso de emojis — máximo 1 por mensaje
- Mensajes de más de 3-4 líneas salvo confirmación de pedido

EXPRESIONES NATURALES PARA CADA SITUACIÓN:
- Saludo inicial: 'Buenas, soy Zeli 👋' o '¡Hola! Somos Zeli'
- Confirmar recepción: 'Listo, ya lo tengo'
- Pedir espera: 'Un momento, ya te confirmo'
- Éxito: 'Perfecto, ya conseguimos tu pieza'
- No encontrado: 'Mira, no la tenemos ahorita pero te avisamos'
- Despedida: 'Con gusto, cualquier cosa aquí estamos'
- Asentir: 'Dale', 'Está bien', 'Listo'
- Agradecer paciencia: 'Gracias por la espera'

LONGITUD DE RESPUESTAS:
- Preguntas simples: 1 línea máximo
- Respuestas informativas: 2-3 líneas máximo
- Confirmación de pedido: puede ser más larga con el resumen
- Nunca re-saludes en medio de una conversación activa
- Nunca expliques de más — sé directo

CAMPOS A RECOPILAR — SOLO ESTOS CUATRO:
1. Pieza (qué parte necesita)
2. Marca del vehículo (Toyota, Nissan, etc.)
3. Modelo del vehículo (Hilux, Corolla, etc.)
4. Año del vehículo

PROHIBIDO preguntar sobre: versión del motor, cilindrada, si es original o genérico, color, transmisión, o cualquier otro detalle. Solo los cuatro campos. Cuando los tengas todos, para."""

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

WAIT_ACKNOWLEDGMENT = "Dale, tómate tu tiempo. Aquí estamos cuando estés listo. 👍"

LANGUAGE_GUARD = (
    "\n\nIMPORTANTE: USA SIEMPRE tuteo panameño (tú). PROHIBIDO voseo. "
    "Nunca: necesitás, tenés, decís, pasás, decime, mandame, avisame, contame. "
    "Correcto: dime, mándame, avísame, cuéntame. "
    "Prohibido: 'te late', 'órale', 'chévere', 'chido', jerga argentina, mexicana, colombiana o española. "
    "Habla como un panameño joven y servicial en una tienda de repuestos — directo, amable, sin florituras. "
    "Solo pregunta por los 4 campos: pieza, marca, modelo, año. "
    "Máximo 3-4 líneas. Natural y directo."
)


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

    prompt = f"{instruction}\n\nMensaje del cliente: \"{customer_message}\"{LANGUAGE_GUARD}"

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
        alert_claude_error(e, f"responder.generate_response[{situation}]")
        return WAIT_ACKNOWLEDGMENT


GOODBYE_COMPLETED = "Con gusto, aquí estamos cuando nos necesites. 👋"
GOODBYE_MID_FLOW  = "Claro, cuando necesites una pieza aquí estamos. 👋"


def _resolved_part(req: dict) -> str:
    """Return Luis's canonical part name if available, else the raw parsed part."""
    return (req.get("luis") or {}).get("part_identified") or req.get("part") or "?"


def generate_queue_confirmation(requests: list) -> str:
    """Generate a confirmation summary for one or more queued requests."""
    if len(requests) == 1:
        req   = requests[0]
        part  = _resolved_part(req)
        make  = req.get("make", "?")
        model = req.get("model", "?")
        year  = req.get("year", "?")
        instruction = (
            f"Genera un resumen de confirmación del pedido para el cliente. "
            f"Pieza: {part}. Vehículo: {make} {model} {year}. "
            f"Usa 🔩 para la pieza y 🚗 para el vehículo. "
            f"Pide que confirmen con 'sí' o que corrijan lo que esté mal. "
            f"Sé claro y conciso. No uses frases largas."
        )
    else:
        lines = "\n".join(
            f"🔩 {_resolved_part(r)} — {r.get('make')} {r.get('model')} {r.get('year')}"
            for r in requests
        )
        instruction = (
            f"El cliente pidió estas piezas:\n{lines}\n\n"
            f"Genera un resumen de confirmación del pedido completo. "
            f"Lista cada pieza con 🔩. Pide que confirmen con 'sí' o corrijan lo que esté mal. "
            f"Sé conciso, máximo 4 líneas."
        )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": instruction + LANGUAGE_GUARD}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ generate_queue_confirmation error: {e}")
        alert_claude_error(e, "responder.generate_queue_confirmation")
        if len(requests) == 1:
            req = requests[0]
            return (
                f"🔩 {_resolved_part(req)} — 🚗 {req.get('make')} {req.get('model')} {req.get('year')}\n\n"
                f"¿Todo correcto? Responde *sí* o corrígeme lo que esté mal."
            )
        lines = "\n".join(
            f"🔩 {_resolved_part(r)} — {r.get('make')} {r.get('model')} {r.get('year')}"
            for r in requests
        )
        return (
            f"Confirmemos tu pedido:\n\n{lines}\n\n"
            f"¿Todo correcto? Responde *sí* o corrígeme lo que esté mal."
        )


def generate_multi_sourcing_summary(
    found_parts: list, not_found_parts: list, vehicle: str
) -> str:
    """
    Generate a message about sourcing results.
    found_parts: list of (req, options) tuples
    not_found_parts: list of req dicts
    """
    not_found_names = ", ".join(r.get("part", "?") for r in not_found_parts)
    found_names     = ", ".join(r.get("part", "?") for r, _ in found_parts)

    prompt = (
        f"Resultados de búsqueda de repuestos{f' para {vehicle}' if vehicle else ''}:\n"
        + (f"✅ Encontrado(s): {found_names}\n" if found_names else "")
        + f"❌ No disponible(s): {not_found_names}\n\n"
        f"Informa al cliente con empatía. "
        + (f"Menciona que se enviará cotización para {found_names}. " if found_names else "")
        + f"Para {not_found_names}, SIEMPRE ofrece un siguiente paso "
        f"(avisar cuando haya stock, sugerir alternativa, o conectar con el equipo). "
        f"Sé conciso, máximo 3-4 oraciones."
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt + LANGUAGE_GUARD}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ generate_multi_sourcing_summary error: {e}")
        alert_claude_error(e, "responder.generate_multi_sourcing_summary")
        msg = ""
        if found_parts:
            msg += f"✅ Cotización en camino para: {found_names}.\n"
        msg += f"❌ No pudimos encontrar: {not_found_names}. Te avisamos cuando tengamos disponibilidad."
        return msg.strip()


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

    if len(options) == 1:
        closing_instruction = (
            "Al final pregunta SI quiere esa opción. "
            "Ejemplo correcto: '¿Te sirve esta opción? Responde sí o no.' "
            "NO pidas número de opción."
        )
    else:
        closing_instruction = (
            "Al final SIEMPRE incluye la instrucción de que responda con el número de opción. "
            "Ejemplo correcto: '¿Cuál te sirve? Solo dime el número.' "
        )

    prompt = (
        f"Presenta estas opciones de repuesto al cliente de forma natural y profesional. "
        f"Pieza: {part} para {make} {model} {year}.\n\n"
        f"{options_text}"
        f"Recomienda la mejor opción si hay una clara. "
        f"{closing_instruction} "
        f"Tono: panameño casual y directo. "
        f"PROHIBIDO: '¿Te late?', '¿Qué te parece?', 'órale', 'chévere', voseo. "
        f"Sé conciso. Usa el formato de lista numerada para las opciones."
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt + LANGUAGE_GUARD}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️ generate_quote_presentation error: {e}")
        alert_claude_error(e, "responder.generate_quote_presentation")
        # Fallback to structured format
        msg = f"🔩 *{part} — {make} {model} {year}*\n\nOpciones disponibles:\n\n"
        for i, (opt, price) in enumerate(zip(options, final_prices), 1):
            msg += f"*{i}.* {opt['label']} — ${price} · {opt['lead_time']}\n\n"
        if len(options) == 1:
            msg += "¿Te sirve esta opción? Responde *sí* o *no*."
        else:
            nums = " o ".join(str(i) for i in range(1, len(options) + 1))
            msg += f"Responde con el número de opción ({nums})."
        return msg
