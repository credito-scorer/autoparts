import os
import json
import concurrent.futures
from connectors.sheets import search_supplier_sheet
from connectors.rockauto import search_rockauto
from connectors.whatsapp_supplier import (
    query_whatsapp_supplier,
    get_registered_suppliers
)
from dotenv import load_dotenv

load_dotenv()

def get_sheet_suppliers() -> list:
    """
    Returns list of suppliers with Google Sheets inventory.
    Add sheet suppliers to .env as:
    SHEET_SUPPLIERS=Name1|sheetid1|leadtime1,Name2|sheetid2|leadtime2
    """
    suppliers_raw = os.getenv("SHEET_SUPPLIERS", "")
    
    if not suppliers_raw:
        return []
    
    suppliers = []
    for entry in suppliers_raw.split(","):
        parts = entry.strip().split("|")
        if len(parts) == 3:
            suppliers.append({
                "name": parts[0],
                "sheet_id": parts[1],
                "lead_time": parts[2]
            })
    
    return suppliers

def search_single_sheet_supplier(supplier: dict, parsed: dict) -> dict | None:
    """Search one sheet supplier and return result."""
    try:
        result = search_supplier_sheet(supplier["sheet_id"], parsed)
        if result:
            result["lead_time"] = supplier.get("lead_time", "1-2 días")
            return result
        return None
    except Exception as e:
        print(f"Error searching {supplier['name']}: {e}")
        return None

def source_parts(parsed: dict) -> list:
    """
    Query all suppliers simultaneously and return
    all available options sorted by total cost.
    
    Returns list of supplier results, each containing:
    - supplier_name
    - price / total_cost
    - lead_time
    - source
    """
    
    results = []
    sheet_suppliers = get_sheet_suppliers()
    whatsapp_suppliers = get_registered_suppliers()
    
    print(f"\n🔍 Sourcing: {parsed.get('part')} for "
          f"{parsed.get('make')} {parsed.get('model')} {parsed.get('year')}")
    print(f"   Sheet suppliers: {len(sheet_suppliers)}")
    print(f"   WhatsApp suppliers: {len(whatsapp_suppliers)}")
    print(f"   USA supplier: enabled")
    
    # Run all queryable sources simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        
        # USA supplier (Claude-powered)
        futures["usa"] = executor.submit(search_rockauto, parsed)
        
        # Google Sheet suppliers
        for supplier in sheet_suppliers:
            key = f"sheet_{supplier['name']}"
            futures[key] = executor.submit(
                search_single_sheet_supplier, supplier, parsed
            )
        
        # Collect results
        for key, future in futures.items():
            try:
                result = future.result(timeout=30)
                if result:
                    results.append(result)
                    print(f"   ✅ {result['supplier_name']}: "
                          f"${result.get('total_cost') or result.get('price')} "
                          f"— {result['lead_time']}")
                else:
                    print(f"   ❌ {key}: not found")
            except Exception as e:
                print(f"   ❌ {key} error: {e}")
    
    # Send WhatsApp queries (async — responses come back via webhook)
    for supplier in whatsapp_suppliers:
        query_whatsapp_supplier(supplier, parsed)
    
    # Sort by total cost
    results.sort(key=lambda x: x.get("total_cost") or x.get("price") or 999)
    
    return results


if __name__ == "__main__":
    test_parsed = {
        "part": "Alternador",
        "make": "Toyota",
        "model": "Hilux",
        "year": "2008"
    }
    
    print("Testing sourcing engine...")
    results = source_parts(test_parsed)
    
    print(f"\n📦 Found {len(results)} option(s):")
    for i, r in enumerate(results, 1):
        cost = r.get('total_cost') or r.get('price')
        print(f"\n  Option {i}: {r['supplier_name']}")
        print(f"  Cost: ${cost}")
        print(f"  Lead time: {r['lead_time']}")
