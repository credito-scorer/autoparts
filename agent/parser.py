import os
import json
from anthropic import Anthropic
from dotenv import load_dotenv
from utils.monitor import alert_claude_error

try:
    from rapidfuzz import fuzz
    from rapidfuzz import process as rf_process
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

load_dotenv()

# ─── Three-layer make/model resolution ───────────────────────────────────────

MODEL_TO_MAKE: dict = {
    # Toyota
    "Hilux": "Toyota", "Fortuner": "Toyota", "Land Cruiser": "Toyota",
    "Prado": "Toyota", "Corolla": "Toyota", "Camry": "Toyota",
    "RAV4": "Toyota", "Yaris": "Toyota", "Sequoia": "Toyota",
    "Tacoma": "Toyota", "Tundra": "Toyota", "4Runner": "Toyota",
    "Avanza": "Toyota", "Rush": "Toyota", "Hiace": "Toyota",
    "Dyna": "Toyota", "FJ Cruiser": "Toyota", "Innova": "Toyota",
    "Vios": "Toyota", "Crown": "Toyota", "Sienna": "Toyota",
    "Matrix": "Toyota", "Highlander": "Toyota", "Prius": "Toyota",
    "LC200": "Toyota",
    # Nissan
    "Frontier": "Nissan", "Pathfinder": "Nissan", "X-Trail": "Nissan",
    "Qashqai": "Nissan", "Sentra": "Nissan", "Altima": "Nissan",
    "Maxima": "Nissan", "Micra": "Nissan", "March": "Nissan",
    "Titan": "Nissan", "Armada": "Nissan", "Murano": "Nissan",
    "Versa": "Nissan", "Navara": "Nissan", "NP300": "Nissan",
    "Rogue": "Nissan", "Kicks": "Nissan", "Juke": "Nissan",
    "Urvan": "Nissan", "Patrol": "Nissan",
    # Hyundai
    "Tucson": "Hyundai", "Santa Fe": "Hyundai", "Elantra": "Hyundai",
    "Accent": "Hyundai", "Sonata": "Hyundai", "i10": "Hyundai",
    "i20": "Hyundai", "i30": "Hyundai", "Creta": "Hyundai",
    "Palisade": "Hyundai", "Ioniq": "Hyundai", "Kona": "Hyundai",
    "Veloster": "Hyundai", "H100": "Hyundai", "Porter": "Hyundai",
    "H1": "Hyundai", "Grand I10": "Hyundai",
    # Honda
    "CR-V": "Honda", "Civic": "Honda", "Accord": "Honda",
    "Pilot": "Honda", "Passport": "Honda", "Ridgeline": "Honda",
    "Odyssey": "Honda", "HR-V": "Honda", "Fit": "Honda",
    "Jazz": "Honda", "City": "Honda", "Freed": "Honda",
    "Mobilio": "Honda", "Element": "Honda", "Insight": "Honda",
    "Stream": "Honda",
    # Honda aliases (user-typed shorthand)
    "HRV": "Honda", "CRV": "Honda",
    # Mitsubishi
    "Montero": "Mitsubishi", "Outlander": "Mitsubishi", "ASX": "Mitsubishi",
    "Lancer": "Mitsubishi", "Galant": "Mitsubishi", "Pajero": "Mitsubishi",
    "Eclipse": "Mitsubishi", "L200": "Mitsubishi", "Strada": "Mitsubishi",
    "Triton": "Mitsubishi", "Mirage": "Mitsubishi", "Space Wagon": "Mitsubishi",
    "Colt": "Mitsubishi",
    # Isuzu
    "D-Max": "Isuzu", "MU-X": "Isuzu", "Trooper": "Isuzu",
    "Rodeo": "Isuzu", "Elf": "Isuzu", "NKR": "Isuzu",
    "NHR": "Isuzu", "NPR": "Isuzu", "Forward": "Isuzu",
    # Isuzu aliases
    "DMax": "Isuzu", "MUX": "Isuzu",
    # Kia
    "Sportage": "Kia", "Sorento": "Kia", "Stinger": "Kia",
    "Optima": "Kia", "Rio": "Kia", "Seltos": "Kia",
    "Telluride": "Kia", "Carnival": "Kia", "Picanto": "Kia",
    "Soul": "Kia", "Cerato": "Kia", "K5": "Kia", "Niro": "Kia",
    # Suzuki
    "Jimny": "Suzuki", "Swift": "Suzuki", "Vitara": "Suzuki",
    "Grand Vitara": "Suzuki", "SX4": "Suzuki", "Ertiga": "Suzuki",
    "APV": "Suzuki", "Carry": "Suzuki", "Alto": "Suzuki",
    "Samurai": "Suzuki",
    # Subaru
    "Forester": "Subaru", "Outback": "Subaru", "Impreza": "Subaru",
    "Legacy": "Subaru", "XV": "Subaru", "Crosstrek": "Subaru",
    "WRX": "Subaru", "BRZ": "Subaru", "Tribeca": "Subaru",
    # RAM
    "RAM 700": "RAM", "RAM 1500": "RAM", "RAM 2500": "RAM", "RAM 3500": "RAM",
    "ProMaster": "RAM", "Dakota": "RAM",
    # Dodge
    "Durango": "Dodge", "Charger": "Dodge", "Challenger": "Dodge",
    "Journey": "Dodge", "Caravan": "Dodge", "Neon": "Dodge",
    "Caliber": "Dodge",
    # Jeep
    "Wrangler": "Jeep", "Cherokee": "Jeep", "Grand Cherokee": "Jeep",
    "Compass": "Jeep", "Renegade": "Jeep", "Gladiator": "Jeep",
    "Commander": "Jeep", "Liberty": "Jeep", "Patriot": "Jeep",
    # Ford
    "F-150": "Ford", "F150": "Ford", "Explorer": "Ford",
    "Escape": "Ford", "Expedition": "Ford", "Edge": "Ford",
    "Bronco": "Ford", "Ranger": "Ford", "Maverick": "Ford",
    "Mustang": "Ford", "Transit": "Ford", "EcoSport": "Ford",
    "Fusion": "Ford", "Focus": "Ford", "Fiesta": "Ford",
    # Chevrolet
    "Silverado": "Chevrolet", "Tahoe": "Chevrolet", "Suburban": "Chevrolet",
    "Traverse": "Chevrolet", "Equinox": "Chevrolet", "Trailblazer": "Chevrolet",
    "Colorado": "Chevrolet", "Blazer": "Chevrolet", "Camaro": "Chevrolet",
    "Malibu": "Chevrolet", "Spark": "Chevrolet", "Aveo": "Chevrolet",
    "Cruze": "Chevrolet", "Trax": "Chevrolet", "S10": "Chevrolet",
    "Captiva": "Chevrolet",
    # Mazda
    "CX-5": "Mazda", "CX-9": "Mazda", "CX-30": "Mazda",
    "Mazda 3": "Mazda", "Mazda 6": "Mazda", "BT-50": "Mazda",
    "MX-5": "Mazda", "CX-7": "Mazda",
    # Chery
    "Tiggo": "Chery", "Tiggo 3": "Chery", "Tiggo 5": "Chery",
    "Tiggo 7": "Chery", "Tiggo 8": "Chery", "Arrizo": "Chery",
    "QQ": "Chery", "Grand Tiger": "Chery", "Tigo": "Chery",
    # JAC
    "JAC S3": "JAC", "JAC S4": "JAC", "JAC S5": "JAC",
    "JAC T6": "JAC", "JAC T8": "JAC", "Sei 3": "JAC",
    "Sei 4": "JAC", "Sei 7": "JAC",
    # JAC aliases (without prefix)
    "T6": "JAC", "T8": "JAC", "Sei 2": "JAC", "Sei2": "JAC",
    # BYD
    "BYD Song": "BYD", "BYD Tang": "BYD", "BYD Han": "BYD",
    "Atto 3": "BYD", "BYD Yuan": "BYD", "BYD Dolphin": "BYD",
    "BYD Seal": "BYD", "BYD F3": "BYD", "BYD G6": "BYD",
    # BYD aliases (without prefix / misspellings)
    "Dolphin": "BYD", "Dolfin": "BYD", "Atto": "BYD", "Seal": "BYD",
    # Great Wall
    "Wingle": "Great Wall", "Poer": "Great Wall", "Jolion": "Great Wall",
    "Haval H6": "Great Wall", "Cannon": "Great Wall",
    # Geely
    "Emgrand": "Geely", "Coolray": "Geely", "Okavango": "Geely",
    "Azkarra": "Geely", "EC7": "Geely", "Tugella": "Geely",
    # DFSK
    "Glory 580": "DFSK", "Glory": "DFSK", "C35": "DFSK", "Seres": "DFSK",
    # Lifan
    "Lifan 620": "Lifan", "Lifan 820": "Lifan", "X50": "Lifan",
    "X60": "Lifan", "X70": "Lifan",
    # Zotye
    "T600": "Zotye", "Z560": "Zotye", "Z360": "Zotye",
    # Foton
    "Tunland": "Foton", "Thunder": "Foton", "Toano": "Foton",
    "Midi": "Foton",
    # FAW
    "FAW B50": "FAW", "FAW T77": "FAW", "FAW V80": "FAW",
}

