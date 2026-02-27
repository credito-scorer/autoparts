import os
from datetime import datetime
from connectors.sheets import get_order_log
from dotenv import load_dotenv

load_dotenv()

def log_request(data: dict):
    """
    Log every transaction to the Google Sheet order log.
    
    data format:
    {
        "customer_number": str,
        "raw_message": str,
        "parsed": dict,
        "status": str,
        "options": list (optional),
        "final_prices": list (optional),
        "chosen_option": int (optional)
    }
    """
    try:
        log = get_order_log()
        
        parsed = data.get("parsed") or {}
        options = data.get("options") or []
        final_prices = data.get("final_prices") or []
        
        # Build the row
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.get("raw_message", ""),
            data.get("customer_number", "").replace("whatsapp:", ""),
            parsed.get("part", ""),
            parsed.get("make", ""),
            parsed.get("model", ""),
            parsed.get("year", ""),
        ]
        
        # Add up to 3 supplier options
        for i in range(3):
            if i < len(options):
                opt = options[i]
                row.extend([
                    opt.get("supplier_name", ""),
                    opt.get("cost", ""),
                    opt.get("lead_time", "")
                ])
            else:
                row.extend(["", "", ""])
        
        # Final prices and status
        row.extend([
            ",".join([str(p) for p in final_prices]) if final_prices else "",
            data.get("chosen_option", ""),
            data.get("status", "received")
        ])
        
        log.append_row(row)
        print(f"📊 Logged to sheet: {parsed.get('part')} — {data.get('status')}")
        
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


if __name__ == "__main__":
    print("Testing logger...")
    log_request({
        "customer_number": "whatsapp:+50768001234",
        "raw_message": "necesito el alternador del hilux 08",
        "parsed": {
            "part": "Alternador",
            "make": "Toyota",
            "model": "Hilux",
            "year": "2008"
        },
        "options": [
            {
                "supplier_name": "USA (via Miami forwarder)",
                "cost": 210.0,
                "lead_time": "5-7 días"
            }
        ],
        "final_prices": [283.5],
        "status": "quoted"
    })
    print("✅ Check your Google Sheet")
