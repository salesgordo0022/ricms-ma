# -*- coding: utf-8 -*-
"""Auditor de INTEGRIDADE da base RICMS-MA.

Roda todas as verificações de consistência que garantem que a informação chega
CORRETA ao cliente. Uso:  python auditor_integridade.py  (sai != 0 se houver erro grave).

Categorias:
  ERRO  = quebra a experiência do cliente (dado errado/ausente que ele vê)
  ALERTA = suspeito, revisar (não bloqueia deploy)
"""
import json, re, sys, unicodedata
from pathlib import Path
from collections import defaultdict, Counter

BASE_DIR = Path(__file__).parent
BASE = json.loads((BASE_DIR / "data" / "base.json").read_text(encoding="utf-8"))
BENEF = BASE.get("beneficios", [])


def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def _dig(s):
    return re.sub(r"\D", "", str(s or ""))


ECON = ("isen", "reduc", "diferimento", "credito", "credito presumido", "credito outorgado")
erros, alertas = [], []


def erro(cat, idx, prod, msg):
    erros.append((cat, idx, prod, msg))


def alerta(cat, idx, prod, msg):
    alertas.append((cat, idx, prod, msg))


# ---- 1) NCM: vazio (quebra busca por NCM), formato inválido ----
def check_ncm():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        ncm = (b.get("ncm") or "").strip()
        ben = _norm(b.get("beneficio"))
        econ = any(e in ben for e in ECON)
        # produto de mercadoria (tem ncm_descricao) e benefício econômico, mas SEM ncm -> não acha por NCM
        tem_desc = bool((b.get("ncm_descricao") or "").strip())
        if not ncm and econ and tem_desc:
            erro("NCM_VAZIO", i, prod, f"benefício '{b.get('beneficio')}' sem NCM (não achável por NCM). desc={b.get('ncm_descricao','')[:40]}")
        if ncm:
            d = _dig(ncm)
            if len(d) not in (0, 2, 4, 6, 8):
                alerta("NCM_FORMATO", i, prod, f"NCM '{ncm}' com {len(d)} dígitos (esperado 2/4/6/8)")


# ---- 2) Produto: nome vazio ou genérico ----
def check_produto():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        if not prod:
            erro("PRODUTO_VAZIO", i, "", "item sem nome de produto")
        elif _norm(prod) in ("outros", "outras", "produto", "-", "n/a"):
            alerta("PRODUTO_GENERICO", i, prod, "nome de produto genérico")


# ---- 3) Números do benefício + SIMULAÇÃO do card que o cliente vê ----
def _carga_zero(carga):
    n = _norm(carga)
    return (not n) or ("isent" in n) or re.match(r"^0([.,]0+)?\s*%?$", n) is not None


def check_numeros():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        ben = _norm(b.get("beneficio"))
        carga = (b.get("carga_final") or "").strip()
        reduc = (b.get("reducao_bc") or "").strip()
        trib = _norm(b.get("cst_icms_sugerido"))
        is_st = bool(re.search(r"(\b60\b|\b500\b|^10|201|202)", trib)) or "st" in _norm(b.get("cest"))
        # CLIENT-FACING: benefício econômico que o card renderiza como "23% tributado integral"
        # (sem carga, sem redução, sem isenção, sem ST) -> cliente NÃO vê o benefício.
        # 'manutenção/crédito' é benefício do LADO DO CRÉDITO (não muda a carga da saída) -> não conta.
        eh_carga = any(e in ben for e in ("reduc", "diferimento")) or ("credito presumido" in ben) or ("credito outorgado" in ben)
        if eh_carga and "isen" not in ben and "manuten" not in ben \
           and not carga and not reduc and not is_st:
            erro("CARD_MOSTRA_23PCT", i, prod, f"benefício '{b.get('beneficio')}' sem carga/redução/ST -> card mostra 23% (sem benefício) ao cliente")
        # isenção com carga NÃO-zero é contradição — salvo benefício DUPLO (isenção OU redução)
        if "isen" in ben and "reduc" not in ben and carga and not _carga_zero(carga):
            alerta("ISENCAO_COM_CARGA", i, prod, f"Isenção com carga_final='{carga}' (esperado 0%/isento)")


