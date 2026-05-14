"""
Parser Pack: Erste Bank
Format:      Periodični izvod prometa (PDF)
Version:     1.0.0
"""
import re
from collections import defaultdict
import pdfplumber
from pack_utils import parse_amount, to_iso, AMOUNT_RE

BANK_KEY  = 'Erste Bank (periodični izvod prometa)'
BANK_TAG  = 'Erste'
DETECT_RE = r'IZVOD PROMETA'

_ERSTE_BAL = {'Stanje','stanje','promet','Promet','S','t','a','n','j','e',
               'Početno','Konačno','Privremeno','Prethodno','Raspoloživo'}

def parse(path):
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
                dw = sorted([w for rk,row in tx_rows.items() for w in row
                              if re.match(r'^\d{2}\.\d{2}\.\d{4}\.?$',w['text']) and w['x0']<40],
                             key=lambda w:w['top'])
                val_date  = to_iso(dw[0]['text']) if dw else ''
                exec_date = to_iso(dw[1]['text']) if len(dw)>1 else val_date
                cp_words = sorted([w for rk,row in tx_rows.items() for w in row
                                    if 70<w['x0']<235
                                    and not re.match(r'^HR\w{14,}$',w['text'])
                                    and not re.match(r'^HR\d{2}$',w['text'])
                                    and not re.match(r'^\d{4}-',w['text'])
                                    and not re.match(r'^\d{2}\.\d{2}\.\d{4}',w['text'])
                                    and w['text'] not in _ERSTE_BAL],
                                   key=lambda w:(w['top'],w['x0']))
                cp_name = ' '.join(w['text'] for w in cp_words[:6]).strip()
                cp_iban = ''
                for rk in sorted(tx_rows.keys()):
                    for w in sorted(tx_rows[rk],key=lambda w:w['x0']):
                        if re.match(r'^HR\w{14,}$',w['text']) and w['x0']<235:
                            cp_iban=w['text']; break
                    if cp_iban: break
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
                bank_ref = ''
                for rk,row in tx_rows.items():
                    for w in row:
                        if re.match(r'^\d{4}-\d+-\d+$',w['text']): bank_ref=w['text']; break
                    if bank_ref: break
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
