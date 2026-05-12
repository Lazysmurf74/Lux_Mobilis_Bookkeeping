"""
Parser Pack: NAZIV BANKE
Format:      Tip izvoda (PDF)
Version:     1.0.0

TEMPLATE – kopirajte ovaj file, promijenite BANK_KEY, BANK_TAG, DETECT_RE
i implementirajte parse(path). Sve ostalo radi automatski.
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

# ── Identifikacija (obavezno) ─────────────────────────────────────────────────
BANK_KEY  = 'Naziv Banke (tip izvoda)'          # prikazuje se u dropdown-u
BANK_TAG  = 'KRATKI_TAG'                         # koristi se u imenu XML datoteke
DETECT_RE = r'Jedinstveni tekst iz PDF headera'  # regex za auto-detekciju banke

# ── Parser ────────────────────────────────────────────────────────────────────
def parse(path: str) -> tuple[dict, list[dict]]:
    """
    Parsiraj PDF izvod i vrati (header, transakcije).

    header = {
        'iban':       str,   # HR... bez razmaka
        'bic':        str,   # SWIFTBIC
        'client':     str,   # ime klijenta
        'client_oib': str,   # OIB (opcionalno)
        'stmt_num':   str,   # broj izvoda (opcionalno)
        'stmt_date':  str,   # datum DD.MM.YYYY (opcionalno)
    }

    Svaka transakcija = {
        'seq':         int,   # redni broj
        'cp_iban':     str,   # IBAN druge strane
        'bank_ref':    str,   # bankovna referenca (poziv na broj)
        'val_date':    str,   # YYYY-MM-DD
        'exec_date':   str,   # YYYY-MM-DD
        'description': str,   # max 120 znakova
        'debit':       float, # isplata (0.0 ako nema)
        'credit':      float, # uplata  (0.0 ako nema)
    }
    """
    header = {}
    transactions = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text  = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)

            # TODO: implementirajte parsiranje

    return header, transactions
