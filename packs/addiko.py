"""
Parser Pack: Addiko Bank
Format:      Izvadak o stanju i prometu po računu (PDF)
Version:     1.0.0
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'Addiko Bank (izvadak o stanju i prometu)'
BANK_TAG  = 'Addiko'
DETECT_RE = r'Addiko|HAABHR22'

_ADDIKO_SKIP_DESC = {
    'Ukupni','promet','Broj','naloga','duguje:','potražuje:',
    'Rezervacija','kartične','transakcije:','prisilne','naplate:',
    'Najava','plaćanja','za:','Novo','stanje:','EUR',
    'Početno','stanje','Dozvoljeno','prekoračenje:',
}

def parse(path):
    header = {}
    transactions = []
    seq_counter = 0

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words: continue
            if not header.get('iban'):
                m = re.search(r'Broj računa-IBAN:\s*(HR[\d\s]{15,})', text)
                if m: header['iban'] = re.sub(r'\s+', '', m.group(1))
                header['bic'] = 'HAABHR22'
                m = re.search(r'OIB\s*/\s*MB:\s*(\d{11})', text)
                if m: header['client_oib'] = m.group(1)
                m = re.search(r'Broj\s*/\s*datum:\s*(\d+)\s*/\s*(\d{2}\.\d{2}\.\d{4})', text)
                if m:
                    header['stmt_num'] = m.group(1).zfill(3)
                    header['stmt_date'] = m.group(2)
                m = re.search(r'^(GALIĆ[^\n]+|[A-ZŠĐČĆŽ][A-ZŠĐČĆŽ\s]+(?:D\.O\.O\.|d\.o\.o\.|j\.d\.o\.o\.))', text, re.MULTILINE)
                if m: header['client'] = m.group(1).strip()

            duguje_x = None; potrazuje_x = None
            for w in words:
                if w['text'] == 'Duguje':    duguje_x    = w['x0']
                if w['text'] == 'Potražuje': potrazuje_x = w['x0']
            if duguje_x is None:    duguje_x    = 480
            if potrazuje_x is None: potrazuje_x = 545
            split_x = (duguje_x + potrazuje_x) / 2

            rows = defaultdict(list)
            for w in words: rows[round(w['top'] / 3) * 3].append(w)
            sorted_rows = sorted(rows.keys())

            tx_header_rows = []
            for rk in sorted_rows:
                row = sorted(rows[rk], key=lambda w: w['x0'])
                for w in row:
                    if re.match(r'^\d{16}$', w['text']):
                        tx_header_rows.append(rk); break

            for hi, hk in enumerate(tx_header_rows):
                end_rk = tx_header_rows[hi + 1] if hi + 1 < len(tx_header_rows) else max(sorted_rows) + 100
                block_rows = {rk: rows[rk] for rk in sorted_rows if hk <= rk < end_rk}
                all_block_words = [w for rk, row in block_rows.items() for w in row]

                dates_in_block = []
                for rk in sorted(block_rows.keys()):
                    for w in sorted(block_rows[rk], key=lambda w: w['x0']):
                        if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']):
                            dates_in_block.append(w['text'])
                val_date  = to_iso(dates_in_block[0]) if dates_in_block else ''
                exec_date = to_iso(dates_in_block[1]) if len(dates_in_block) > 1 else val_date

                cp_iban = ''
                for w in all_block_words:
                    if re.match(r'^HR\w{14,}$', w['text']) and w['x0'] < 420:
                        cp_iban = w['text']; break

                bank_ref = ''
                for w in all_block_words:
                    if re.match(r'^\d{3,5}-\d{8,}-\d+$', w['text']):
                        bank_ref = w['text']; break
                if not bank_ref:
                    for w in all_block_words:
                        if re.match(r'^\d{4}-\d+$', w['text']):
                            bank_ref = w['text']; break

                first_row_rk = sorted(block_rows.keys())[0]
                desc_words = []
                for rk in sorted(block_rows.keys()):
                    if rk == first_row_rk: continue
                    for w in sorted(block_rows[rk], key=lambda w: w['x0']):
                        if w['x0'] < 430 and w['text'] not in _ADDIKO_SKIP_DESC:
                            if re.match(r'^HR\w{14,}$', w['text']): continue
                            if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']): continue
                            if re.match(r'^\d{16}$', w['text']): continue
                            if re.match(r'^HR(99|00|01|05|17|68|69)$', w['text']): continue
                            if re.match(r'^\d{3,5}-\d{8,}', w['text']): continue
                            desc_words.append(w['text'])
                first_row_words = sorted(block_rows.get(first_row_rk, []), key=lambda w: w['x0'])
                first_name_parts = []
                for w in first_row_words:
                    if re.match(r'^\d{16}$', w['text']): continue
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', w['text']): continue
                    if w['x0'] < 430 and w['text'] not in _ADDIKO_SKIP_DESC:
                        if re.match(r'^HR(99|00|01|05|17|68|69)$', w['text']): continue
                        if AMOUNT_RE.match(w['text']): continue
                        first_name_parts.append(w['text'])
                description = ' '.join(first_name_parts + desc_words[:6]).strip()[:120]

                debit = credit = 0.0
                for w in all_block_words:
                    if AMOUNT_RE.match(w['text']) and w['x0'] > 420:
                        if w['x0'] < split_x: debit  = parse_amount(w['text'])
                        else:                  credit = parse_amount(w['text'])

                if not val_date and debit == 0.0 and credit == 0.0: continue
                seq_counter += 1
                transactions.append({
                    'seq': seq_counter, 'cp_iban': cp_iban, 'bank_ref': bank_ref,
                    'val_date': val_date, 'exec_date': exec_date,
                    'description': description, 'debit': debit, 'credit': credit,
                })

    transactions.sort(key=lambda t: (t['val_date'], t['seq']))
    return header, transactions
