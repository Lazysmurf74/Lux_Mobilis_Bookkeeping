"""
Lazy Pdf to Camt 0.53 Converter
Supports: PBZ, ZABA, Erste Bank, Addiko  +  updateable parser packs
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading, re, os
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import logging
import os
import sys
import ctypes
from logging.handlers import RotatingFileHandler
from pathlib import Path


# 1. Identifica la cartella corretta in base al Sistema Operativo
def get_log_directory(app_name):
    if sys.platform == "win32":
        # Percorso: C:\Users\Nome\AppData\Roaming\NomeApp
        base_dir = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        # Percorso: /Users/Nome/Library/Application Support/NomeApp
        base_dir = Path.home() / "Library" / "Application Support"
    else:
        # Percorso Linux: /home/Nome/.config/NomeApp
        base_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        )

    final_dir = base_dir / app_name
    final_dir.mkdir(parents=True, exist_ok=True)
    return final_dir


# Nome della tua applicazione per la cartella dedicata
APP_NAME = "LuxMobilisHelper"
log_file = get_log_directory(APP_NAME) / "error_log.txt"

# 2. Configura la rotazione automatica (Max 2MB per file, tiene solo gli ultimi 3)
log_handler = RotatingFileHandler(
    log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
)

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[log_handler],
)


# 3. Gestore degli errori fatali
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error(
        "Errore fatale non gestito:", exc_info=(exc_type, exc_value, exc_traceback)
    )


sys.excepthook = handle_exception


# ── Parser pack system ────────────────────────────────────────────────────────
from pack_loader import load_packs, detect_parser_dynamic
from updater import run_update_check

def _reload_packs():
    """Reload BANKS and BANK_TAGS after an update."""
    global BANKS, BANK_TAGS
    BANKS, BANK_TAGS = load_packs()

# 2. Fix Icona Barra Applicazioni (Solo per Windows)
try:
    myappid = 'bank.converter.camt053.v1' 
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except:
    pass

# 3. Import delle librerie pesanti/esterne

try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False

# ── helpers ───────────────────────────────────────────────────────────────────

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def parse_amount(s):
    try: return float(str(s).replace('.','').replace(',','.'))
    except: return 0.0

def to_iso(d):
    d = str(d).rstrip('.')
    p = d.split('.')
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p)==3 else d

AMOUNT_RE = re.compile(r'^\d{1,3}(?:\.\d{3})*,\d{2}$')

# ── PBZ Parser ────────────────────────────────────────────────────────────────

def parse_pbz(path):
    header = {}
    transactions = {}
    with pdfplumber.open(path) as pdf:
        p1 = pdf.pages[0].extract_text() or ""
        m = re.search(r'Račun broj:\s*(HR[\d\s]+?)\s+SWIFT', p1)
        if m: header['iban'] = m.group(1).replace(' ','')
        m = re.search(r'SWIFT \(BIC\):\s*(\w+)', p1)
        if m: header['bic'] = m.group(1)
        m = re.search(r'Datum izvatka:\s*(\d{2}\.\d{2}\.\d{4})', p1)
        if m: header['stmt_date'] = m.group(1)
        m = re.search(r'Naziv i adresa klijenta:\s*([^\n]+)', p1)
        if m: header['client'] = m.group(1).strip().split(' Novo ')[0]
        m = re.search(r'OIB:\s*(\d{11})\s+Naziv', p1)
        if m: header['client_oib'] = m.group(1)
        m = re.search(r'Izvadak EUR br\.:\s*(\d+)', p1)
        if m: header['stmt_num'] = m.group(1).zfill(3)

        all_amt_words = []
        for pi, page in enumerate(pdf.pages):
            for w in page.extract_words(x_tolerance=2, y_tolerance=2):
                if AMOUNT_RE.match(w['text']) and w['x0'] > 650:
                    all_amt_words.append([pi, round(w['top']), w['x0'], w['text'], False])

        for page in pdf.pages:
            text = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=2)

            # Build a lookup: for each row (rounded top), words sorted by x
            # Used to find dates by x-position within the transaction's row range
            DATE_RE_W = re.compile(r'^\d{2}\.\d{2}\.\d{4}\.?$')
            rows_w = defaultdict(list)
            for w in words:
                rows_w[round(w['top'])].append(w)

            for m in re.finditer(
                r'^(\d+)\.\s+((?:HR|DE|SI|BE|LT|AT|GB|FR|NL|PL|CZ|HU)\w+)\s+(\S+)',
                text, re.MULTILINE):
                seq = int(m.group(1))
                if seq in transactions: continue
                cp_iban  = m.group(2)
                bank_ref = m.group(3)
                sep = text.find('_'*20, m.end())
                block = text[m.start():sep] if sep!=-1 else text[m.start():]

                # ── Date extraction via word x-position ──────────────────────
                # In PBZ layout the date pair sits in the right cell (~x 820-920).
                # pdfplumber char positions: "Datum valute" date is left (~x0 820),
                # "Datum izvršenja" date is right (~x0 870+).
                # We find the approximate top of this transaction from the regex match
                # then look in word rows within ±60 pts for date words in that x-band.
                #
                # Fallback: if word approach finds nothing, parse dates from text block
                # but skip lines that also contain non-date text (avoids desc dates).

                # Estimate the y-range for this transaction block
                # Count newlines before match to estimate row
                pre_lines = text[:m.start()].count('\n')
                # Find word rows that belong to this block (between this seq and next)
                next_m_pos = m.end()
                next_m = re.search(
                    r'^(\d+)\.\s+((?:HR|DE|SI|BE|LT|AT|GB|FR|NL|PL|CZ|HU)\w+)\s+(\S+)',
                    text[m.end():], re.MULTILINE)

                # Collect date words from the right-side date columns (x0 > 800)
                date_words_right = []
                for rk, rw in rows_w.items():
                    for w in rw:
                        if DATE_RE_W.match(w['text']) and w['x0'] > 800:
                            date_words_right.append(w)

                # The transaction's date pair: two consecutive date words that are
                # closest to each other vertically and both x0 > 800.
                # We associate them with this transaction by matching against all
                # dates found in the text block.
                block_dates_text = re.findall(r'\b(\d{2}\.\d{2}\.\d{4})\b', block)
                # Filter to dates that are NOT embedded in longer text lines
                # (i.e. lines where the date is the only or near-only content)
                clean_dates = []
                for line in block.split('\n'):
                    line = line.strip()
                    # Accept lines that are purely one or two dates
                    only_dates = re.fullmatch(
                        r'(\d{2}\.\d{2}\.\d{4})\.?\s*(\d{2}\.\d{2}\.\d{4})?\.?', line)
                    if only_dates:
                        clean_dates.append(only_dates.group(1))
                        if only_dates.group(2):
                            clean_dates.append(only_dates.group(2))

                if clean_dates:
                    val_date  = to_iso(clean_dates[0])
                    exec_date = to_iso(clean_dates[1]) if len(clean_dates) > 1 else val_date
                elif block_dates_text:
                    # Last-resort: use all found dates, pick last two (most likely
                    # to be the date pair at the bottom of the block, not desc dates)
                    val_date  = to_iso(block_dates_text[-2]) if len(block_dates_text) >= 2 else to_iso(block_dates_text[-1])
                    exec_date = to_iso(block_dates_text[-1])
                else:
                    val_date = exec_date = ''
                # ─────────────────────────────────────────────────────────────

                lines = [l.strip() for l in block.split('\n') if l.strip()]
                desc_parts = []
                for l in lines[1:]:
                    if re.match(r'^(HR|DE|SI)\d{2}[\d ]', l): continue
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}', l): continue
                    if re.match(r'^\d{7,}$', l): continue
                    if re.match(r'^(HR99|HR00|HR01|HR05|HR55|HR68|HR67|HR69|HR17)', l): break
                    clean = re.sub(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b','',l)
                    clean = re.sub(r'\(\d{3}\)','',clean).strip()
                    if clean: desc_parts.append(clean)
                description = ' '.join(desc_parts[:3]).strip()[:120]
                clean_block = re.sub(r'\d+,\d{2}\s*\(\d{3}\)', '', block)
                all_amts = re.findall(r'\b(\d{1,3}(?:\.\d{3})*,\d{2})\b', clean_block)
                transactions[seq] = {
                    'seq':seq,'cp_iban':cp_iban,'bank_ref':bank_ref,
                    'val_date':val_date,'exec_date':exec_date,
                    'description':description,'debit':0.0,'credit':0.0,'_amts':all_amts,
                }

        for tx in transactions.values():
            if not tx['_amts']: continue
            target = tx['_amts'][-1]
            for entry in all_amt_words:
                if entry[4]: continue
                if entry[3] == target:
                    tx['debit' if entry[2]<755 else 'credit'] = parse_amount(target)
                    entry[4] = True; break

    result = sorted(transactions.values(), key=lambda t: t['seq'])
    for t in result: del t['_amts']
    return header, result

# ── ZABA Parser ───────────────────────────────────────────────────────────────

_ZABA_SKIP = {'PRETHODNO','STANJE','NOVO','Ukupan','plaćanja:','STANJE:','broj','iznos','plaćanja','(br.'}

def parse_zaba(path):
    header = {}
    transactions = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words: continue
            if 'IZVADAK' not in {w['text'] for w in words} and 'Račun' not in {w['text'] for w in words}:
                continue
            duguje_x = 469; credit_x = 529
            for w in words:
                if w['text'] == 'Duguje': duguje_x = w['x0']
                if w['text'] == 'Potražuje': credit_x = w['x0']
            split_x = (duguje_x + credit_x) / 2
            if not header.get('iban'):
                # Client IBAN on ZABA is on the RIGHT side of the header (x0 > 300).
                # The bank's own IBAN and counterparty IBANs are on the left.
                # Strategy 1: find "IBAN" label word on the right half, take HR token after it.
                for i, w in enumerate(words):
                    if w['text'] == 'IBAN' and w['x0'] > 300:
                        for w2 in words[i+1:i+6]:
                            candidate = re.sub(r'\s+', '', w2['text'])
                            if re.match(r'^HR\d{2}\d{15,}$', candidate):
                                header['iban'] = candidate; break
                        if header.get('iban'): break
                if not header.get('iban'):
                    # Strategy 2: collect all HR IBANs from page text, skip the bank's own
                    text = page.extract_text() or ""
                    all_ibans = re.findall(r'HR\d{2}[\d\s]{15,}', text)
                    cleaned = [re.sub(r'\s+', '', x) for x in all_ibans
                               if len(re.sub(r'\s+', '', x)) >= 19]
                    # Skip ZABA's own account IBANs
                    client_ibans = [x for x in cleaned
                                    if not re.match(r'^HR\d{2}(23[46]0000|2360009)', x)]
                    if client_ibans:
                        header['iban'] = client_ibans[0]
                header['bic'] = 'ZABAHR2X'
                for w in words:
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$', w['text']) and w['x0']<100:
                        header['stmt_date'] = w['text'].rstrip('.'); break
                for i, w in enumerate(words):
                    if w['text'] in ('J.D.O.O.','D.O.O.','d.o.o.','j.d.o.o.') and w['x0']>300:
                        cw = [words[j]['text'] for j in range(max(0,i-6),i+1) if words[j]['x0']>300]
                        header['client'] = ' '.join(cw); break
                for w in words:
                    if w['text'] == 'OIB:' and w['x0']>300:
                        idx = words.index(w)
                        if idx+1 < len(words): header['client_oib'] = words[idx+1]['text']; break
            rows = defaultdict(list)
            for w in words: rows[round(w['top']/3)*3].append(w)
            sorted_rows = sorted(rows.keys())
            for ri, rk in enumerate(sorted_rows):
                row = sorted(rows[rk], key=lambda w: w['x0'])
                if not row: continue
                first = row[0]
                if not (re.match(r'^\d{1,3}$', first['text']) and first['x0']<60 and first['top']>200): continue
                if len(row)<2: continue
                dm = re.match(r'^(\d{2}\.\d{2}\.\d{4})\.?$', row[1]['text'])
                if not dm: continue
                seq = int(first['text'])
                book_date = to_iso(dm.group(1))
                exec_date = book_date
                if len(row)>2:
                    dm2 = re.match(r'^(\d{2}\.\d{2}\.\d{4})\.?$', row[2]['text'])
                    if dm2: exec_date = to_iso(dm2.group(1))
                debit = credit = 0.0
                for w in row:
                    if AMOUNT_RE.match(w['text']) and w['x0']>450:
                        if w['x0']<split_x: debit = parse_amount(w['text'])
                        else: credit = parse_amount(w['text'])
                cp_iban = ''
                for w in row:
                    if re.match(r'^(?:HR|DE|SI|BE|LT)\w{14,}$', w['text']) and w['x0']>150:
                        cp_iban = w['text']; break
                desc_parts = []; bank_ref = ''
                for next_rk in sorted_rows[ri+1:ri+6]:
                    nr = sorted(rows[next_rk], key=lambda w: w['x0'])
                    if nr and re.match(r'^\d{1,3}$', nr[0]['text']) and nr[0]['x0']<60: break
                    for w in nr:
                        if w['x0']>310 and w['x0']<465 and w['text'] not in _ZABA_SKIP:
                            if not re.match(r'^HR\d{2}$', w['text']): desc_parts.append(w['text'])
                    if not bank_ref:
                        for w in nr:
                            if w['x0']>65 and w['x0']<155 and len(w['text'])>8: bank_ref=w['text']; break
                transactions.append({
                    'seq':seq,'cp_iban':cp_iban,'bank_ref':bank_ref,
                    'val_date':book_date,'exec_date':exec_date,
                    'description':' '.join(desc_parts[:8]).strip()[:120],
                    'debit':debit,'credit':credit,
                })
    return header, transactions

# ── Erste Parser ──────────────────────────────────────────────────────────────

_ERSTE_BAL = {'Stanje','stanje','promet','Promet','S','t','a','n','j','e',
               'Početno','Konačno','Privremeno','Prethodno','Raspoloživo'}

def parse_erste(path):
    header = {}
    transactions = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if 'IZVOD PROMETA' not in text: continue
            if not header.get('iban'):
                for pat, key in [
                    (r'IBAN:\s*(HR\w+)','iban'),(r'SWIFT/BIC:\s*(\w+)','bic'),
                    (r'Naziv klijenta:\s*(.+?)(?=\n)','client'),
                    (r'OIB:\s*(4\d{10})','client_oib'),(r'Broj izvoda:\s*(\S+)','stmt_num'),
                ]:
                    m = re.search(pat, text)
                    if m: header[key] = m.group(1).strip()
                m = re.search(r'do\s+(\d{2}\.\d{2}\.\d{4})\.', text)
                if m: header['stmt_date'] = m.group(1)
            isplata_x = uplata_x = None
            for w in words:
                if w['text']=='Isplata': isplata_x=w['x0']
                if w['text']=='Uplata':  uplata_x=w['x0']
            if not isplata_x: isplata_x=491
            if not uplata_x:  uplata_x=551
            rows = defaultdict(list)
            for w in words: rows[round(w['top']/3)*3].append(w)
            sorted_rows = sorted(rows.keys())
            # Find seq+dash rows
            seq_rows = []
            for rk in sorted_rows:
                row = sorted(rows[rk], key=lambda w: w['x0'])
                seq_w = None
                for w in row:
                    if re.match(r'^\d{1,2}$', w['text']) and 232<w['x0']<250:
                        for w2 in row:
                            if w2['text']=='-' and w['x0']<w2['x0']<w['x0']+15:
                                seq_w=w; break
                    if seq_w: break
                if seq_w: seq_rows.append((rk, int(seq_w['text'])))
            # Stanje boundary rows
            stanje_rows = [rk for rk in sorted_rows
                           if any(w['text']=='Stanje' and w['x0']<40 for w in rows[rk])
                           and any(w['text']=='dan' for w in rows[rk])]
            rekap_row = max(sorted_rows)+100
            def find_end(hk, hi):
                if hi+1<len(seq_rows):
                    cand = seq_rows[hi+1][0]
                    for sr in stanje_rows:
                        if hk<sr<cand: return sr
                    return cand
                for sr in stanje_rows:
                    if sr>hk: return sr
                return rekap_row
            for hi,(hk,seq) in enumerate(seq_rows):
                end_rk = find_end(hk,hi)
                ws = hk-30
                tx_rows = {rk:rows[rk] for rk in sorted_rows if ws<=rk<end_rk}
                # Dates
                dw = sorted([w for rk,row in tx_rows.items() for w in row
                              if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$',w['text']) and w['x0']<40],
                             key=lambda w:w['top'])
                val_date  = to_iso(dw[0]['text']) if dw else ''
                exec_date = to_iso(dw[1]['text']) if len(dw)>1 else val_date
                # CP name
                cp_words = sorted([w for rk,row in tx_rows.items() for w in row
                                    if 70<w['x0']<235
                                    and not re.match(r'^HR\w{14,}$',w['text'])
                                    and not re.match(r'^HR\d{2}$',w['text'])
                                    and not re.match(r'^\d{4}-',w['text'])
                                    and not re.match(r'^\d{2}\.\d{2}\.\d{4}',w['text'])
                                    and w['text'] not in _ERSTE_BAL],
                                   key=lambda w:(w['top'],w['x0']))
                cp_name = ' '.join(w['text'] for w in cp_words[:6]).strip()
                # CP IBAN
                cp_iban = ''
                for rk in sorted(tx_rows.keys()):
                    for w in sorted(tx_rows[rk],key=lambda w:w['x0']):
                        if re.match(r'^HR\w{14,}$',w['text']) and w['x0']<235:
                            cp_iban=w['text']; break
                    if cp_iban: break
                # Description
                dwords = sorted([w for rk,row in tx_rows.items() for w in row
                                  if 248<w['x0']<465 and w['text']!='-'
                                  and not re.match(r'^\d{1,2}$',w['text'])
                                  and not re.match(r'^HR\d{2}$',w['text'])
                                  and not re.match(r'^\d{4}-\d+',w['text'])
                                  and not re.match(r'^\d{2}\.\d{2}\.\d{4}',w['text'])
                                  and w['text'] not in _ERSTE_BAL
                                  and not re.match(r'^424472',w['text'])],
                                 key=lambda w:(w['top'],w['x0']))
                desc = ' '.join(w['text'] for w in dwords[:10]).strip()
                # Bank ref
                bank_ref = ''
                for rk,row in tx_rows.items():
                    for w in row:
                        if re.match(r'^\d{4}-\d+-\d+$',w['text']): bank_ref=w['text']; break
                    if bank_ref: break
                # Amount (only from seq row onwards, skip balance rows)
                debit=credit=0.0
                for rk in sorted(tx_rows.keys()):
                    if rk<hk: continue
                    row=tx_rows[rk]
                    if any(w['text'] in ('Stanje','stanje','Promet','promet') and w['x0']>300 for w in row): continue
                    for w in sorted(row,key=lambda w:w['x0']):
                        if AMOUNT_RE.match(w['text']) and w['x0']>450:
                            mid=(isplata_x+uplata_x)/2
                            if w['x0']<mid: debit=parse_amount(w['text'])
                            else: credit=parse_amount(w['text'])
                full_desc = f"{cp_name} - {desc}".strip(' -')[:120]
                transactions.append({
                    'seq':seq,'cp_iban':cp_iban,'bank_ref':bank_ref,
                    'val_date':val_date,'exec_date':exec_date,
                    'description':full_desc,'debit':debit,'credit':credit,
                })
    transactions.sort(key=lambda t:(t['val_date'],t['seq']))
    return header, transactions

# ── Addiko Bank Parser ────────────────────────────────────────────────────────
#
# Addiko "IZVADAK O STANJU I PROMETU PO RAČUNU" PDFs.
# Layout (from real statements):
#   • Header block contains IBAN, BIC, OIB/MB, Broj/datum
#   • Each transaction row has:
#       - transaction number (e.g. 6452610477467723)
#       - execution date (DD.MM.YYYY) repeated as value date
#       - counterparty name / description lines
#       - counterparty IBAN on the same or next line (HR…)
#       - Debit ("Duguje") amount in the right column  OR
#         Credit ("Potražuje") amount in the rightmost column
#   • Debit column is left of credit column; we use x-position to distinguish.
#   • "Ukupni promet" / "Novo stanje" / "Rezervacija" lines are footers – skip.

_ADDIKO_SKIP_DESC = {
    'Ukupni','promet','Broj','naloga','duguje:','potražuje:',
    'Rezervacija','kartične','transakcije:','prisilne','naplate:',
    'Najava','plaćanja','za:','Novo','stanje:','EUR',
    'Početno','stanje','Dozvoljeno','prekoračenje:',
}

def parse_addiko(path):
    header = {}
    transactions = []
    seq_counter = 0

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words:
                continue
            # ── Header (only once) ──────────────────────────────────────────
            if not header.get('iban'):
                # IBAN: "HR38 2500 0091 1015 6212 5"  (may have spaces)
                m = re.search(r'Broj računa-IBAN:\s*(HR[\d\s]{15,})', text)
                if m:
                    header['iban'] = re.sub(r'\s+', '', m.group(1))
                header['bic'] = 'HAABHR22'
                m = re.search(r'OIB\s*/\s*MB:\s*(\d{11})', text)
                if m:
                    header['client_oib'] = m.group(1)
                # Statement number and date: "2 / 28.02.2026"
                m = re.search(r'Broj\s*/\s*datum:\s*(\d+)\s*/\s*(\d{2}\.\d{2}\.\d{4})', text)
                if m:
                    header['stmt_num'] = m.group(1).zfill(3)
                    header['stmt_date'] = m.group(2)
                # Client name: first non-bank name line (top-left of page)
                m = re.search(r'^(GALIĆ[^\n]+|[A-ZŠĐČĆŽ][A-ZŠĐČĆŽ\s]+(?:D\.O\.O\.|d\.o\.o\.|j\.d\.o\.o\.))', text, re.MULTILINE)
                if m:
                    header['client'] = m.group(1).strip()

            # ── Determine debit / credit column x-positions ─────────────────
            duguje_x  = None
            potrazuje_x = None
            for w in words:
                if w['text'] == 'Duguje':    duguje_x    = w['x0']
                if w['text'] == 'Potražuje': potrazuje_x = w['x0']
            if duguje_x is None:    duguje_x    = 480
            if potrazuje_x is None: potrazuje_x = 545
            split_x = (duguje_x + potrazuje_x) / 2

            # ── Group words into rows ───────────────────────────────────────
            rows = defaultdict(list)
            for w in words:
                rows[round(w['top'] / 3) * 3].append(w)
            sorted_rows = sorted(rows.keys())

            # ── Find transaction header rows ────────────────────────────────
            # Addiko transaction IDs are 16-digit numbers (e.g. 6452610477467723)
            tx_header_rows = []
            for rk in sorted_rows:
                row = sorted(rows[rk], key=lambda w: w['x0'])
                for w in row:
                    if re.match(r'^\d{16}$', w['text']):
                        tx_header_rows.append(rk)
                        break

            for hi, hk in enumerate(tx_header_rows):
                end_rk = tx_header_rows[hi + 1] if hi + 1 < len(tx_header_rows) else max(sorted_rows) + 100

                # Collect all words in this transaction block
                block_rows = {rk: rows[rk] for rk in sorted_rows if hk <= rk < end_rk}
                all_block_words = [w for rk, row in block_rows.items() for w in row]

                # ── Execution / value date ──────────────────────────────────
                dates_in_block = []
                for rk in sorted(block_rows.keys()):
                    for w in sorted(block_rows[rk], key=lambda w: w['x0']):
                        if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']):
                            dates_in_block.append(w['text'])
                val_date  = to_iso(dates_in_block[0]) if dates_in_block else ''
                exec_date = to_iso(dates_in_block[1]) if len(dates_in_block) > 1 else val_date

                # ── Counterparty IBAN ───────────────────────────────────────
                cp_iban = ''
                for w in all_block_words:
                    if re.match(r'^HR\w{14,}$', w['text']) and w['x0'] < 420:
                        cp_iban = w['text']
                        break

                # ── Bank reference (poziv na broj: "40002-97526263255-110") ─
                bank_ref = ''
                for w in all_block_words:
                    if re.match(r'^\d{3,5}-\d{8,}-\d+$', w['text']):
                        bank_ref = w['text']
                        break
                # Also try "NNNNN-NNNNNNNNNNN" patterns (HR payment references)
                if not bank_ref:
                    for w in all_block_words:
                        if re.match(r'^\d{4}-\d+$', w['text']):
                            bank_ref = w['text']
                            break

                # ── Description ─────────────────────────────────────────────
                # Take words in the middle zone (x between ~60 and ~430)
                # from the lines AFTER the first (header) row.
                first_row_rk = sorted(block_rows.keys())[0]
                desc_words = []
                for rk in sorted(block_rows.keys()):
                    if rk == first_row_rk:
                        continue  # skip the ID+date header line
                    for w in sorted(block_rows[rk], key=lambda w: w['x0']):
                        if w['x0'] < 430 and w['text'] not in _ADDIKO_SKIP_DESC:
                            if re.match(r'^HR\w{14,}$', w['text']): continue
                            if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']): continue
                            if re.match(r'^\d{16}$', w['text']): continue
                            if re.match(r'^HR(99|00|01|05|17|68|69)$', w['text']): continue
                            if re.match(r'^\d{3,5}-\d{8,}', w['text']): continue
                            desc_words.append(w['text'])
                # Also grab counterparty name from the first line (after ID + dates)
                first_row_words = sorted(block_rows.get(first_row_rk, []), key=lambda w: w['x0'])
                first_name_parts = []
                skip_count = 0  # skip: transaction ID, date, date
                for w in first_row_words:
                    if re.match(r'^\d{16}$', w['text']): skip_count += 1; continue
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']): skip_count += 1; continue
                    if w['x0'] < 430 and w['text'] not in _ADDIKO_SKIP_DESC:
                        if re.match(r'^HR(99|00|01|05|17|68|69)$', w['text']): continue
                        if AMOUNT_RE.match(w['text']): continue
                        first_name_parts.append(w['text'])
                description = ' '.join(first_name_parts + desc_words[:6]).strip()[:120]

                # ── Amounts ──────────────────────────────────────────────────
                debit = credit = 0.0
                for w in all_block_words:
                    if AMOUNT_RE.match(w['text']) and w['x0'] > 420:
                        if w['x0'] < split_x:
                            debit  = parse_amount(w['text'])
                        else:
                            credit = parse_amount(w['text'])

                # Skip footer / balance lines that look like transactions
                if not val_date and debit == 0.0 and credit == 0.0:
                    continue

                seq_counter += 1
                transactions.append({
                    'seq':        seq_counter,
                    'cp_iban':    cp_iban,
                    'bank_ref':   bank_ref,
                    'val_date':   val_date,
                    'exec_date':  exec_date,
                    'description': description,
                    'debit':      debit,
                    'credit':     credit,
                })

    transactions.sort(key=lambda t: (t['val_date'], t['seq']))
    return header, transactions

# ── camt.053 builder ──────────────────────────────────────────────────────────

def build_camt(header, transactions, direction):
    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y-%m-%dT%H:%M:%S")
    iban   = header.get('iban','')
    bic    = header.get('bic','')
    client = header.get('client','')
    client_oib = header.get('client_oib','')
    snum   = header.get('stmt_num','001')
    stmt_date = header.get('stmt_date','')
    msg_id = f"{snum}-{now.strftime('%Y-%m')}"

    if direction=='debit':    txs=[t for t in transactions if t['debit']>0]
    elif direction=='credit': txs=[t for t in transactions if t['credit']>0]
    else:                     txs=list(transactions)
    if not txs: return None

    # Date range from transactions
    dates = [t['val_date'] for t in txs if t.get('val_date')]
    fallback_dt = to_iso(stmt_date) if stmt_date else now.strftime('%Y-%m-%d')
    fr_dt = min(dates) if dates else fallback_dt
    to_dt = max(dates) if dates else fallback_dt

    # Fill any transaction with a missing date using the statement date as fallback
    for t in txs:
        if not t.get('val_date'):
            t['val_date']  = fallback_dt
        if not t.get('exec_date'):
            t['exec_date'] = t['val_date']

    # Opening/closing balances (sum from transactions)
    total_dbit = sum(t['debit']  for t in transactions)
    total_crdt = sum(t['credit'] for t in transactions)

    entries = []
    for t in txs:
        if direction=='debit':    amt=t['debit'];  cd='DBIT'
        elif direction=='credit': amt=t['credit']; cd='CRDT'
        else:
            if t['debit']>0: amt=t['debit'];  cd='DBIT'
            else:            amt=t['credit']; cd='CRDT'
        if amt==0: continue

        cp    = esc(t.get('cp_iban',''))
        desc  = esc(t.get('description',''))
        ref   = esc(t.get('bank_ref',''))
        e2e   = ref if ref else 'HR99'
        val   = t.get('val_date','')
        exe   = t.get('exec_date', val)
        acct_svcr_ref = esc(t.get('bank_ref',''))

        # Counterparty block: Cdtr for DBIT, Dbtr for CRDT
        if cd == 'DBIT':
            cp_block = f"""          <RltdPties>
            <Cdtr>
              <Nm>{desc[:70]}</Nm>
            </Cdtr>
            {'<CdtrAcct><Id><IBAN>' + cp + '</IBAN></Id></CdtrAcct>' if cp else ''}
          </RltdPties>"""
        else:
            cp_block = f"""          <RltdPties>
            <Dbtr>
              <Nm>{desc[:70]}</Nm>
            </Dbtr>
            {'<DbtrAcct><Id><IBAN>' + cp + '</IBAN></Id></DbtrAcct>' if cp else ''}
          </RltdPties>"""

        fmly = 'RCDT' if cd=='CRDT' else 'ICDT'
        entries.append(f"""      <Ntry>
        <Amt Ccy="EUR">{amt:.2f}</Amt>
        <CdtDbtInd>{cd}</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>{val}</Dt></BookgDt>
        <ValDt><Dt>{exe}</Dt></ValDt>
        {('<AcctSvcrRef>' + acct_svcr_ref + '</AcctSvcrRef>') if acct_svcr_ref else ''}
        <BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>{fmly}</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn></BkTxCd>
        <NtryDtls><TxDtls>
          <Refs><EndToEndId>{e2e}</EndToEndId></Refs>
{cp_block}
          <RmtInf><Ustrd>{desc[:140]}</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>""")

    newline = chr(10)

    # MsgRcpt block (recipient = account holder)
    msgrcp = f"""      <MsgRcpt>
        <Nm>{esc(client)}</Nm>
        {('<Id><OrgId><Othr><Id>' + esc(client_oib) + '</Id></Othr></OrgId></Id>') if client_oib else ''}
      </MsgRcpt>"""

    # Svcr block
    svcr = ''
    if bic:
        svcr = f'        <Svcr><FinInstnId><BIC>{esc(bic)}</BIC><Nm>{esc(client)}</Nm></FinInstnId></Svcr>'

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02 camt.053.001.02.xsd">
  <BkToCstmrStmt>
    <GrpHdr>
      <MsgId>{esc(msg_id)}</MsgId>
      <CreDtTm>{ts}</CreDtTm>
{msgrcp}
    </GrpHdr>
    <Stmt>
      <Id>{esc(snum)}</Id>
      <CreDtTm>{ts}</CreDtTm>
      <FrToDt>
        <FrDtTm>{fr_dt}T00:00:00</FrDtTm>
        <ToDtTm>{to_dt}T00:00:00</ToDtTm>
      </FrToDt>
      <Acct>
        <Id>
          <IBAN>{esc(iban)}</IBAN>
        </Id>
        <Ccy>EUR</Ccy>
{svcr}
      </Acct>
      <Bal>
        <Tp><CdOrPrtry><Cd>PRCD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="EUR">{total_dbit:.2f}</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Dt><Dt>{fr_dt}</Dt></Dt>
      </Bal>
      <Bal>
        <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="EUR">{total_crdt:.2f}</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>{to_dt}</Dt></Dt>
      </Bal>
{newline.join(entries)}
    </Stmt>
  </BkToCstmrStmt>
</Document>"""

