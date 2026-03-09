#!/usr/bin/env python3
"""
Zeli Technologies — Image Price List Extractor
Processes JPG photos of physical price lists through Claude vision API
and outputs structured part data into the same index as catalogue_extractor.py

Usage:
    python image_extractor.py --jpg path/to/image.jpg --distributor "Tocumen"
    python image_extractor.py --folder ./catalogues/ --distributor "Tocumen"
"""

import os
import re
import json
import base64
import hashlib
import argparse
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
import anthropic

from catalogue_extractor import merge_into_index

load_dotenv()

MODEL = "claude-opus-4-5"

PROMPT = """You are extracting parts data from a photo of a printed automotive price list.

Rules:
- Return ONLY a valid JSON array. No markdown, no code fences, no explanation, no preamble.
- Each element represents one line item (one part).
- Some lines have a handwritten price crossing out the printed price. When that happens, use the handwritten price. If no handwriting, use the printed price.
- If a field is not visible or not applicable, use null.
- Prices must be numbers (no currency symbols).

Each object must have exactly these fields:
{
  "part_number": "string or null",
  "description": "string or null",
  "price": number or null,
  "make": "string or null",
  "model": "string or null",
  "year_range": "string or null"
}

Extract every line item visible in the image. Return the JSON array now."""


def _encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def _media_type(image_path: str) -> str:
    """Detect media type from file magic bytes, not extension."""
    with open(image_path, "rb") as f:
        header = f.read(12)
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    # Fall back to extension hint
    ext = Path(image_path).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")


def extract_parts_from_image(image_path: str, distributor: str, page: int = 1) -> list[dict]:
    """Send one image to Claude vision and return normalized part dicts."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    image_data = _encode_image(image_path)
    mime = _media_type(image_path)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": PROMPT,
                    },
                ],
            }
        ],
    )

    raw_text = response.content[0].text.strip()

    # Strip accidental markdown fences if the model adds them despite instructions
    raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
    raw_text = re.sub(r"\n?```$", "", raw_text)

    items = json.loads(raw_text)
    if not isinstance(items, list):
        raise ValueError(f"Expected JSON array, got {type(items).__name__}")

    parts = []
    now = datetime.now().isoformat()

    for item in items:
        if not isinstance(item, dict):
            continue

        description  = (item.get("description")  or "").strip()
        part_number  = (item.get("part_number")   or "").strip()
        make         = (item.get("make")           or "").strip()
        model        = (item.get("model")          or "").strip()
        year_range   = (item.get("year_range")     or "").strip()

        price = item.get("price")
        if price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None

        # Skip completely empty rows
        if not description and not part_number:
            continue

        combined = " ".join(filter(None, [part_number, description, make, model, year_range]))
        uid = hashlib.md5(f"{distributor}{combined}{page}".encode()).hexdigest()[:10]

        parts.append({
            "id":           uid,
            "distributor":  distributor,
            "page":         page,
            "raw":          combined,
            "description":  description,
            "part_number":  part_number,
            "price":        price,
            "make":         make,
            "model":        model,
            "year_range":   year_range,
            "category":     "",
            "updated_at":   now,
        })

    return parts


def process_image(image_path: str, distributor: str) -> list[dict]:
    print(f"\n🖼️  Processing: {image_path}")
    print(f"   Distributor: {distributor}")
    try:
        parts = extract_parts_from_image(image_path, distributor)
        print(f"   ✅ Extracted {len(parts)} parts")
        return parts
    except json.JSONDecodeError as e:
        print(f"   ❌ JSON parse error: {e}")
        return []
    except anthropic.APIError as e:
        print(f"   ❌ API error: {e}")
        return []
    except Exception as e:
        print(f"   ❌ Unexpected error: {e}")
        return []


def process_single(jpg_path: str, distributor: str) -> None:
    parts = process_image(jpg_path, distributor)
    if parts:
        merge_into_index(parts, distributor)
        print(f"💾 Merged {len(parts)} parts into index for '{distributor}'")
    else:
        print("⚠️  No parts extracted — index unchanged")


def process_folder(folder_path: str, distributor: str) -> None:
    folder = Path(folder_path)
    images = sorted(
        list(folder.glob("*.jpg")) +
        list(folder.glob("*.JPG")) +
        list(folder.glob("*.jpeg")) +
        list(folder.glob("*.JPEG"))
    )
    if not images:
        print(f"❌ No JPG images found in {folder_path}")
        return

    print(f"📂 Found {len(images)} image(s) in {folder_path}")

    all_parts = []
    for i, img in enumerate(images, 1):
        parts = process_image(str(img), distributor)
        all_parts.extend(parts)

    if all_parts:
        merge_into_index(all_parts, distributor)
        print(f"\n💾 Merged {len(all_parts)} total parts into index for '{distributor}'")
    else:
        print("\n⚠️  No parts extracted from any image — index unchanged")

    print(f"✅ Done — {len(images)} image(s) processed, {len(all_parts)} parts indexed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zeli Image Price List Extractor")
    parser.add_argument("--jpg",         help="Path to a single JPG image")
    parser.add_argument("--folder",      help="Folder containing JPG images to batch process")
    parser.add_argument("--distributor", required=True, help="Distributor name for all images")

    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set in environment or .env")
        raise SystemExit(1)

    if args.jpg:
        process_single(args.jpg, args.distributor)
    elif args.folder:
        process_folder(args.folder, args.distributor)
    else:
        parser.print_help()
