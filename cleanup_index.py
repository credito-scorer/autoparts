#!/usr/bin/env python3
import json
from pathlib import Path

INDEX_FILE = Path("data/catalogue_index.json")
KEEP = {"Jefcar", "MAM General", "MAM Sensores", "Tocumen"}

with open(INDEX_FILE, encoding="utf-8") as f:
    parts = json.load(f)

before = len(parts)
cleaned = [p for p in parts if p.get("distributor") in KEEP]
after = len(cleaned)

with open(INDEX_FILE, "w", encoding="utf-8") as f:
    json.dump(cleaned, f, ensure_ascii=False, indent=2)

removed_names = {p.get("distributor") for p in parts} - KEEP
print(f"Before: {before} parts")
print(f"After:  {after} parts")
print(f"Removed: {before - after} parts from {sorted(removed_names)}")
