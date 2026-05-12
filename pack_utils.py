"""
pack_utils.py – Shared helpers imported by every parser pack.
All packs do:  from pack_utils import esc, parse_amount, to_iso, AMOUNT_RE
"""
import re

AMOUNT_RE = re.compile(r'^\d{1,3}(?:\.\d{3})*,\d{2}$')

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def parse_amount(s):
    try:
        return float(str(s).replace('.','').replace(',','.'))
    except:
        return 0.0

def to_iso(d):
    d = str(d).rstrip('.')
    p = d.split('.')
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else d
