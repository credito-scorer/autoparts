import os
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_sheet_client():
    """Authenticate and return the Google Sheets client."""
    creds = Credentials.from_service_account_file(
        "google_credentials.json",
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client

def get_order_log():
    """Return the order log worksheet."""
    client = get_sheet_client()
    sheet = client.open_by_key(os.getenv("GOOGLE_SHEETS_ID"))
    return sheet.sheet1

def get_supplier_sheet(sheet_id: str, worksheet_name: str = "Sheet1"):
    """
    Return a supplier's inventory worksheet.
    Each supplier gets their own Google Sheet.
    sheet_id: the supplier's Google Sheet ID
    """
    client = get_sheet_client()
    sheet = client.open_by_key(sheet_id)
    return sheet.worksheet(worksheet_name)

def search_supplier_sheet(sheet_id: str, parsed: dict) -> dict | None:
    """
    Search a supplier's Google Sheet for a matching part.
    
    Expected sheet columns:
    Part Name | Part Number | Make | Model | Year | Price | Stock | Lead Time | Notes
    
    Returns best match or None.
    """
    try:
        worksheet = get_supplier_sheet(sheet_id)
        records = worksheet.get_all_records()
        
        part = parsed.get("part", "").lower()
        make = parsed.get("make", "").lower()
        model = parsed.get("model", "").lower()
        year = str(parsed.get("year", ""))
        
        best_match = None
        best_score = 0
        
        for record in records:
            score = 0
            
            # Check part name match
            record_part = str(record.get("Part Name", "")).lower()
            if part in record_part or record_part in part:
                score += 3
            
            # Check make match
            record_make = str(record.get("Make", "")).lower()
            if make in record_make or record_make in make:
                score += 2
            
            # Check model match
            record_model = str(record.get("Model", "")).lower()
            if model in record_model or record_model in model:
                score += 2
                
            # Check year match
            record_year = str(record.get("Year", ""))
            if year in record_year or record_year == "all":
                score += 1
            
            # Check stock
            in_stock = str(record.get("Stock", "0"))
            if in_stock == "0" or in_stock.lower() == "no":
                continue
                
            if score > best_score:
                best_score = score
                best_match = record
        
        if best_score >= 3 and best_match:
            return {
                "supplier_name": best_match.get("Supplier Name", "Proveedor Local"),
                "part_name": best_match.get("Part Name"),
                "price": float(best_match.get("Price", 0)),
                "stock": best_match.get("Stock"),
                "lead_time": best_match.get("Lead Time", "1-2 días"),
                "notes": best_match.get("Notes", ""),
                "source": "google_sheet"
            }
            
        return None
        
    except Exception as e:
        print(f"Error searching supplier sheet {sheet_id}: {e}")
        return None


if __name__ == "__main__":
    # Quick connection test
    try:
        log = get_order_log()
        print(f"✅ Connected to order log: {log.title}")
        print(f"   Rows: {len(log.get_all_records())}")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
