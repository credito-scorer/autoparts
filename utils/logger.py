import os
import uuid
from datetime import datetime, timezone, timedelta
from connectors.sheets import get_order_log
from dotenv import load_dotenv

load_dotenv()

# Panama time is UTC-5
_PANAMA_TZ = timezone(timedelta(hours=-5))

HEADERS = [
    "transaction_id",       # 1
    "timestamp",            # 2
    "customer_number",      # 3
    "raw_message",          # 4
    "part_parsed",          # 5
    "part_normalized",      # 6  manual annotation
    "make",                 # 7
    "model",                # 8
    "year",                 # 9
    "resolution_method",    # 10
    "supplier_used",        # 11
    "source_cost",          # 12  manual fill
    "quote_sent",           # 13
    "margin_usd",           # 14  manual fill
    "margin_pct",           # 15  manual fill
    "lead_time_promised",   # 16
    "lead_time_actual",     # 17  manual fill
    "status",               # 18
    "chosen_option",        # 19
    "sourcing_sources",     # 20
    "how_described",        # 21  manual fill
    "why_rejected",         # 22  manual fill
    "owner_notes",          # 23
]


def _panama_now() -> str:
    return datetime.now(_PANAMA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_headers(sheet) -> None:
    """Write header row if cell A1 is not 'transaction_id'."""
    try:
        a1 = sheet.acell("A1").value
        if not a1 or a1 != "transaction_id":
            sheet.insert_row(HEADERS, 1)
    except Exception:
        pass


def log_request(data: dict):
    """
    Log a transaction row using the 23-column ontology schema.

    Recognised keys in data:
        customer_number, raw_message, parsed (dict), status,
        quote_price, lead_time, chosen_option, supplier_used, owner_notes
    """
    try:
        sheet = get_order_log()
        _ensure_headers(sheet)

        parsed = data.get("parsed") or {}
        customer = str(data.get("customer_number", "")).replace("whatsapp:", "").strip()

        row = [
            str(uuid.uuid4()),                              # 1  transaction_id
            _panama_now(),                                  # 2  timestamp
            customer,                                       # 3  customer_number
            str(data.get("raw_message", "") or ""),         # 4  raw_message
            str(parsed.get("part", "") or ""),              # 5  part_parsed
            "",                                             # 6  part_normalized (manual)
            str(parsed.get("make", "") or ""),              # 7  make
            str(parsed.get("model", "") or ""),             # 8  model
            str(parsed.get("year", "") or ""),              # 9  year
            str(parsed.get("resolution_method", "") or ""), # 10 resolution_method
            str(data.get("supplier_used", "") or ""),       # 11 supplier_used
            "",                                             # 12 source_cost (manual)
            str(data.get("quote_price", "") or ""),         # 13 quote_sent
            "",                                             # 14 margin_usd (manual)
            "",                                             # 15 margin_pct (manual)
            str(data.get("lead_time", "") or ""),           # 16 lead_time_promised
            "",                                             # 17 lead_time_actual (manual)
            str(data.get("status", "received")),            # 18 status
            str(data.get("chosen_option", "") or ""),       # 19 chosen_option
            "",                                             # 20 sourcing_sources
            "",                                             # 21 how_described (manual)
            "",                                             # 22 why_rejected (manual)
            str(data.get("owner_notes", "") or ""),         # 23 owner_notes
        ]

        sheet.append_row(row)
        print(f"📊 Logged: {parsed.get('part') or data.get('status', '?')} — {data.get('status')}")

    except Exception as e:
        print(f"⚠️ Logging error (non-critical): {e}")
        try:
            from utils.monitor import alert_sheets_failed
            parsed = data.get("parsed") or {}
            summary = (
                f"{parsed.get('part','')} {parsed.get('make','')} "
                f"{parsed.get('model','')} {parsed.get('year','')} "
                f"— {data.get('status','?')} — {data.get('customer_number','?')}"
            ).strip()
            alert_sheets_failed(e, summary)
        except Exception:
            pass


def log_event(event_type: str, data: dict):
    """
    Log a non-transaction event (image_received, escalation_fired, etc.)
    using the 23-column schema. Part/sourcing fields are left empty;
    event detail goes into owner_notes.
    """
    log_request({
        "customer_number": data.get("customer_number", ""),
        "raw_message":     data.get("raw_message", ""),
        "parsed":          {},
        "status":          event_type,
        "owner_notes":     event_type,
    })


if __name__ == "__main__":
    print("Testing logger...")
    log_request({
        "customer_number": os.getenv("TEST_CUSTOMER_NUMBER", "whatsapp:+50768001234"),
        "raw_message": "necesito el alternador del hilux 08",
        "parsed": {
            "part": "Alternador",
            "make": "Toyota",
            "model": "Hilux",
            "year": "2008",
            "resolution_method": "layer1_dict",
        },
        "status": "quoted",
        "quote_price": "195",
        "lead_time": "3-5 días",
    })
    print("✅ Check your Google Sheet")
