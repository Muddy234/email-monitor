"""
Build script — packages the Chrome extension into a .zip for Web Store submission.

Usage:
    python build_extension.py

Output:
    dist/clarion-ai-<version>.zip
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
EXT_DIR = ROOT / "extension"
DIST_DIR = ROOT / "dist"

# Files to include in the zip (relative to extension/)
INCLUDE = [
    "manifest.json",
    "background.js",
    "content.js",
    "popup.html",
    "popup.js",
    "supabase-config.js",
    "supabase-auth.js",
    "supabase-rest.js",
    "supabase-realtime.js",
    "icons/icon16.png",
    "icons/icon48.png",
    "icons/icon128.png",
]


def build():
    # Read version from manifest
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version", "0.0.0")

    DIST_DIR.mkdir(exist_ok=True)
    zip_name = f"clarion-ai-{version}.zip"
    zip_path = DIST_DIR / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE:
            src = EXT_DIR / rel
            if not src.exists():
                print(f"  WARNING: missing {rel}")
                continue
            zf.write(src, rel)
            print(f"  + {rel}")

    print(f"\nBuilt {zip_path} ({zip_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
