"""
Parser Pack: PBZ – Privredna Banka Zagreb
Format:      Mjesečni izvadak (PDF)
Version:     1.0.1
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'PBZ – Privredna Banka Zagreb (mjesečni izvadak)'
BANK_TAG  = 'PBZ'
DETECT_RE = r'Privredna banka Zagreb|PBZ d\.d\.|Izvadak EUR br'

def parse(path):
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
            DATE_RE_W = re.compile(r'^\d{2}\.\d{2}\.\d{4}\.?$')
            rows_w = defaultdict(list)
            for w in words:
                rows_w[round(w['top'])].append(w)

            for m in re.finditer(
                r'^(\d+)\.\s+([A-Z]{2}\d{2}\w+)\s+(\S+)',
                text, re.MULTILINE):
                seq = int(m.group(1))
                if seq in transactions: continue
                cp_iban  = m.group(2)
                bank_ref = m.group(3)
                sep = text.find('_'*20, m.end())
                block = text[m.start():sep] if sep!=-1 else text[m.start():]

                block_dates_text = re.findall(r'\b(\d{2}\.\d{2}\.\d{4})\b', block)
                clean_dates = []
                for line in block.split('\n'):
                    line = line.strip()
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
                    val_date  = to_iso(block_dates_text[-2]) if len(block_dates_text) >= 2 else to_iso(block_dates_text[-1])
                    exec_date = to_iso(block_dates_text[-1])
                else:
                    val_date = exec_date = ''

                lines = [l.strip() for l in block.split('\n') if l.strip()]
                desc_parts = []
                for l in lines[1:]:
                    if re.match(r'^[A-Z]{2}\d{2}[\d ]', l): continue
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