# ---- 4) Base legal ausente em benefício ativo ----
def check_base_legal():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        ben = _norm(b.get("beneficio"))
        if any(e in ben for e in ECON) and not (b.get("base_legal") or "").strip():
            alerta("SEM_BASE_LEGAL", i, prod, f"benefício '{b.get('beneficio')}' sem base legal")


# ---- 5) Duplicados exatos (produto+ncm+beneficio) ----
def check_duplicados():
    vis = defaultdict(list)
    for i, b in enumerate(BENEF):
        # inclui base_legal e cfop_tipico: itens com fundamento/CFOP distintos NÃO são duplicata
        k = (_norm(b.get("produto")), _dig(b.get("ncm")), _norm(b.get("beneficio")),
             _norm(b.get("carga_final")), _norm(b.get("base_legal")), _norm(b.get("cfop_tipico_original")))
        vis[k].append(i)
    for k, idxs in vis.items():
        if len(idxs) > 1 and k[0]:
            alerta("DUPLICADO", idxs[0], k[0], f"{len(idxs)} itens realmente idênticos idx={idxs}")


# ---- 6) Revogados/retirados ainda presentes ----
def check_revogados():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        vig = _norm(b.get("vigencia_anexo"))
        campos = _norm(" ".join(str(b.get(k, "")) for k in ("beneficio", "beneficio_resumo", "condicao_resumo", "base_legal")))
        if "revog" in vig or "revog" in campos:
            if "vigente" not in vig:
                erro("REVOGADO_PRESENTE", i, prod, f"marcado como revogado mas ainda na lista (vig={b.get('vigencia_anexo','')})")


# ---- 7) CEST malformado (só quando parece um CEST de verdade) ----
def check_cest():
    for i, b in enumerate(BENEF):
        prod = (b.get("produto") or "").strip()
        cest = (b.get("cest") or "").strip()
        n = _norm(cest)
        if not cest or n.startswith(("n/a", "na", "nao", "não", "-", "sem", "isent")):
            continue
        # aceita 1 CEST ou LISTA separada por ; (cada um NN.NNN.NN); ignora notas "... ver anexo"
        partes = [p.strip() for p in re.split(r"[;,/]", cest) if p.strip()
                  and "..." not in p and "anexo" not in _norm(p) and not _norm(p).startswith("ver")]
        if partes and all(re.match(r"^\d{2}\.?\d{3}\.?\d{2}$", p) for p in partes):
            continue
        if re.search(r"\d", cest):  # tem dígitos mas não bate o formato -> revisar
            alerta("CEST_FORMATO", i, prod, f"CEST '{cest[:30]}' fora do formato NN.NNN.NN")


def run():
    for fn in (check_ncm, check_produto, check_numeros, check_base_legal,
               check_duplicados, check_revogados, check_cest):
        fn()
    print(f"=== AUDITORIA DA BASE ({len(BENEF)} benefícios) ===\n")
    ce = Counter(e[0] for e in erros)
    ca = Counter(a[0] for a in alertas)
    print(f"ERROS graves: {len(erros)}  {dict(ce)}")
    print(f"ALERTAS:      {len(alertas)}  {dict(ca)}\n")
    if erros:
        print("--- ERROS (mostrando até 30) ---")
        for cat, i, prod, msg in erros[:30]:
            print(f"  [{cat}] #{i} {prod[:34]!r}: {msg}")
    if alertas:
        print("\n--- ALERTAS (mostrando até 25) ---")
        for cat, i, prod, msg in alertas[:25]:
            print(f"  [{cat}] #{i} {prod[:30]!r}: {msg}")
    return len(erros)


if __name__ == "__main__":
    sys.exit(1 if run() > 0 else 0)
