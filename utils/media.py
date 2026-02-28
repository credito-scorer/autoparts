import os
import requests
from dotenv import load_dotenv

load_dotenv()

PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1016895944841092")
GRAPH_API_BASE  = "https://graph.facebook.com/v17.0"


def download_meta_media(media_id: str) -> tuple[bytes, str]:
    """Download media from Meta Cloud API.

    Returns (image_bytes, mime_type).
    Raises requests.HTTPError on failure.
    """
    token   = os.getenv("META_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: resolve the download URL from the media ID
    meta_resp = requests.get(f"{GRAPH_API_BASE}/{media_id}", headers=headers, timeout=15)
    meta_resp.raise_for_status()
    meta      = meta_resp.json()
    url       = meta["url"]
    mime_type = meta.get("mime_type", "image/jpeg")

    # Step 2: download the actual bytes
    data_resp = requests.get(url, headers=headers, timeout=30)
    data_resp.raise_for_status()

    return data_resp.content, mime_type


def upload_meta_media(image_bytes: bytes, mime_type: str) -> str:
    """Upload media bytes to Meta Cloud API and return the new media ID.

    Media IDs are not reusable across recipients, so always re-upload
    before forwarding to a different phone number.
    Raises requests.HTTPError on failure.
    """
    token   = os.getenv("META_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}

    ext_map = {
        "image/jpeg": "jpg",
        "image/png":  "png",
        "image/gif":  "gif",
        "image/webp": "webp",
    }
    ext = ext_map.get(mime_type, "jpg")

    files = {
        "file":              (f"media.{ext}", image_bytes, mime_type),
        "messaging_product": (None, "whatsapp"),
        "type":              (None, mime_type),
    }

    url  = f"{GRAPH_API_BASE}/{PHONE_NUMBER_ID}/media"
    resp = requests.post(url, headers=headers, files=files, timeout=30)
    resp.raise_for_status()

    return resp.json()["id"]