CHINESE_BRAND_VARIANTS: dict = {
    "cheri": "Chery", "cherry": "Chery", "chery": "Chery",
    "yac": "JAC", "jac": "JAC",
    "byd": "BYD",
    "great wall": "Great Wall", "greatwall": "Great Wall", "gwm": "Great Wall",
    "haval": "Great Wall",
    "geely": "Geely", "gili": "Geely",
    "dfsk": "DFSK", "dongfeng": "DFSK", "dong feng": "DFSK",
    "lifan": "Lifan",
    "zotye": "Zotye", "zoti": "Zotye",
    "foton": "Foton", "fotton": "Foton",
    "faw": "FAW",
}

ALL_MAKES: list = [
    "Toyota", "Nissan", "Hyundai", "Honda", "Mitsubishi", "Isuzu",
    "Kia", "Suzuki", "Subaru", "RAM", "Dodge", "Jeep", "Ford",
    "Chevrolet", "Mazda", "Chery", "JAC", "BYD", "Great Wall",
    "Geely", "DFSK", "Lifan", "Zotye", "Foton", "FAW",
    "Volkswagen", "Mercedes-Benz", "BMW", "Audi", "Lexus",
]

CHINESE_MAKES: set = {
    "Chery", "JAC", "BYD", "Great Wall", "Geely", "DFSK",
    "Lifan", "Zotye", "Foton", "FAW",
}

