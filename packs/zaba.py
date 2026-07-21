"""
Parser Pack: ZABA – Zagrebačka Banka
Format:      Dnevni izvod (PDF)
Version:     1.0.6

Layout notes (from real PDFs):
  - Bank IBAN:  label 'IBAN:' (with colon) at top≈82,  x0≈42  → SKIP
  - Owner IBAN: label 'IBAN'  (no colon)   at top≈226, x0≈42,
                followed immediately by HR... token on same row  → USE THIS
  - Continuation pages lack 'IZVADAK'/'Račun' header words —
    processed if same doc (is_zaba_doc flag) AND have transaction rows.
  - cp_name column: x0 220-320 (counterparty name + address lines)
  - description column: x0 310-465
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'ZABA – Zagrebačka Banka (dnevni izvod)'
BANK_TAG  = 'ZABA'
DETECT_RE = r'Zagrebačka banka|ZABAHR2X|IZVADAK'

_ZABA_SKIP = {'PRETHODNO','STANJE','NOVO','Ukupan','plaćanja:','STANJE:','broj','iznos','plaćanja','(br.'}

def _has_transaction_rows(words):
    """Return True if page has any numbered transaction rows (seq col at x0<60)."""
    for w in words:
        if re.match(r'^\d{1,3}$', w['text']) and w['x0'] < 60 and w['top'] > 80:
            return True
    return False

def parse(path):
    header = {}
    transactions = []
    is_zaba_doc = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            if not words:
                continue

            word_texts = {w['text'] for w in words}

            # Page 1 gate: must have IZVADAK or Račun to confirm ZABA statement
            if not is_zaba_doc:
                if 'IZVADAK' not in word_texts and 'Račun' not in word_texts:
                    continue
                is_zaba_doc = True

            # Continuation pages: skip non-transaction pages (e.g. deposit info page)
            if 'IZVADAK' not in word_texts and 'Račun' not in word_texts:
                if not _has_transaction_rows(words):
                    continue

            # Detect debit/credit column positions
            duguje_x = 469; credit_x = 529
            for w in words:
                if w['text'] == 'Duguje': duguje_x = w['x0']
                if w['text'] == 'Potražuje': credit_x = w['x0']
            split_x = (duguje_x + credit_x) / 2

            # --- Header extraction (once, from first valid page) ---
            if not header.get('iban'):
                # Strategy 1: 'IBAN' label WITHOUT colon at x0<80 → owner account
                # The bank's own label is 'IBAN:' with a colon — we skip that.
                for i, w in enumerate(words):
                    if w['text'] == 'IBAN' and w['x0'] < 80:
                        for w2 in words[i+1:i+4]:
                            if abs(w2['top'] - w['top']) > 6:
                                break
                            candidate = re.sub(r'\s+', '', w2['text'])
                            if re.match(r'^HR\d{19,}$', candidate):
                                header['iban'] = candidate
                                break
                        if header.get('iban'):
                            break

                # Strategy 2: full-page text scan, skip ZABA's own bank IBANs
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
                    if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$', w['text']) and w['x0'] < 100:
                        header['stmt_date'] = w['text'].rstrip('.'); break
                for i, w in enumerate(words):
                    if w['text'] in ('J.D.O.O.','D.O.O.','d.o.o.','j.d.o.o.') and w['x0'] > 300:
                        cw = [words[j]['text'] for j in range(max(0,i-6),i+1) if words[j]['x0'] > 300]
                        header['client'] = ' '.join(cw); break
                                              
            # Fallback: client has no D.O.O./J.D.O.O. suffix (individual,
            # obrt, or other entity type). Grab the recipient block that
            # sits below the MB: line instead — it holds name + address
            # regardless of legal form.
                if not header.get('client'):
                    for w in words:
                        if w['text'] == 'MB:' and w['x0'] > 300:
                            mb_top = w['top']
                            block_rows = defaultdict(list)
                            for w2 in words:
                                if 300 < w2['x0'] < 500 and w2['top'] > mb_top + 5:
                                    block_rows[round(w2['top'])].append(w2['text'])
                            collected = []
                            for top in sorted(block_rows.keys()):
                                line = ' '.join(block_rows[top])
                                if '(cid:' in line: continue
                                if re.match(r'^\d{4,}-\w+$', line): continue  # barcode ref code
                                collected.append(line)
                                if re.search(r'^\d{5}\b', line): break  # postal code = end of block
                                if len(collected) >= 5: break
                            if collected:
                                header['client'] = ', '.join(collected)[:140]
                            break

                for w in words:
                    if w['text'] == 'OIB:' and w['x0'] > 300:
                        idx = words.index(w)
                        if idx+1 < len(words): header['client_oib'] = words[idx+1]['text']; break

            # --- Transaction row extraction ---
            rows = defaultdict(list)
            for w in words:
                rows[round(w['top']/3)*3].append(w)
            sorted_rows = sorted(rows.keys())

            for ri, rk in enumerate(sorted_rows):
                row = sorted(rows[rk], key=lambda w: w['x0'])
                if not row: continue
                first = row[0]
                if not (re.match(r'^\d{1,3}$', first['text']) and first['x0'] < 60 and first['top'] > 80):
                    continue
                if len(row) < 2: continue
                dm = re.match(r'^(\d{2}\.\d{2}\.\d{4})\.?$', row[1]['text'])
                if not dm: continue

                seq = int(first['text'])
                book_date = to_iso(dm.group(1))
                exec_date = book_date
                if len(row) > 2:
                    dm2 = re.match(r'^(\d{2}\.\d{2}\.\d{4})\.?$', row[2]['text'])
                    if dm2: exec_date = to_iso(dm2.group(1))

                debit = credit = 0.0
                for w in row:
                    if AMOUNT_RE.match(w["text"]) and w["x0"] > 450:
                        is_negative = w["text"].startswith("-")
                        amt = parse_amount(w["text"])
                        if w["x0"] < split_x:
                            # Negative debit = reversal → goes to credit side
                            if is_negative: credit = amt
                            else: debit = amt
                        else:
                            # Negative credit = reversal → goes to debit side
                            if is_negative: debit = amt
                            else: credit = amt

                cp_iban = ''
                for w in row:
                    if re.match(r'^(?:HR|DE|SI|BE|LT|AT|GB|FR|NL)\w{14,}$', w['text']) and w['x0'] > 150:
                        cp_iban = w['text']; break

                desc_parts = []; bank_ref = ''; cp_name_parts = []

                # cp_name: counterparty name column x0 220-320 (header row)
                for w in row:
                    if 220 < w['x0'] < 312 and w['text'] not in _ZABA_SKIP:
                        if not re.match(r'^(?:HR|DE|SI|AT|GB|FR|NL)\w{5,}$', w['text']):
                            if not re.match(r'^HR\d{2}$', w['text']):
                                cp_name_parts.append(w['text'])

                for next_rk in sorted_rows[ri+1:ri+6]:
                    nr = sorted(rows[next_rk], key=lambda w: w['x0'])
                    if nr and re.match(r'^\d{1,3}$', nr[0]['text']) and nr[0]['x0'] < 60:
                        break
                    for w in nr:
                        # counterparty name column: x0 220-320
                        if 220 < w['x0'] < 312 and w['text'] not in _ZABA_SKIP:
                            if not re.match(r'^(?:HR|DE|SI|AT|GB|FR|NL)\w{5,}$', w['text']):
                                if not re.match(r'^HR\d{2}$', w['text']):
                                    cp_name_parts.append(w['text'])
                        # description column: x0 310-465
                        if w['x0'] > 310 and w['x0'] < 465 and w['text'] not in _ZABA_SKIP:
                            if not re.match(r'^HR\d{2}$', w['text']):
                                desc_parts.append(w['text'])
                    if not bank_ref:
                        for w in nr:
                            if w['x0'] > 65 and w['x0'] < 155 and len(w['text']) > 8:
                                bank_ref = w['text']; break

                cp_name = ' '.join(cp_name_parts[:6]).strip()[:70]

                # Build description: join all collected parts (120-char limit
                # keeps it safe). For STORNO rows ZABA appends the original
                # transaction date (YYYY-MM-DD) at the end of the description
                # column — we need enough slots to include it, hence [:20].
                description = ' '.join(desc_parts[:20]).strip()[:120]

                transactions.append({
                    'seq': seq, 'cp_iban': cp_iban, 'bank_ref': bank_ref,
                    'cp_name': cp_name,
                    'val_date': book_date, 'exec_date': exec_date,
                    'description': description,
                    'debit': debit, 'credit': credit,
                })

    return header, transactions
