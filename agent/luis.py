import os
import json
from anthropic import Anthropic
from utils.monitor import alert_claude_error

client = Anthropic()

# Load knowledge base once at module level
_KB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'luis_knowledge_base.txt')

def _load_knowledge_base() -> str:
    try:
        with open(_KB_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"⚠️ Luis: could not load knowledge base: {e}")
        return ""

_KNOWLEDGE_BASE = _load_knowledge_base()

_SYSTEM_PROMPT = f"""You are Luis, the parts intelligence engine for Zeli Technologies in Santiago, Panama.

You are NEVER customer-facing. You work silently behind Zeli, the customer-facing WhatsApp bot.
Your job is to reason about auto parts requests and return structured JSON recommendations to Zeli.

Here is your complete knowledge base:

{_KNOWLEDGE_BASE}

CRITICAL BEHAVIOR RULES:
- NEVER commit to a timeline or price you cannot back up
- NEVER recommend generic timing belts or clutch kits — safety-critical parts only get Tier 1 or Tier 2 brands
- ALWAYS clarify diesel vs gas before sourcing any engine part
- ALWAYS flag Chinese makes as higher sourcing difficulty
- ALWAYS suggest complete kit when clutch, timing belt, or water pump is requested
- If uncertain, "déjame verificar" is a valid and correct answer
- Ask maximum 2 clarifying questions at once — never interrogate the customer

You must ALWAYS respond with valid JSON only. No explanations outside the JSON.
"""

_USER_PROMPT_TEMPLATE = """A customer sent this request via WhatsApp:
"{message}"

The parser already extracted:
- part: {part}
- make: {make}
- model: {model}
- year: {year}

Customer history (last interactions): {history}

Analyze this request using your knowledge base and return ONLY this JSON:
{{
    "part_identified": "canonical part name in Spanish or null",
    "confidence": "high | medium | low",
    "needs_clarification": true or false,
    "clarifying_questions": ["question 1 in Spanish", "question 2 in Spanish"],
    "brand_recommendations": ["Tier 1 brand", "Tier 2 brand"],
    "sourcing_path": "catalogue | panama_city | miami | unknown",
    "lead_time": "mismo día | 1-2 días | 3-5 días | 5-7 días | desconocido",
    "zeli_message": "The exact message Zeli should send to the customer in natural Panamanian Spanish",
    "ronel_flag": true or false,
    "ronel_note": "Note for Ronel if this needs human attention, or null",
    "safety_warning": "Any safety concern Ronel should know about, or null"
}}

Rules for zeli_message:
- If needs_clarification is true: zeli_message should ask the clarifying questions naturally
- If needs_clarification is false: zeli_message should confirm receipt and set expectations on sourcing
- Always warm, conversational, natural Panamanian Spanish
- Never mention Luis, never mention internal systems
- Maximum 3 sentences
"""


def consult_luis(parsed: dict, raw_message: str, customer_history: list = None) -> dict | None:
    """
    Takes a parsed request dict from parser.py and enriches it with
    Luis's parts intelligence.
    
    Returns enriched dict with sourcing recommendation and zeli_message,
    or None if Luis call fails.
    """
    history_str = "None" if not customer_history else json.dumps(customer_history[-5:], ensure_ascii=False)

    prompt = _USER_PROMPT_TEMPLATE.format(
        message=raw_message,
        part=parsed.get('part') or 'unknown',
        make=parsed.get('make') or 'unknown',
        model=parsed.get('model') or 'unknown',
        year=parsed.get('year') or 'unknown',
        history=history_str
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        # Merge Luis output back into parsed dict
        parsed['luis'] = result
        parsed['zeli_message'] = result.get('zeli_message')
        parsed['ronel_flag'] = result.get('ronel_flag', False)
        parsed['ronel_note'] = result.get('ronel_note')
        parsed['sourcing_path'] = result.get('sourcing_path')
        parsed['lead_time'] = result.get('lead_time')
        parsed['brand_recommendations'] = result.get('brand_recommendations', [])

        return parsed

    except Exception as e:
        alert_claude_error(e, "luis.consult_luis")
        return None


if __name__ == "__main__":
    # Quick test
    test_cases = [
        {
            "msg": "necesito el alternador del hilux 08",
            "parsed": {"part": "alternador", "make": "Toyota", "model": "Hilux", "year": "2008"}
        },
        {
            "msg": "busco pastillas de freno corolla 2015 delanteras",
            "parsed": {"part": "pastillas de freno", "make": "Toyota", "model": "Corolla", "year": "2015"}
        },
        {
            "msg": "necesito la correa del tiggo 5",
            "parsed": {"part": "correa", "make": "Chery", "model": "Tiggo 5", "year": None}
        },
    ]

    for tc in test_cases:
        print(f"\n{'='*60}")
        print(f"Input: {tc['msg']}")
        result = consult_luis(tc['parsed'], tc['msg'])
        if result and result.get('luis'):
            luis = result['luis']
            print(f"Confidence: {luis.get('confidence')}")
            print(f"Needs clarification: {luis.get('needs_clarification')}")
            print(f"Brands: {luis.get('brand_recommendations')}")
            print(f"Sourcing: {luis.get('sourcing_path')} — {luis.get('lead_time')}")
            print(f"Zeli says: {luis.get('zeli_message')}")
            if luis.get('ronel_flag'):
                print(f"⚠️  Ronel note: {luis.get('ronel_note')}")
