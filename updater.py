"""
updater.py – Checks for parser pack updates and installs them after user confirmation.

Flow:
  1. Fetch remote manifest.json
  2. Compare versions with local manifest
  3. Show tkinter dialog listing available updates (user can tick/untick each)
  4. Download + sha256-verify selected packs
  5. Write to user's APPDATA/LazyCamt/parser_packs/
  6. Return list of installed pack ids so caller can reload
"""
import hashlib
import json
import os
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Callable

# ── Paths ──────────────────────────────────────────────────────────────────────

REMOTE_MANIFEST_URL = "https://raw.githubusercontent.com/Lazysmurf74/Lux_Mobilis_Bookkeeping/main/packs/manifest.json"
#   The remote manifest.json must live at that URL (GitHub raw, CDN, your own server…)

def _user_packs_dir() -> Path:
    appdata = os.environ.get("APPDATA") or Path.home()
    d = Path(appdata) / "LuxMobilisHelper" / "parser_packs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _bundled_packs_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "parser_packs"

def _local_manifest() -> dict:
    """Read manifest from user dir first, then bundled."""
    for d in (_user_packs_dir(), _bundled_packs_dir()):
        p = d / "manifest.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {"packs": {}}

def _save_local_manifest(data: dict):
    p = _user_packs_dir() / "manifest.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ── Remote check ───────────────────────────────────────────────────────────────

def check_for_updates(timeout: int = 8) -> list[dict]:
    """
    Returns a list of packs that have a newer version available remotely.
    Each item: { id, bank_key, version, url, sha256, changelog }
    Returns [] on network error (silent fail – app still works offline).
    """
    try:
        import urllib.request
        with urllib.request.urlopen(REMOTE_MANIFEST_URL, timeout=timeout) as r:
            remote = json.loads(r.read().decode())
    except Exception as e:
        print(f"[updater] Cannot reach update server: {e}", file=sys.stderr)
        return []

    local = _local_manifest()
    local_packs = local.get("packs", {})
    updates = []
    for pack_id, info in remote.get("packs", {}).items():
        local_ver = local_packs.get(pack_id, {}).get("version", "0.0.0")
        if _version_gt(info["version"], local_ver):
            updates.append({
                "id":        pack_id,
                "bank_key":  info.get("bank_key", pack_id),
                "version":   info["version"],
                "url":       info["url"],
                "sha256":    info.get("sha256", ""),
                "changelog": info.get("changelog", ""),
                "file":      info.get("file", f"{pack_id}.py"),
            })
    return updates

