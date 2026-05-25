"""
pack_loader.py – Loads parser packs dynamically from the parser_packs/ folder.

Usage in LazyCamt.py:
    from pack_loader import load_packs
    BANKS, BANK_TAGS = load_packs()
"""
import importlib.util
import json
import sys
from pathlib import Path


def _packs_dir() -> Path:
    """
    Resolve parser_packs/ correctly both when running as a plain script
    and when bundled with PyInstaller (sys._MEIPASS).
    User-updated packs live in APPDATA/LazyCamt/parser_packs/ and take
    priority over the bundled copies.
    """
    # 1. User-writable location (updated packs land here)
    import os
    appdata = os.environ.get("APPDATA") or Path.home()
    user_dir = Path(appdata) / "LuxMobilisHelper" / "parser_packs"

    # 2. Bundled / source location
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    bundled_dir = base / "parser_packs"

    # Prefer user dir if it exists and has a manifest
    if (user_dir / "manifest.json").exists():
        return user_dir
    return bundled_dir


def load_packs() -> tuple[dict, dict]:
    """
    Scan parser_packs/ for *.py files that expose BANK_KEY, BANK_TAG
    and a parse(path) function.

    Returns:
        BANKS     – { display_name: parse_fn | None }   (None = auto-detect)
        BANK_TAGS – { display_name: short_tag }
    """
    packs_dir = _packs_dir()

    # Ensure pack_utils is importable from this directory
    if str(packs_dir) not in sys.path:
        sys.path.insert(0, str(packs_dir))

    banks     = {"Automatski prepoznaj banku": None}
    bank_tags = {}

    manifest_path = packs_dir / "manifest.json"
    installed = {}
    if manifest_path.exists():
        try:
            installed = json.loads(manifest_path.read_text(encoding="utf-8")).get("packs", {})
        except Exception:
            pass

    # Load every .py that is listed in the manifest (or all .py if no manifest)
    candidates = []
    if installed:
        for info in installed.values():
            f = packs_dir / info.get("file", "")
            if f.suffix == ".py" and f.exists():
                candidates.append(f)
    else:
        candidates = [f for f in packs_dir.glob("*.py") if f.name != "pack_utils.py"]

    for py_file in sorted(candidates):
        try:
            spec   = importlib.util.spec_from_file_location(py_file.stem, py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            key = getattr(module, "BANK_KEY",  None)
            tag = getattr(module, "BANK_TAG",  None)
            fn  = getattr(module, "parse",     None)

            if key and tag and callable(fn):
                banks[key]     = fn
                bank_tags[key] = tag
        except Exception as e:
            print(f"[pack_loader] Skipping {py_file.name}: {e}", file=sys.stderr)

    return banks, bank_tags


def detect_parser_dynamic(path: str, banks: dict):
    """
    Auto-detect bank from PDF content using DETECT_RE from each pack.
    Returns (bank_key, parse_fn) or raises ValueError.
    """
    import re
    import pdfplumber

    packs_dir = _packs_dir()
    if str(packs_dir) not in sys.path:
        sys.path.insert(0, str(packs_dir))

    # Build a map: bank_key → DETECT_RE, parse_fn
    detect_map = {}
    manifest_path = packs_dir / "manifest.json"
    installed = {}
    if manifest_path.exists():
        try:
            installed = json.loads(manifest_path.read_text(encoding="utf-8")).get("packs", {})
        except Exception:
            pass

    candidates = []
    if installed:
        for info in installed.values():
            f = packs_dir / info.get("file", "")
            if f.suffix == ".py" and f.exists():
                candidates.append(f)
    else:
        candidates = [f for f in packs_dir.glob("*.py") if f.name != "pack_utils.py"]

    for py_file in sorted(candidates):
        try:
            spec   = importlib.util.spec_from_file_location(py_file.stem, py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            key     = getattr(module, "BANK_KEY",   None)
            pattern = getattr(module, "DETECT_RE",  None)
            fn      = getattr(module, "parse",      None)
            if key and pattern and callable(fn):
                detect_map[key] = (pattern, fn)
        except Exception:
            pass

    with pdfplumber.open(path) as pdf:
        text = ""
        for page in pdf.pages[:2]:
            text += (page.extract_text() or "")

    for bank_key, (pattern, fn) in detect_map.items():
        if re.search(pattern, text, re.IGNORECASE):
            return bank_key, fn

    from pathlib import Path as _Path
    raise ValueError(f"Ne mogu prepoznati banku iz PDF-a: {_Path(path).name}")
