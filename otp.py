"""
Parser Pack: OTP Banka
Format:      Izvještaj o stanju i prometu – IZVOD PS (PDF)
Version:     1.0.0

Layout: one izvod per PDF page; each page has its own header block with
IBAN, izvod broj, dan. Transactions start with a date row at x0≈33,
followed by IBAN at x0≈84, ref at x0≈244, description at x0≈400,
and amounts in Duguje (x0≈709–782) / Potražuje (x0≈782+) columns.
Red.broj (sequence 1,2,3…) appears one row below the date row at x0≈64.
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'OTP Banka (izvještaj o stanju i prometu)'
BANK_TAG  = 'OTP'
DETECT_RE = r'OTP\s*banka|OTPVHR2X|IZVOD\s+PS'

_SKIP_DESC = {
    'Ukupni', 'promet:', 'Novo', 'stanje:', 'Početno', 'stanje:',
    'Duguje', 'Potražuje', 'Iznos', 'Šifra', 'Opis', 'plaćanja',
}


def parse(path: str):
    header: dict = {}
    transactions: list = []
    global_seq = 0

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text  = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words:
                continue
            if 'IZVOD' not in text and 'OTPVHR2X' not in text:
                continue

            # ── Per-page header ──────────────────────────────────────────
            page_header: dict = {}
            m = re.search(r'Račun\s+\(IBAN\):\s*(HR\w+)', text)
            if m:
                page_header['iban'] = m.group(1)
            m = re.search(r'BIC:(OTP\w+)', text)
            if m:
                page_header['bic'] = m.group(1)
            m = re.search(r'OIB:(\d{11})', text)
            if m:
                page_header['client_oib'] = m.group(1)
            m = re.search(r'^(WRONG WAY|[\w\s]+d\.o\.o\.)', text, re.MULTILINE)
            if m:
                page_header['client'] = m.group(1).strip()
            m = re.search(r'Izvod\s+broj:\s*(\d+)', text)
            if m:
                page_header['stmt_num'] = m.group(1).zfill(3)
            m = re.search(r'na\s+dan:\s*(\d{2}\.\d{2}\.\d{4})', text)
            if m:
                page_header['stmt_date'] = m.group(1)

            # Use first page for global header
            if not header:
                header = dict(page_header)
            else:
                # Keep latest stmt_date across pages
                if page_header.get('stmt_date'):
                    header['stmt_date'] = page_header['stmt_date']

            # ── Column positions ─────────────────────────────────────────
            duguje_x    = 709.0
            potrazuje_x = 782.0
            for w in words:
                if w['text'] == 'Duguje':    duguje_x    = w['x0']
                if w['text'] == 'Potražuje': potrazuje_x = w['x0']
            split_x = (duguje_x + potrazuje_x) / 2

            # ── Group words into rows ────────────────────────────────────
            rows: dict[int, list] = defaultdict(list)
            for w in words:
                rows[round(w['top'] / 2) * 2].append(w)
            sorted_rks = sorted(rows.keys())

            # ── Find transaction header rows ─────────────────────────────
            # A tx header row has: date at x0≈33 AND IBAN at x0≈84
            tx_header_rks = []
            for rk in sorted_rks:
                row = rows[rk]
                has_date = any(
                    re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']) and w['x0'] < 50
                    for w in row
                )
                has_iban = any(
                    re.match(r'^(HR|IE|NL|DE|SI|AT|GB|FR)\w{10,}$', w['text']) and 80 < w['x0'] < 100
                    for w in row
                )
                if has_date and has_iban and rk > 110:
                    tx_header_rks.append(rk)

            for hi, hk in enumerate(tx_header_rks):
                end_rk = (tx_header_rks[hi + 1]
                          if hi + 1 < len(tx_header_rks)
                          else max(sorted_rks) + 100)

                block = {rk: rows[rk] for rk in sorted_rks if hk <= rk < end_rk}
                all_words_blk = [w for rk, row in block.items() for w in row]

                # date (val_date = booking date for OTP)
                date_ws = [w for w in rows[hk]
                           if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']) and w['x0'] < 50]
                val_date = to_iso(date_ws[0]['text']) if date_ws else ''

                # counterparty IBAN
                cp_iban = ''
                for w in rows[hk]:
                    if re.match(r'^(HR|IE|NL|DE|SI|AT|GB|FR)\w{10,}$', w['text']) and 80 < w['x0'] < 100:
                        cp_iban = w['text']
                        break

                # bank_ref: PNB platitelja (x0≈244 area), type codes like IPR/ZOU/ZIN
                bank_ref = ''
                ref_type = ''
                for w in rows[hk]:
                    if re.match(r'^(IPR|ZOU|ZIN|IUP)$', w['text']) and w['x0'] > 230:
                        ref_type = w['text']
                    if re.match(r'^\d{7,}$', w['text']) and w['x0'] > 244:
                        bank_ref = w['text']
                        break
                if ref_type and bank_ref:
                    bank_ref = f"{ref_type} {bank_ref}"

                # PNB primatelja rows (HR00/HR17/HR67/HR68/HR69/HR99 + value)
                pnb_ref = ''
                for rk, row in block.items():
                    for w in row:
                        if (re.match(r'^HR(00|17|67|68|69|05|01|99)$', w['text'])
                                and w['x0'] > 230):
                            # get next word as reference value
                            row_sorted = sorted(row, key=lambda x: x['x0'])
                            idx = next((i for i, x in enumerate(row_sorted)
                                        if x['text'] == w['text']), -1)
                            if idx >= 0 and idx + 1 < len(row_sorted):
                                pnb_ref = row_sorted[idx + 1]['text']
                                break
                    if pnb_ref:
                        break

                # description: words at x0≈400+ on all block rows, skip amounts & refs
                desc_words = []
                for rk in sorted(block.keys()):
                    row = block[rk]
                    for w in sorted(row, key=lambda x: x['x0']):
                        if w['x0'] < 395 or w['x0'] > duguje_x - 5:
                            continue
                        if AMOUNT_RE.match(w['text']):
                            continue
                        if w['text'] in _SKIP_DESC:
                            continue
                        if re.match(r'^HR(00|17|67|68|69|05|01|99)$', w['text']):
                            continue
                        desc_words.append(w['text'])
                    if len(desc_words) >= 10:
                        break
                description = ' '.join(desc_words[:10]).strip()[:120]

                # amounts
                debit = credit = 0.0
                for w in all_words_blk:
                    if AMOUNT_RE.match(w['text']) and w['x0'] > duguje_x - 10:
                        if w['x0'] < split_x:
                            debit  = parse_amount(w['text'])
                        else:
                            credit = parse_amount(w['text'])

                if not val_date:
                    continue

                global_seq += 1
                transactions.append({
                    'seq':         global_seq,
                    'cp_iban':     cp_iban,
                    'bank_ref':    bank_ref,
                    'val_date':    val_date,
                    'exec_date':   val_date,
                    'description': description,
                    'debit':       debit,
                    'credit':      credit,
                })

    transactions.sort(key=lambda t: (t['val_date'], t['seq']))
    return header, transactions