def _version_gt(a: str, b: str) -> bool:
    """True if version string a > b (simple semver comparison)."""
    def parts(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except Exception:
            return (0, 0, 0)
    return parts(a) > parts(b)

# ── Download & verify ──────────────────────────────────────────────────────────

def _download_pack(pack: dict) -> bytes:
    import urllib.request
    with urllib.request.urlopen(pack["url"], timeout=30) as r:
        data = r.read()
    if pack["sha256"]:
        got = hashlib.sha256(data).hexdigest()
        if got != pack["sha256"]:
            raise ValueError(f"Checksum mismatch for {pack['id']}: expected {pack['sha256']}, got {got}")
    return data

def _install_pack(pack: dict, data: bytes):
    dest_dir = _user_packs_dir()

    # Also copy pack_utils.py from bundled dir if not already in user dir
    utils_src = _bundled_packs_dir() / "pack_utils.py"
    utils_dst = dest_dir / "pack_utils.py"
    if utils_src.exists() and not utils_dst.exists():
        shutil.copy2(utils_src, utils_dst)

    dest_file = dest_dir / pack["file"]
    dest_file.write_bytes(data)

    # Update local manifest
    local = _local_manifest()
    local.setdefault("packs", {})[pack["id"]] = {
        "version": pack["version"],
        "file":    pack["file"],
    }
    _save_local_manifest(local)

# ── UI dialog ─────────────────────────────────────────────────────────────────

class UpdateDialog(tk.Toplevel):
    """
    Modal dialog that lists available updates.
    User can tick/untick each pack, then click Install or Skip.
    """
    def __init__(self, parent: tk.Tk, updates: list[dict], on_done: Callable):
        super().__init__(parent)
        self.title("Dostupni update-ovi parsera")
        self.resizable(False, False)
        self.configure(bg="#f5f5f3")
        self.grab_set()          # modal
        self._updates  = updates
        self._on_done  = on_done
        self._vars     = {}
        self._build(updates)
        self.protocol("WM_DELETE_WINDOW", self._skip)
        # Centre over parent
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h   = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw-w)//2}+{py + (ph-h)//2}")

    def _build(self, updates):
        outer = tk.Frame(self, bg="#f5f5f3", padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="🔄  Ažuriranja parsera",
                 font=("Segoe UI", 13, "bold"), bg="#f5f5f3", fg="#1a1a1a"
                 ).pack(anchor="w")
        tk.Label(outer,
                 text="Dostupne su nove verzije parsera. Odaberite koje želite instalirati:",
                 font=("Segoe UI", 9), bg="#f5f5f3", fg="#555"
                 ).pack(anchor="w", pady=(2, 12))

        for pack in updates:
            row = tk.Frame(outer, bg="#f0f0ee", bd=0, pady=6, padx=10)
            row.pack(fill="x", pady=(0, 6))
            var = tk.BooleanVar(value=True)
            self._vars[pack["id"]] = var

            tk.Checkbutton(
                row, variable=var, bg="#f0f0ee", activebackground="#f0f0ee"
            ).pack(side="left")

            info = tk.Frame(row, bg="#f0f0ee")
            info.pack(side="left", fill="x", expand=True)

            tk.Label(info,
                     text=f"{pack['bank_key']}   →   v{pack['version']}",
                     font=("Segoe UI", 9, "bold"), bg="#f0f0ee", fg="#1a1a1a", anchor="w"
                     ).pack(anchor="w")
            if pack.get("changelog"):
                tk.Label(info,
                         text=pack["changelog"],
                         font=("Segoe UI", 8), bg="#f0f0ee", fg="#666", anchor="w",
                         wraplength=380, justify="left"
                         ).pack(anchor="w")

        btn_row = tk.Frame(outer, bg="#f5f5f3")
        btn_row.pack(fill="x", pady=(14, 0))

        tk.Button(btn_row, text="Preskoči",
                  font=("Segoe UI", 9), bg="#e8e8e6", relief="flat",
                  padx=12, pady=6, cursor="hand2",
                  command=self._skip
                  ).pack(side="right", padx=(8, 0))

        self._install_btn = tk.Button(btn_row, text="Instaliraj odabrano",
                  font=("Segoe UI", 9, "bold"), bg="#1a1a1a", fg="#fff",
                  relief="flat", padx=12, pady=6, cursor="hand2",
                  command=self._install
                  ).pack(side="right")

        self._status = tk.Label(outer, text="", font=("Segoe UI", 8),
                                bg="#f5f5f3", fg="#555")
        self._status.pack(anchor="w", pady=(8, 0))

    def _skip(self):
        self._on_done([])
        self.destroy()

    def _install(self):
        chosen = [p for p in self._updates if self._vars[p["id"]].get()]
        if not chosen:
            self._skip(); return

        self._status.config(text="Preuzimanje…")
        self.update_idletasks()

        def worker():
            installed = []
            errors    = []
            for pack in chosen:
                try:
                    data = _download_pack(pack)
                    _install_pack(pack, data)
                    installed.append(pack["id"])
                except Exception as e:
                    errors.append(f"{pack['id']}: {e}")
            self.after(0, lambda: self._finish(installed, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, installed: list[str], errors: list[str]):
        if errors:
            messagebox.showerror(
                "Greška pri instalaciji",
                "\n".join(errors),
                parent=self
            )
        if installed:
            self._status.config(
                text=f"✅  Instalirano: {', '.join(installed)}"
            )
            self.after(1200, lambda: (self._on_done(installed), self.destroy()))
        else:
            self._on_done([])
            self.destroy()

# ── Public entry point ─────────────────────────────────────────────────────────

def run_update_check(parent: tk.Tk, on_done: Callable[[list[str]], None]):
    """
    Call this at app startup (in a background thread).
    If updates are found, shows the dialog on the main thread.
    on_done(installed_ids) is called after the dialog closes.
    """
    def worker():
        updates = check_for_updates()
        if updates:
            parent.after(0, lambda: UpdateDialog(parent, updates, on_done))
        else:
            on_done([])

    threading.Thread(target=worker, daemon=True).start()