_MAKE_THRESHOLD         = 80   # standard brands
_CHINESE_MAKE_THRESHOLD = 75   # Chinese brands (more typo-prone)
_MODEL_THRESHOLD        = 78   # model name fuzzy match


def _normalize_known_make(make_text: str) -> str | None:
    """Normalize a make string (from Claude or user) to canonical form."""
    if not make_text:
        return None
    lowered = make_text.strip().lower()
    if lowered in CHINESE_BRAND_VARIANTS:
        return CHINESE_BRAND_VARIANTS[lowered]
    for m in ALL_MAKES:
        if m.lower() == lowered:
            return m
    if not _HAS_RAPIDFUZZ:
        return make_text.strip()
    all_makes_lower = [m.lower() for m in ALL_MAKES]
    result = rf_process.extractOne(lowered, all_makes_lower, scorer=fuzz.WRatio)
    if result and result[1] >= _MAKE_THRESHOLD:
        matched = result[0]
        for m in ALL_MAKES:
            if m.lower() == matched:
                return m
    return make_text.strip()


def _fuzzy_match_make(raw_message: str) -> str | None:
    """Scan raw message text for any vehicle make using fuzzy matching."""
    if not raw_message:
        return None
    lowered = raw_message.lower()
    words = lowered.split()
    # Check Chinese brand variants — exact word or multi-word match
    for variant, canonical in CHINESE_BRAND_VARIANTS.items():
        if " " in variant:
            if variant in lowered:
                return canonical
        elif variant in words:
            return canonical
    if not _HAS_RAPIDFUZZ:
        return None
    all_makes_lower   = [m.lower() for m in ALL_MAKES]
    chinese_lower     = [m.lower() for m in sorted(CHINESE_MAKES)]
    chinese_list      = sorted(CHINESE_MAKES)
    for word in words:
        if len(word) < 3:
            continue
        result_cn = rf_process.extractOne(word, chinese_lower, scorer=fuzz.WRatio)
        if result_cn and result_cn[1] >= _CHINESE_MAKE_THRESHOLD:
            matched = result_cn[0]
            for m in chinese_list:
                if m.lower() == matched:
                    return m
        result = rf_process.extractOne(word, all_makes_lower, scorer=fuzz.WRatio)
        if result and result[1] >= _MAKE_THRESHOLD:
            matched = result[0]
            for m in ALL_MAKES:
                if m.lower() == matched:
                    return m
    return None