# ── GUI ───────────────────────────────────────────────────────────────────────

# ── Load parser packs dynamically ────────────────────────────────────────────
BANKS, BANK_TAGS = load_packs()

def detect_parser(path):
    """Auto-detect bank using DETECT_RE from each installed pack."""
    return detect_parser_dynamic(path, BANKS)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Izvadak → camt.053")
        # --- AGGIUNTA ICONA (Versione Dinamica) ---
        import os
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_path, "icona.ico")
        icon_png = os.path.join(base_path, "icona.png")

        try:
            self.iconbitmap(icon_path)
        except:
            try:
                self._icon_img = tk.PhotoImage(file=icon_png)
                self.iconphoto(True, self._icon_img)
            except:
                pass 
        # ------------------------------------------
        self.resizable(False, False)
        self.configure(bg="#f5f5f3")
        self._files = []; self._xml_data = None; self._suggested_name = None
        self._build_ui()
        self.eval("tk::PlaceWindow . center")
        if not PDF_OK:
            messagebox.showerror("Nedostaje biblioteka",
                "pdfplumber nije instaliran.\n\nPokrenite:\n  pip install pdfplumber")

        # Check for parser pack updates in background (non-blocking)
        run_update_check(self, self._on_packs_updated)

    def _on_packs_updated(self, installed_ids: list):
        """Called after UpdateDialog closes. Reloads packs if anything was installed."""
        if not installed_ids:
            return
        _reload_packs()
        self._bank_combo.config(values=list(BANKS.keys()))
        self._bank_var.set(list(BANKS.keys())[0])

    def _build_ui(self):
        W = 580
        outer = tk.Frame(self, bg="#f5f5f3", padx=22, pady=18)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Izvadak → camt.053", font=("Segoe UI",16,"bold"),
                 bg="#f5f5f3", fg="#1a1a1a").pack(anchor="w")
        tk.Label(outer, text="PBZ · ZABA · Erste Bank · Addiko  ·  bez AI  ·  potpuno offline",
                 font=("Segoe UI",9), bg="#f5f5f3", fg="#888").pack(anchor="w", pady=(2,14))

        brow = tk.Frame(outer, bg="#f5f5f3"); brow.pack(fill="x", pady=(0,10))
        tk.Label(brow, text="Banka:", font=("Segoe UI",10), bg="#f5f5f3",
                 fg="#444", width=7, anchor="w").pack(side="left")
        self._bank_var = tk.StringVar(value=list(BANKS.keys())[0])
        self._bank_combo = ttk.Combobox(brow, textvariable=self._bank_var,
                     values=list(BANKS.keys()), state="readonly",
                     width=50, font=("Segoe UI",10))
        self._bank_combo.pack(side="left", padx=(6,0))

        drow = tk.Frame(outer, bg="#f5f5f3"); drow.pack(fill="x", pady=(0,6))
        tk.Label(drow, text="Izvoz:", font=("Segoe UI",10), bg="#f5f5f3",
                 fg="#444", width=7, anchor="w").pack(side="left")
        self._dir_var = tk.StringVar(value="both")
        for val,lbl in [("debit","Isplate (Duguje)"),("credit","Uplate (Potražuje)"),("both","Sve transakcije")]:
            tk.Radiobutton(drow, text=lbl, variable=self._dir_var, value=val,
                           bg="#f5f5f3", font=("Segoe UI",9), fg="#333").pack(side="left", padx=(6,0))

        mrow = tk.Frame(outer, bg="#f5f5f3"); mrow.pack(fill="x", pady=(0,12))
        tk.Label(mrow, text="Način:", font=("Segoe UI",10), bg="#f5f5f3",
                 fg="#444", width=7, anchor="w").pack(side="left")
        self._mode_var = tk.StringVar(value="single")
        tk.Radiobutton(mrow, text="Jedan XML (spoji sve)", variable=self._mode_var,
                       value="single", bg="#f5f5f3", font=("Segoe UI",9), fg="#333").pack(side="left", padx=(6,0))
        tk.Radiobutton(mrow, text="Batch (zaseban XML po datoteci)", variable=self._mode_var,
                       value="batch", bg="#f5f5f3", font=("Segoe UI",9), fg="#333").pack(side="left", padx=(6,0))

        tk.Label(outer, text="PDF datoteke:", font=("Segoe UI",9),
                 bg="#f5f5f3", fg="#555").pack(anchor="w", pady=(0,4))
        lf = tk.Frame(outer, bg="#f5f5f3"); lf.pack(fill="x", pady=(0,6))
        self._lb = tk.Listbox(lf, height=5, font=("Segoe UI",9), selectmode="extended",
                              relief="solid", bd=1, bg="#fff", fg="#222", width=68)
        self._lb.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(lf, orient="vertical", command=self._lb.yview); sb.pack(side="right", fill="y")
        self._lb.config(yscrollcommand=sb.set)

        br = tk.Frame(outer, bg="#f5f5f3"); br.pack(fill="x", pady=(0,12))
        tk.Button(br, text="+ Dodaj PDF", font=("Segoe UI",9), bg="#e8e8e6",
                  relief="flat", padx=10, pady=5, cursor="hand2",
                  command=self._add).pack(side="left")
        tk.Button(br, text="✕ Ukloni", font=("Segoe UI",9), bg="#e8e8e6",
                  relief="flat", padx=10, pady=5, cursor="hand2",
                  command=self._remove).pack(side="left", padx=(8,0))

        self._btn = tk.Button(outer, text="Parsiraj i generiraj camt.053",
                              font=("Segoe UI",11,"bold"), bg="#1a1a1a", fg="#fff",
                              relief="flat", padx=20, pady=10, cursor="hand2",
                              state="disabled", command=self._start)
        self._btn.pack(fill="x", pady=(0,10))

        self._progress = ttk.Progressbar(outer, mode="indeterminate", length=W)
        self._progress.pack(fill="x", pady=(0,8))

        self._status = tk.Label(outer, text="", font=("Segoe UI",9),
                                bg="#f5f5f3", fg="#444", wraplength=W-10, justify="left")
        self._status.pack(anchor="w", pady=(0,8))

        self._save_btn = tk.Button(outer, text="💾  Spremi camt.053 XML",
                                   font=("Segoe UI",11), bg="#16a34a", fg="#fff",
                                   relief="flat", padx=20, pady=10, cursor="hand2",
                                   command=self._save)

    def _add(self):
        paths = filedialog.askopenfilenames(title="Odaberi PDF izvadak(e)",
                    filetypes=[("PDF","*.pdf"),("Sve","*.*")])
        for p in paths:
            if p not in self._files:
                self._files.append(p); self._lb.insert("end", Path(p).name)
        self._btn.config(state="normal" if self._files else "disabled")

    def _remove(self):
        for i in reversed(self._lb.curselection()):
            self._lb.delete(i); del self._files[i]
        self._suggested_name = None
        self._btn.config(state="normal" if self._files else "disabled")

    def _start(self):
        if not self._files: return
        self._btn.config(state="disabled"); self._save_btn.pack_forget()
        self._xml_data = None; self._suggested_name = None
        self._progress.start(10)
        self._status.config(text="Čitam PDF datoteke...", fg="#555")
        threading.Thread(target=self._process, daemon=True).start()

    def _process(self):
        try:
            bank_key  = self._bank_var.get()
            direction = self._dir_var.get()
            mode      = self._mode_var.get()
            auto      = (bank_key == 'Automatski prepoznaj banku')

            if mode == 'batch':
                # ── Batch: one XML per file ──────────────────────────────────
                results = []   # list of (path, xml, summary_line)
                errors  = []
                for i, fp in enumerate(self._files):
                    self.after(0, lambda i=i: self._status.config(
                        text=f"Parsiram {i+1}/{len(self._files)}: {Path(fp).name}...", fg="#555"))
                    try:
                        if auto:
                            detected_key, parser = detect_parser(fp)
                            tag = BANK_TAGS.get(detected_key, 'bank')
                        else:
                            parser = BANKS[bank_key]
                            tag = BANK_TAGS.get(bank_key, 'bank')
                        h, txs = parser(fp)
                        txs.sort(key=lambda t:(t.get('val_date',''), t.get('seq',0)))
                        xml = build_camt(h, txs, direction)
                        if xml is None:
                            errors.append(f"{Path(fp).name}: nema transakcija za odabrani smjer")
                            continue
                        stem = Path(fp).stem
                        out_name = f"{stem}_camt053_{tag}.xml"
                        td = sum(t['debit']  for t in txs)
                        tc = sum(t['credit'] for t in txs)
                        results.append((out_name, xml,
                            f"{Path(fp).name} → {out_name}  "
                            f"({len(txs)} tx, -{td:.2f} / +{tc:.2f} EUR)"
                            + (f" [{detected_key.split('–')[0].strip()}]" if auto else "")))
                    except Exception as e:
                        errors.append(f"{Path(fp).name}: {e}")

                if not results:
                    self.after(0, self._on_error,
                               "Niti jedna datoteka nije konvertirana.\n" + "\n".join(errors))
                    return

                # Ask user for output folder (must run on main thread)
                folder_holder = [None]
                done_ev = threading.Event()
                def ask_folder():
                    folder_holder[0] = filedialog.askdirectory(title="Odaberi mapu za batch XML datoteke")
                    done_ev.set()
                self.after(0, ask_folder)
                done_ev.wait()
                folder = folder_holder[0]
                if not folder:
                    self.after(0, self._on_error, "Batch izvoz otkazan (nije odabrana mapa).")
                    return

                saved = []
                for out_name, xml, _ in results:
                    out_path = Path(folder) / out_name
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(xml)
                    saved.append(str(out_path))

                lines = [f"✅  Batch gotovo! Spremljeno {len(saved)}/{len(self._files)} datoteka u:\n   {folder}"]
                for _, _, line in results:
                    lines.append("   • " + line)
                if errors:
                    lines.append(f"\n⚠️  Greške ({len(errors)}):")
                    for e in errors:
                        lines.append("   • " + e)
                self.after(0, self._on_success_batch, "\n".join(lines))

            else:
                # ── Single XML (merge all files) ─────────────────────────────
                all_txs = []; master_header = {}; detected_tags = []
                for i, fp in enumerate(self._files):
                    self.after(0, lambda i=i: self._status.config(
                        text=f"Parsiram {i+1}/{len(self._files)}...", fg="#555"))
                    if auto:
                        detected_key, parser = detect_parser(fp)
                        detected_tags.append(BANK_TAGS.get(detected_key, 'bank'))
                    else:
                        parser = BANKS[bank_key]
                    h, txs = parser(fp)
                    if not master_header: master_header = h
                    all_txs.extend(txs)

                all_txs.sort(key=lambda t:(t.get('val_date',''), t.get('seq',0)))
                td = sum(t['debit']  for t in all_txs)
                tc = sum(t['credit'] for t in all_txs)
                dn = sum(1 for t in all_txs if t['debit']  > 0)
                cn = sum(1 for t in all_txs if t['credit'] > 0)

                xml = build_camt(master_header, all_txs, direction)
                if xml is None:
                    self.after(0, self._on_error, "Nema transakcija za odabrani smjer."); return
                self._xml_data = xml

                # Build suggested filename fresh from current file list
                first_stem = Path(self._files[0]).stem
                if auto:
                    tag = detected_tags[0] if detected_tags else 'bank'
                else:
                    tag = BANK_TAGS.get(bank_key, 'bank')
                self._suggested_name = f"{first_stem}_camt053_{tag}.xml"

                auto_note = ""
                if auto and detected_tags:
                    unique = list(dict.fromkeys(detected_tags))
                    auto_note = f"\n   Prepoznato: {', '.join(unique)}"

                summary = (f"✅  Gotovo!{auto_note}\n"
                           f"   Datoteke: {len(self._files)}  |  Transakcije: {len(all_txs)}\n"
                           f"   Isplate: {dn} = {td:.2f} EUR\n"
                           f"   Uplate:  {cn} = {tc:.2f} EUR\n"
                           f"   IBAN: {master_header.get('iban','—')}  |  {master_header.get('client','—')}")
                self.after(0, self._on_success, summary)

        except Exception as e:
            import traceback
            self.after(0, self._on_error, f"{e}\n\n{traceback.format_exc()[-600:]}")

    def _on_success(self, msg):
        self._progress.stop(); self._status.config(text=msg, fg="#166534")
        self._btn.config(state="normal"); self._save_btn.pack(fill="x")

    def _on_success_batch(self, msg):
        self._progress.stop(); self._status.config(text=msg, fg="#166534")
        self._btn.config(state="normal")
        # No save button needed – files already written to disk

    def _on_error(self, msg):
        self._progress.stop(); self._status.config(text=f"❌  {msg}", fg="#b91c1c")
        self._btn.config(state="normal")

    def _save(self):
        if not self._xml_data: return
        suggested = self._suggested_name or f"camt053_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
        path = filedialog.asksaveasfilename(
            title="Spremi camt.053", defaultextension=".xml",
            initialfile=suggested,
            filetypes=[("XML","*.xml")])
        if path:
            with open(path,"w",encoding="utf-8") as f: f.write(self._xml_data)
            messagebox.showinfo("Spremljeno", f"Datoteka spremljena:\n{path}")

if __name__ == "__main__":
    App().mainloop()
