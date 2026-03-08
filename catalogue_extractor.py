#!/usr/bin/env python3
"""
Zeli Technologies — Distributor Catalogue Extractor
Processes distributor PDFs → structured JSON + CSV for search index

Usage:
    python catalogue_extractor.py --pdf path/to/catalogue.pdf --distributor "Nombre Distribuidor"
    python catalogue_extractor.py --folder ./catalogues/  # batch process all PDFs in folder

Output:
    - data/catalogue_index.json  (master search index, all distributors combined)
    - data/catalogue_index.csv   (same data, spreadsheet-friendly)
    - data/raw/<distributor>.json (per-distributor raw extract)
"""

import os
import re
import json
import argparse
import hashlib
from pathlib import Path
from datetime import datetime

import pdfplumber
import pandas as pd

OUTPUT_DIR = Path("data")
RAW_DIR = OUTPUT_DIR / "raw"
INDEX_FILE = OUTPUT_DIR / "catalogue_index.json"
CSV_FILE = OUTPUT_DIR / "catalogue_index.csv"

OUTPUT_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)


# ── EXTRACTION ──────────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> list[dict]:
    """Extract all text from PDF, page by page."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"  📄 {total} pages found")
        for i, page in enumerate(pdf.pages, 1):
            if i % 20 == 0:
                print(f"  ... processing page {i}/{total}")
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            pages.append({
                "page": i,
                "text": text,
                "tables": tables
            })
    return pages


def extract_tables_from_pdf(pdf_path: str) -> list[dict]:
    """Extract structured table data from PDF — best for catalogue PDFs with columns."""
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            if i % 20 == 0:
                print(f"  ... extracting tables page {i}/{total}")
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    for row in table:
                        if row and any(cell for cell in row if cell):
                            rows.append({
                                "page": i,
                                "row": [str(cell).strip() if cell else "" for cell in row]
                            })
    return rows


# ── PARSING ─────────────────────────────────────────────────────────────────

# Common patterns in parts catalogues
PRICE_PATTERN = re.compile(r'\$?\s*(\d{1,6}(?:[.,]\d{2,3})?)')
PART_NUMBER_PATTERN = re.compile(r'\b([A-Za-z0-9]{4,20}(?:[-/][A-Za-z0-9]{2,10})*)\b')
YEAR_PATTERN = re.compile(r'\b(19[8-9]\d|20[0-3]\d)\b')

# Common makes in Panama market
MAKES = [
    "toyota", "hyundai", "kia", "nissan", "mitsubishi", "honda", "mazda",
    "chevrolet", "ford", "dodge", "jeep", "suzuki", "subaru", "isuzu",
    "volkswagen", "vw", "mercedes", "bmw", "audi", "fiat", "renault",
    "peugeot", "daihatsu", "chery", "geely", "great wall", "jac",
    "vigo", "revo", "hilux", "mazada", "fortuner", "land cruiser",
    "corolla", "yaris", "camry", "accent", "elantra", "cerato",
    "sportage", "frontier", "patrol", "l200", "pajero", "lancer"
]

MAKES_PATTERN = re.compile(r'\b(' + '|'.join(MAKES) + r')\b', re.IGNORECASE)


def parse_row_to_part(row: list[str], page: int, distributor: str) -> dict | None:
    """
    Try to parse a table row into a structured part entry.
    Catalogues vary widely — this tries multiple heuristics.
    """
    if not row or len(row) < 2:
        return None

    combined = " ".join(row).strip()
    if len(combined) < 5:
        return None

    # Skip header rows
    header_indicators = ["descripcion", "descripción", "precio", "referencia",
                         "codigo", "código", "marca", "modelo", "part", "price"]
    if any(h in combined.lower() for h in header_indicators) and len(combined) < 80:
        return None

    part = {
        "id": hashlib.md5(f"{distributor}{combined}{page}".encode()).hexdigest()[:10],
        "distributor": distributor,
        "page": page,
        "raw": combined,
        "description": "",
        "part_number": "",
        "price": None,
        "make": "",
        "model": "",
        "year_range": "",
        "category": "",
        "updated_at": datetime.now().isoformat()
    }

    # Try to find price — usually last numeric column
    for cell in reversed(row):
        price_match = PRICE_PATTERN.search(cell)
        if price_match:
            price_str = price_match.group(1).replace(",", ".")
            try:
                part["price"] = float(price_str)
                break
            except ValueError:
                continue

    # Description — usually longest text cell
    text_cells = [c for c in row if c and len(c) > 8 and not PRICE_PATTERN.fullmatch(c.strip())]
    if text_cells:
        part["description"] = max(text_cells, key=len)

    # Part number — alphanumeric codes
    pn_match = PART_NUMBER_PATTERN.search(combined)
    if pn_match:
        part["part_number"] = pn_match.group(1)

    # Make detection
    make_match = MAKES_PATTERN.search(combined)
    if make_match:
        part["make"] = make_match.group(1).title()

    # Year range
    years = YEAR_PATTERN.findall(combined)
    if len(years) >= 2:
        part["year_range"] = f"{min(years)}-{max(years)}"
    elif len(years) == 1:
        part["year_range"] = years[0]

    # Skip category header rows (all-uppercase, no price, short)
    if part["price"] is None and len(combined) < 60 and combined == combined.upper():
        return None

    # Skip rows that look empty/useless
    if not part["description"] and not part["part_number"]:
        return None

    return part


def parse_text_lines(pages: list[dict], distributor: str) -> list[dict]:
    """
    Fallback parser for text-based PDFs where table extraction doesn't work well.
    Tries to find part-like lines in raw text.
    """
    parts = []
    for page_data in pages:
        lines = page_data["text"].split("\n")
        for line in lines:
            line = line.strip()
            if len(line) < 10:
                continue
            # Must have either a price or a part number to be worth indexing
            has_price = PRICE_PATTERN.search(line)
            has_part_num = PART_NUMBER_PATTERN.search(line)
            if not (has_price or has_part_num):
                continue

            part = {
                "id": hashlib.md5(f"{distributor}{line}{page_data['page']}".encode()).hexdigest()[:10],
                "distributor": distributor,
                "page": page_data["page"],
                "raw": line,
                "description": line,
                "part_number": "",
                "price": None,
                "make": "",
                "model": "",
                "year_range": "",
                "category": "",
                "updated_at": datetime.now().isoformat()
            }

            if has_price:
                try:
                    part["price"] = float(has_price.group(1).replace(",", "."))
                except ValueError:
                    pass

            if has_part_num:
                part["part_number"] = has_part_num.group(1)

            make_match = MAKES_PATTERN.search(line)
            if make_match:
                part["make"] = make_match.group(1).title()

            years = YEAR_PATTERN.findall(line)
            if len(years) >= 2:
                part["year_range"] = f"{min(years)}-{max(years)}"
            elif years:
                part["year_range"] = years[0]

            parts.append(part)

    return parts


# ── INDEX MANAGEMENT ────────────────────────────────────────────────────────

def load_index() -> list[dict]:
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            return json.load(f)
    return []


def save_index(parts: list[dict]):
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(parts, f, ensure_ascii=False, indent=2)
    df = pd.DataFrame(parts)
    df.to_csv(CSV_FILE, index=False, encoding="utf-8")
    print(f"  💾 Saved {len(parts)} total parts to index")


def merge_into_index(new_parts: list[dict], distributor: str):
    """Merge new parts into master index, replacing old entries for same distributor."""
    existing = load_index()
    # Remove old entries for this distributor
    existing = [p for p in existing if p.get("distributor") != distributor]
    existing.extend(new_parts)
    save_index(existing)
    return existing


# ── MAIN PROCESSING ──────────────────────────────────────────────────────────

def process_pdf(pdf_path: str, distributor: str) -> list[dict]:
    print(f"\n🔍 Processing: {pdf_path}")
    print(f"   Distributor: {distributor}")

    parts = []

    # Strategy 1: Table extraction (best for structured catalogues)
    print("  → Attempting table extraction...")
    table_rows = extract_tables_from_pdf(pdf_path)
    if table_rows:
        print(f"  → Found {len(table_rows)} table rows")
        for item in table_rows:
            part = parse_row_to_part(item["row"], item["page"], distributor)
            if part:
                parts.append(part)
        print(f"  → Parsed {len(parts)} parts from tables")

    # Strategy 2: Text line extraction (fallback)
    if len(parts) < 10:
        print("  → Table extraction sparse, trying text extraction...")
        pages = extract_text_from_pdf(pdf_path)
        text_parts = parse_text_lines(pages, distributor)
        print(f"  → Parsed {len(text_parts)} parts from text")
        # Merge, avoid duplicates by id
        existing_ids = {p["id"] for p in parts}
        parts.extend([p for p in text_parts if p["id"] not in existing_ids])

    # Save raw extract
    raw_file = RAW_DIR / f"{distributor.replace(' ', '_').lower()}.json"
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(parts, f, ensure_ascii=False, indent=2)
    print(f"  💾 Raw extract saved: {raw_file}")

    # Merge into master index
    merge_into_index(parts, distributor)
    print(f"  ✅ Done — {len(parts)} parts indexed for {distributor}")

    return parts


def process_folder(folder_path: str):
    """Batch process all PDFs in a folder. Uses filename as distributor name."""
    folder = Path(folder_path)
    pdfs = list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))
    if not pdfs:
        print(f"❌ No PDFs found in {folder_path}")
        return

    # Load optional name map
    name_map = {}
    distributors_file = folder / "distributors.json"
    if distributors_file.exists():
        with open(distributors_file, encoding="utf-8") as f:
            name_map = json.load(f)
        print(f"📋 Loaded distributors.json ({len(name_map)} entries)")

    print(f"📂 Found {len(pdfs)} PDFs to process")
    for pdf in pdfs:
        if pdf.name in name_map:
            distributor = name_map[pdf.name]
        else:
            distributor = pdf.stem.replace("_", " ").replace("-", " ").title()
        process_pdf(str(pdf), distributor)
    print(f"\n✅ Batch complete. Index at: {INDEX_FILE}")


def print_stats():
    """Print current index statistics."""
    parts = load_index()
    if not parts:
        print("Index is empty.")
        return
    df = pd.DataFrame(parts)
    print(f"\n📊 Index Statistics")
    print(f"   Total parts: {len(parts)}")
    print(f"   Distributors: {df['distributor'].nunique()}")
    for dist, count in df['distributor'].value_counts().items():
        print(f"     • {dist}: {count} parts")
    print(f"   Parts with price: {df['price'].notna().sum()}")
    print(f"   Parts with make: {(df['make'] != '').sum()}")
    print(f"   Index file: {INDEX_FILE}")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zeli Catalogue Extractor")
    parser.add_argument("--pdf", help="Path to single PDF")
    parser.add_argument("--distributor", help="Distributor name (used with --pdf)")
    parser.add_argument("--folder", help="Folder with multiple PDFs to batch process")
    parser.add_argument("--stats", action="store_true", help="Show index statistics")

    args = parser.parse_args()

    if args.stats:
        print_stats()
    elif args.folder:
        process_folder(args.folder)
    elif args.pdf:
        if not args.distributor:
            distributor = Path(args.pdf).stem.replace("_", " ").title()
            print(f"No --distributor given, using filename: '{distributor}'")
        else:
            distributor = args.distributor
        process_pdf(args.pdf, distributor)
    else:
        parser.print_help()
