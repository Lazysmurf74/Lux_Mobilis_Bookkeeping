"""
Parser Pack: HPB – Hrvatska Poštanska Banka
Format:      Izvadak o promjenama i stanju na računu (PDF)
Version:     1.0.0

Layout: single-page-per-month, one transaction per row group.
Each transaction row starts with a date at x0≈47, description at x0≈95,
optional amount in ISPLATE col (x0≈398–450) or UPLATE col (x0≈450–510).
Running balance in STANJE col (x0>510) — we use sign-change to assign D/C.
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'HPB – Hrvatska Poštanska Banka (izvadak)'
BANK_TAG  = 'HPB'
DETECT_RE = r'Hrvatska\s+Po[šs]tanska\s+Banka|HPB\s+d\.d\.|IZVADAK\s+O\s+PROMJENAMA'

_SKIP_WORDS = {
    'POČETNO', 'STANJE', 'NOVO', 'U', 'EUR', 'NA', 'DAN',
    'DATUM', 'VALUTE', 'OPIS', 'TRANSAKCIJE', 'ISPLATE', 'UPLATE',
    'Za', 'programski', 'naplaćene', 'naknade', 'po', 'računu',
    'nije', 'zaračunat', 'porez', 'na', 'promet', 'dodanu',
    'vrijednost', 'sukladno', 'Zakonu', 'o', 'PDV-u.',
    'Informacije', 'obradi', 'osobnih', 'podataka', 'dostupne',
    'su', 'mrežnoj', 'stranici', 'Banke', 'str:', 'od:',
    '%[A7161034]', 'Poslovna', 'mreža', 'banke', 'Vašoj',
    'neposrednoj', 'blizini:', 'POJMOVNIK:',
}


def parse(path: str):
    header = {}
    transactions = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text  = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words:
                continue

            # ── Header (first page only) ──────────────────────────────────
            if not header.get('iban'):
                m = re.search(r'IBAN\s+(HR[\d\s]{15,})', text)
                if m:
                    header['iban'] = re.sub(r'\s+', '', m.group(1))
                m = re.search(r'OIB:\s*(\d{11})', text)
                if m:
                    header['client_oib'] = m.group(1)
                header['bic'] = 'HPBZHR2X'
                # client name: bold block before OIB
                name_words = [w for w in words
                              if w['x0'] < 300 and 160 < w['top'] < 220
                              and w['text'] not in _SKIP_WORDS]
                if name_words:
                    header['client'] = ' '.join(w['text'] for w in
                                                sorted(name_words, key=lambda w: (w['top'], w['x0'])))
                # stmt date from "IZVOD ZA TRAVANJ 2026." → use last date found
                m = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
                if m:
                    header['stmt_date'] = m.group(1)
                # stmt_num not present in HPB monthly statement

            # ── Column x-boundaries ──────────────────────────────────────
            isplate_x  = 398.0   # ISPLATE column left edge (debit)
            uplate_x   = 450.0   # UPLATE column left edge (credit)
            stanje_x   = 510.0   # STANJE column left edge (running balance)

            for w in words:
                if w['top'] > 295:   # only column header area
                    break
                if w['text'] == 'ISPLATE':  isplate_x = w['x0']
                if w['text'] == 'UPLATE':   uplate_x  = w['x0']
                if w['text'] == 'STANJE' and w['x0'] > 400:  stanje_x = w['x0']

            split_x = (isplate_x + uplate_x) / 2   # mid between debit/credit cols

            # ── Group words into rows ────────────────────────────────────
            rows: dict[int, list] = defaultdict(list)
            for w in words:
                rows[round(w['top'] / 2) * 2].append(w)
            sorted_rks = sorted(rows.keys())

            # ── Find transaction header rows (date at x0≈47, top>290) ───
            tx_header_rks = []
            for rk in sorted_rks:
                row = rows[rk]
                for w in row:
                    if (re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text'])
                            and w['x0'] < 60
                            and w['top'] > 290):
                        tx_header_rks.append(rk)
                        break

            seq = 0
            for hi, hk in enumerate(tx_header_rks):
                end_rk = (tx_header_rks[hi + 1]
                          if hi + 1 < len(tx_header_rks)
                          else max(sorted_rks) + 100)

                block = {rk: rows[rk] for rk in sorted_rks if hk <= rk < end_rk}
                all_words = [w for rk, row in block.items() for w in row]

                # date
                date_w = [w for w in rows[hk] if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']) and w['x0'] < 60]
                val_date = to_iso(date_w[0]['text']) if date_w else ''

                # cp_iban: HR... in description area
                cp_iban = ''
                for w in all_words:
                    if re.match(r'^HR\w{14,}$', w['text']) and w['x0'] < 380:
                        cp_iban = w['text']
                        break

                # bank_ref: SZG or ZG codes, or F-codes
                bank_ref = ''
                for w in all_words:
                    if re.match(r'^(SZG|ZG)\d+$', w['text']):
                        bank_ref = w['text']
                        break
                if not bank_ref:
                    for w in all_words:
                        if re.match(r'^F\d{5,}$', w['text']):
                            bank_ref = w['text']
                            break

                # description: words in x 95–390, first row only, skip IBANs/refs/dates
                desc_words = sorted(
                    [w for w in rows[hk]
                     if 92 < w['x0'] < 390
                     and not re.match(r'^HR\w{14,}$', w['text'])
                     and not re.match(r'^\d{2}\.\d{2}\.\d{4}', w['text'])
                     and not re.match(r'^(SZG|ZG)\d+', w['text'])
                     and not re.match(r'^F\d{5,}$', w['text'])
                     and w['text'] not in _SKIP_WORDS],
                    key=lambda w: w['x0']
                )
                # clean semicolons at end of each token
                desc_parts = [w['text'].rstrip(';') for w in desc_words[:8]]
                description = ' '.join(p for p in desc_parts if p).strip()[:120]

                # amounts: debit in isplate col, credit in uplate col
                # stanje col (x0>=stanje_x) is running balance — skip it
                debit = credit = 0.0
                for w in sorted(all_words, key=lambda x: (x['top'], x['x0'])):
                    if not AMOUNT_RE.match(w['text']):
                        continue
                    if w['x0'] >= stanje_x:        # skip running balance
                        continue
                    if w['x0'] >= uplate_x:        # UPLATE col -> credit
                        if credit == 0.0:
                            credit = parse_amount(w['text'])
                    elif w['x0'] >= isplate_x - 10:  # ISPLATE col -> debit
                        if debit == 0.0:
                            debit = parse_amount(w['text'])

                if not val_date and debit == 0.0 and credit == 0.0:
                    continue

                seq += 1
                transactions.append({
                    'seq':         seq,
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
