"""
Parser Pack: ZABA – Zagrebačka Banka
Format:      Dnevni izvod (PDF)
Version:     1.0.2

Layout notes (from real PDFs):
  - Bank's own IBAN:  label 'IBAN:' (with colon) at top≈82,  x0≈42  → SKIP
  - Owner's IBAN:     label 'IBAN'  (no colon)   at top≈226, x0≈42,
                      followed immediately by HR... token on same row  → USE THIS
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'ZABA – Zagrebačka Banka (dnevni izvod)'
BANK_TAG  = 'ZABA'
DETECT_RE = r'Zagrebačka banka|ZABAHR|IZVADAK'

_ZABA_SKIP = {'PRETHODNO','STANJE','NOVO','Ukupan','plaćanja:','STANJE:','broj','iznos','plaćanja','(br.'}

def parse(path):
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
                # Strategy 1 (precise): find 'IBAN' label WITHOUT a colon
                # (the bank's own IBAN uses 'IBAN:' with colon — we skip that).
                # The owner IBAN label sits at x0≈42, top≈226 and is followed
                # on the same row by the HR... account number.
                for i, w in enumerate(words):
                    if w['text'] == 'IBAN' and w['x0'] < 80:
                        # next word on the same row should be the HR IBAN
                        for w2 in words[i+1:i+4]:
                            if abs(w2['top'] - w['top']) > 6:
                                break  # moved to a different row
                            candidate = re.sub(r'\s+', '', w2['text'])
                            if re.match(r'^HR\d{19,}$', candidate):
                                header['iban'] = candidate
                                break
                        if header.get('iban'):
                            break

                # Strategy 2 (fallback): scan full page text but skip ZABA's
                # own bank account IBANs (prefix HR..2360000 / HR..2360009).
                if not header.get('iban'):
                    text = page.extract_text() or ""
                    all_ibans = re.findall(r'HR\d{2}[\d\s]{15,}', text)
                    cleaned = [re.sub(r'\s+', '', x) for x in all_ibans
                               if len(re.sub(r'\s+', '', x)) >= 19]
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
