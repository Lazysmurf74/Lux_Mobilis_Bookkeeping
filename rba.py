"""
Parser Pack: RBA – Raiffeisen Bank Austria d.d.
Format:      Izvadak o stanju i prometu – IB IZVOD (PDF)
Version:     1.0.0

Layout (single page per izvadak):
  Each transaction block starts with a referenca row at x0≈35 that contains
  EITHER a numeric bank reference (e.g. "532289") OR a long alphanumeric
  reference (e.g. "P012600004548075").  Same row has D/P at x0≈477 and
  Iznos at x0≈524.  Two dates appear: booking date at x0≈412 on the ref row,
  value date at x0≈412 one row below.  The description / Opis transakcije is
  at x0≈216 on the same row.  IBAN of counterparty is at x0≈35 two rows
  below the ref row.  Counterparty name follows at x0≈35.
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'RBA – Raiffeisen Bank Austria (izvadak o stanju i prometu)'
BANK_TAG  = 'RBA'
DETECT_RE = r'Raiffeisen|RZBHHR2|IB\s+IZVOD|IZVADAK\s+O\s+STANJU\s+I\s+PROMETU'

_SKIP_FOOTER = {
    'Proknjiženo', 'stanje', 'Ukupni', 'promet', 'broj', 'naloga:',
    'Dopušteno', 'prekoračenje', 'Rezervacije', 'kartičnim',
    'transakcijama', 'Raspoloživo', 'Ukupne', 'naknade',
    'IZVADAK', 'O', 'STANJU', 'I', 'PROMETU', 'Ispis:',
    'SEKTOR', 'TRANSAKCIJSKIH', 'POSLOVA', 'Početno', 'stanje',
}


def parse(path: str):
    header: dict = {}
    transactions: list = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text  = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words:
                continue
            if 'IZVADAK' not in text and 'Raiffeisen' not in text:
                continue

            # ── Header (first meaningful page only) ──────────────────────
            if not header.get('iban'):
                m = re.search(r'IBAN:\s*(HR\w+)', text)
                if m:
                    header['iban'] = m.group(1)
                m = re.search(r'SWIFT\s+adresa:\s*(\w+)', text)
                if m:
                    header['bic'] = m.group(1)
                m = re.search(r'OIB:\s*(\d{11})', text)
                if m:
                    header['client_oib'] = m.group(1)
                m = re.search(r'Datum:\s*(\d{2}\.\d{2}\.\d{4})', text)
                if m:
                    header['stmt_date'] = m.group(1).rstrip('.')
                m = re.search(r'Broj\s+izvatka:\s*(\d+)', text)
                if m:
                    header['stmt_num'] = m.group(1).zfill(3)
                # client name: right column, bold block above ŽUPANJSKA/address
                name_words = [w for w in words
                              if w['x0'] > 300 and 168 < w['top'] < 210
                              and w['text'] not in _SKIP_FOOTER]
                if name_words:
                    header['client'] = ' '.join(
                        w['text'] for w in sorted(name_words, key=lambda w: (w['top'], w['x0']))
                        if not re.match(r'^\d{5}$', w['text'])
                    )

            # ── Group words into rows ────────────────────────────────────
            rows: dict[int, list] = defaultdict(list)
            for w in words:
                rows[round(w['top'] / 2) * 2].append(w)
            sorted_rks = sorted(rows.keys())

            # ── Find transaction header rows ─────────────────────────────
            # A tx header row has: bank ref at x0≈35 AND D/P marker at x0≈474-480
            # and an amount at x0≈520+
            # (rows before top≈400 are the page header / column titles)
            tx_header_rks = []
            for rk in sorted_rks:
                if rk < 400:
                    continue
                row = rows[rk]
                has_dp = any(w['text'] in ('D', 'P') and 472 < w['x0'] < 482 for w in row)
                has_amount = any(AMOUNT_RE.match(w['text']) and w['x0'] > 515 for w in row)
                has_ref = any(
                    (re.match(r'^\d{6,}$', w['text']) or
                     re.match(r'^[A-Z]\d{10,}$', w['text']))
                    and w['x0'] < 50
                    for w in row
                )
                if has_dp and has_amount and has_ref:
                    tx_header_rks.append(rk)

            seq = 0
            for hi, hk in enumerate(tx_header_rks):
                end_rk = (tx_header_rks[hi + 1]
                          if hi + 1 < len(tx_header_rks)
                          else max(sorted_rks) + 100)

                block = {rk: rows[rk] for rk in sorted_rks if hk <= rk < end_rk}

                ref_row = rows[hk]

                # D/P indicator
                dp = ''
                for w in ref_row:
                    if w['text'] in ('D', 'P') and 472 < w['x0'] < 482:
                        dp = w['text']
                        break

                # amount
                amount = 0.0
                for w in ref_row:
                    if AMOUNT_RE.match(w['text']) and w['x0'] > 515:
                        amount = parse_amount(w['text'])
                        break

                # Skip zero-amount rows (like the "D 0,00" footer separator)
                if amount == 0.0:
                    continue

                debit  = amount if dp == 'D' else 0.0
                credit = amount if dp == 'P' else 0.0

                # bank ref (numeric or alphanumeric at x0<50)
                bank_ref = ''
                for w in ref_row:
                    if (re.match(r'^\d{6,}$', w['text']) or
                            re.match(r'^[A-Z]\d{10,}$', w['text'])) and w['x0'] < 50:
                        bank_ref = w['text']
                        break

                # booking date: x0≈412 on ref row
                book_date_str = ''
                for w in ref_row:
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$', w['text']) and w['x0'] > 400:
                        book_date_str = w['text'].rstrip('.')
                        break

                # value date: x0≈412 on the NEXT row
                val_date_str = book_date_str
                rk_list = sorted(block.keys())
                if len(rk_list) > 1:
                    next_rk = rk_list[1]
                    for w in rows[next_rk]:
                        if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$', w['text']) and w['x0'] > 400:
                            val_date_str = w['text'].rstrip('.')
                            break

                val_date  = to_iso(val_date_str)  if val_date_str  else ''
                exec_date = to_iso(book_date_str) if book_date_str else val_date

                # description: opis transakcije at x0≈216 on the ref row
                desc_words = sorted(
                    [w for w in ref_row if w['x0'] > 210 and w['x0'] < 410
                     and not re.match(r'^\d{2}\.\d{2}\.\d{4}', w['text'])
                     and w['text'] not in _SKIP_FOOTER],
                    key=lambda w: w['x0']
                )
                # also grab continuation desc lines (x0≈216) from next rows
                for rk in rk_list[1:4]:
                    for w in sorted(rows[rk], key=lambda x: x['x0']):
                        if 210 < w['x0'] < 410 and w['text'] not in _SKIP_FOOTER:
                            if not re.match(r'^HR(99|00|17|68|67|05|01)$', w['text']):
                                desc_words.append(w)
                description = ' '.join(w['text'] for w in desc_words[:12]).strip()[:120]

                # counterparty IBAN: HR... at x0≈35 in rows below ref row
                cp_iban = ''
                for rk in rk_list[1:]:
                    for w in rows[rk]:
                        if re.match(r'^HR\w{14,}$', w['text']) and w['x0'] < 50:
                            # skip own IBAN (RBA internal account)
                            if w['text'] != header.get('iban', ''):
                                cp_iban = w['text']
                                break
                    if cp_iban:
                        break

                if not val_date:
                    continue

                seq += 1
                transactions.append({
                    'seq':         seq,
                    'cp_iban':     cp_iban,
                    'bank_ref':    bank_ref,
                    'val_date':    val_date,
                    'exec_date':   exec_date,
                    'description': description,
                    'debit':       debit,
                    'credit':      credit,
                })

    transactions.sort(key=lambda t: (t['val_date'], t['seq']))
    return header, transactions
