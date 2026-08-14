# -*- coding: utf-8 -*-
"""Leitor tolerante de .xlsx (contorna workbook.xml quebrado): sharedStrings + sheet1."""
import zipfile, re, io
import xml.etree.ElementTree as ET
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def _col(ref):  # 'B7' -> indice 1 (0-based)
    m = re.match(r"([A-Z]+)", ref or "")
    if not m: return 0
    c = 0
    for ch in m.group(1):
        c = c * 26 + (ord(ch) - 64)
    return c - 1

def ler_xlsx(path, sheet="xl/worksheets/sheet1.xml"):
    src = io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else path
    z = zipfile.ZipFile(src)
    # shared strings
    sst = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{NS}si"):
            txt = "".join(t.text or "" for t in si.iter(f"{NS}t"))
            sst.append(txt)
    root = ET.fromstring(z.read(sheet))
    linhas = []
    for row in root.iter(f"{NS}row"):
        cells = {}
        maxc = -1
        for c in row.findall(f"{NS}c"):
            ci = _col(c.get("r", ""))
            t = c.get("t")
            v = c.find(f"{NS}v")
            isv = c.find(f"{NS}is")
            if t == "s" and v is not None:
                val = sst[int(v.text)] if v.text and int(v.text) < len(sst) else ""
            elif t == "inlineStr" and isv is not None:
                val = "".join(x.text or "" for x in isv.iter(f"{NS}t"))
            elif v is not None:
                val = v.text or ""
            else:
                val = ""
            cells[ci] = val
            maxc = max(maxc, ci)
        linhas.append([cells.get(i, "") for i in range(maxc + 1)])
    return linhas

if __name__ == "__main__":
    import sys
    L = ler_xlsx(sys.argv[1])
    print("linhas:", len(L))
    for i, r in enumerate(L[:6]):
        print(f"L{i+1}:", [str(c)[:24] for c in r][:14])
