import os
import json
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_client():
    """Get authenticated gspread client from env or file."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "google_credentials.json", scopes=SCOPES
        )
    
    return gspread.authorize(creds)

def get_order_log():
    client = get_client()
    return client.open_by_key(os.getenv("GOOGLE_SHEETS_ID")).sheet1


def get_stores_sheet():
    """Return the Stores worksheet, creating it with headers if it doesn't exist."""
    client = get_client()
    spreadsheet = client.open_by_key(os.getenv("GOOGLE_SHEETS_ID"))
    try:
        return spreadsheet.worksheet("Stores")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Stores", rows=100, cols=6)
        ws.append_row(["number", "name", "contact", "specialty", "tier", "active"])
        return ws

def search_supplier_sheet(sheet_id: str, parsed: dict) -> dict | None:
    try:
        client = get_client()
        sheet = client.open_by_key(sheet_id).sheet1
        records = sheet.get_all_records()
        
        part = parsed.get("part", "").lower()
        make = parsed.get("make", "").lower()
        model = parsed.get("model", "").lower()
        year = str(parsed.get("year", ""))
        
        for row in records:
            row_part = str(row.get("part", "")).lower()
            row_make = str(row.get("make", "")).lower()
            row_model = str(row.get("model", "")).lower()
            row_year = str(row.get("year", ""))
            
            if (part in row_part or row_part in part) and \
               (make in row_make or row_make in make) and \
               (model in row_model or row_model in model):
                
                price = float(row.get("price", 0))
                if price > 0:
                    return {
                        "supplier_name": row.get("supplier", "Local Supplier"),
                        "price": price,
                        "total_cost": price,
                        "lead_time": row.get("lead_time", "1-2 días"),
                        "source": "google_sheet",
                        "notes": row.get("notes", "")
                    }
        return None
        
    except Exception as e:
        print(f"Sheet search error: {e}")
        return None