def _fuzzy_match_model(model_text: str) -> str | None:
    """Fuzzy match model_text against known models; return canonical key or None."""
    if not _HAS_RAPIDFUZZ or not model_text:
        return None
    all_models = list(MODEL_TO_MAKE.keys())
    result = rf_process.extractOne(model_text, all_models, scorer=fuzz.WRatio)
    if result and result[1] >= _MODEL_THRESHOLD:
        return result[0]
    return None


def resolve_make_model(parsed: dict, raw_message: str) -> dict:
    """
    Apply three-layer make/model resolution in-place and return parsed dict.
    - Layer 1: MODEL_TO_MAKE exact + case-insensitive dict lookup
    - Layer 2: Rapidfuzz fuzzy match on model name or raw message words
    - Layer 3: Already handled by the enhanced Claude prompt
    """
    make  = parsed.get("make")
    model = parsed.get("model")

    # Normalize make if Claude already provided one
    if make:
        normalized = _normalize_known_make(make)
        if normalized:
            parsed["make"] = normalized
            make = normalized

    # Layer 1: exact model → make dict lookup
    if not make and model:
        inferred = MODEL_TO_MAKE.get(model)
        if not inferred:
            for k, v in MODEL_TO_MAKE.items():
                if k.lower() == model.lower():
                    inferred = v
                    parsed["model"] = k
                    break
        if inferred:
            parsed["make"] = inferred
            make = inferred

    # Layer 2a: fuzzy model → make lookup
    if not make and model:
        best_model = _fuzzy_match_model(model)
        if best_model:
            parsed["model"] = best_model
            parsed["make"]  = MODEL_TO_MAKE[best_model]
            make = parsed["make"]

    # Layer 2b: scan raw message for make name
    if not make:
        found = _fuzzy_match_make(raw_message)
        if found:
            parsed["make"] = found

    return parsed

# ─────────────────────────────────────────────────────────────────────────────

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

Inferencia de marca: Si el modelo es bien conocido (ej: 'Hilux' → Toyota, 'Frontier' → Nissan, 'Tiggo' → Chery, 'Creta' → Hyundai), infiere la marca aunque no esté explícita. Usa tu conocimiento de marcas comunes en Panamá (incluye chinas: Chery, JAC, BYD, Great Wall, Geely, DFSK).
Regla crítica: Para los demás campos (pieza, modelo, año), devuelve null si no están claramente indicados. Nunca adivines la pieza, modelo o año.
Devuelve el JSON con los campos que puedas extraer (los demás en null).
Responde con null SOLO si el mensaje es puramente conversacional (saludo, ok, gracias, etc.) sin ninguna mención de piezas o vehículos.
No incluyas explicaciones, solo el JSON."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
    except Exception as e:
        alert_claude_error(e, "parser.parse_request")
        return None

    raw = response.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    raw = raw.strip()

    if not raw or raw == "null":
        return None

    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            resolve_make_model(result, message)
        return result
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

Inferencia de marca: Si el modelo es bien conocido (ej: 'Hilux' → Toyota, 'Tiggo' → Chery, 'Frontier' → Nissan), infiere la marca aunque no esté explícita.
Regla crítica: Para los demás campos, devuelve null si no están claramente indicados. Nunca adivines la pieza, modelo o año.
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
            items = [r for r in result if r.get("part")]
            for item in items:
                resolve_make_model(item, message)
            return items
        if isinstance(result, dict) and result.get("part"):
            resolve_make_model(result, message)
            return [result]
        return []
    except Exception as e:
        alert_claude_error(e, "parser.parse_request_multi")
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
        result = json.loads(raw)
        if isinstance(result, dict) and result.get("make"):
            result["make"] = _normalize_known_make(result["make"]) or result["make"]
        return result
    except Exception as e:
        alert_claude_error(e, "parser.extract_partial")
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
    except Exception as e:
        alert_claude_error(e, "parser.interpret_option_choice")
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
    except Exception as e:
        alert_claude_error(e, "parser.parse_correction")
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
    except Exception as e:
        alert_claude_error(e, "parser.detect_needs_human")
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
