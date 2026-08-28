# -*- coding: utf-8 -*-
"""
Assistente Fiscal ICMS/MA - Backend (FastAPI)
- Chave da OpenRouter fica SO no servidor (.env). O frontend NUNCA ve a chave.
- Base de conhecimento local (data/base.json) -> RAG: filtra local, manda so o relevante p/ IA.
- Endpoints: /api/consulta (form), /api/chat (chatbot), /api/perfis (perfis de cliente).
- Da o CFOP EXATO da operacao (nao "5101/5102").
"""
import os, json, re, unicodedata, datetime, threading, tempfile, uuid
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ---- provedor de IA detectado pela chave (gsk_ = Groq, sk-or- = OpenRouter) ----
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
if GROQ_KEY:
    PROVIDER = "Groq"
    AI_KEY = GROQ_KEY
    AI_URL = "https://api.groq.com/openai/v1/chat/completions"
    MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    # modelos descontinuados no Groq -> auto-corrige p/ o atual (evita 404 se env estiver velho)
    if MODEL.strip() in ("llama-3.3-70b-versatile", "llama3-70b-8192", "mixtral-8x7b-32768", "llama-3.1-70b-versatile"):
        MODEL = "openai/gpt-oss-120b"
    FALLBACK_MODEL = os.getenv("GROQ_FALLBACK", "openai/gpt-oss-20b")
elif OPENROUTER_KEY:
    PROVIDER = "OpenRouter"
    AI_KEY = OPENROUTER_KEY
    AI_URL = "https://openrouter.ai/api/v1/chat/completions"
    MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK", "")
else:
    PROVIDER = "nenhum"; AI_KEY = ""; AI_URL = ""; MODEL = ""; FALLBACK_MODEL = ""

app = FastAPI(title="Assistente Fiscal ICMS/MA")

# ---------- carregar base ----------
BASE = json.loads((BASE_DIR / "data" / "base.json").read_text(encoding="utf-8"))
BENEF = BASE.get("beneficios", [])
CFOP_DIC = BASE.get("cfop_dicionario", {}) or {}
# CFOPs padrao de consumidor final que faltam na base (tabela oficial CFOP)
CFOP_DIC.setdefault("5107", {"cfop": "5.107", "natureza": "Saída — dentro do estado", "fluxo": "Saída",
                             "descricao": "Venda de produção do estabelecimento, destinada a não contribuinte"})
CFOP_DIC.setdefault("5108", {"cfop": "5.108", "natureza": "Saída — dentro do estado", "fluxo": "Saída",
                             "descricao": "Venda de mercadoria adquirida ou recebida de terceiros, destinada a não contribuinte"})
ALIQ_MA = 23.0
PERFIS_FILE = BASE_DIR / "perfis.json"

def _sig_cfop(cod):
    """Pega a descricao de um CFOP no dicionario."""
    e = CFOP_DIC.get(cod) or CFOP_DIC.get(cod[0] + "." + cod[1:4])
    if isinstance(e, dict):
        return e.get("descricao", "")
    return e or ""

def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())

def _dig(s):
    return re.sub(r"\D", "", str(s or ""))

# ===== RECURSOS EXTRAS (checklist de elegibilidade + segmentos) =====
# Para DESLIGAR tudo: troque para False (ou apague eligibilidade.py). O frontend some sozinho.
RECURSOS_EXTRAS = True
try:
    import eligibilidade
except Exception:
    RECURSOS_EXTRAS = False
# integração LegisWeb (fonte oficial via API) — EMBUTIDA no app (evita depender de arquivo separado
# que o build do Render pode não subir). Ativa se houver LEGISWEB_TOKEN + LEGISWEB_CLIENTE no ambiente.
import types as _types
_LEGISWEB_IMPORT_ERR = ""
_LW_TOKEN = os.getenv("LEGISWEB_TOKEN", "").strip()
_LW_CLIENTE = os.getenv("LEGISWEB_CLIENTE", "").strip()
_LW_UF = os.getenv("LEGISWEB_UF", "MA").strip() or "MA"
_LW_CERT = os.getenv("LEGISWEB_COD_CERT", "").strip()  # código do certificado A1 (p/ consulta GTIN)
_LW_BASE = "https://www.legisweb.com.br/api"
_LW_CATS = {2: "Redução de BC", 3: "Isenção", 4: "Crédito Presumido/Outorgado", 5: "Diferimento"}
_LW_CACHE = {}   # memória: ckey -> (True, data)
# BASE SEPARADA da LegisWeb: cada consulta com resultado é salva em disco e reusada (não gasta cota de novo).
# Local configurável (p/ disco persistente no Render): env LEGISWEB_CACHE_FILE=/var/data/legisweb_cache.json
_LW_CACHE_FILE = Path(os.getenv("LEGISWEB_CACHE_FILE", "").strip() or (BASE_DIR / "data" / "legisweb_cache.json"))


def _lw_cache_load():
    try:
        raw = json.loads(_LW_CACHE_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            _LW_CACHE[k] = (True, v)
    except Exception:
        pass


def _lw_cache_save():
    try:
        raw = {k: v[1] for k, v in _LW_CACHE.items() if v[0]}
        tmp = _LW_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LW_CACHE_FILE)
    except Exception:
        pass


_lw_cache_load()


def _lw_disponivel():
    return bool(_LW_TOKEN and _LW_CLIENTE)


def _lw_get(endpoint, **params):
    if not _lw_disponivel():
        return False, {"erro": "LegisWeb não configurada (defina LEGISWEB_TOKEN e LEGISWEB_CLIENTE)."}
    params = {k: v for k, v in params.items() if v not in (None, "")}
    ckey = endpoint + "|" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if ckey in _LW_CACHE:
        return _LW_CACHE[ckey]
    params["t"] = _LW_TOKEN
    params["c"] = _LW_CLIENTE
    try:
        r = httpx.get(f"{_LW_BASE}/{endpoint.strip('/')}/", params=params, timeout=25)
        data = r.json()
    except Exception as e:
        return False, {"erro": f"Falha na chamada: {e}"}
    # a LegisWeb devolve erro como lista [{"erro":"..."}] com HTTP 400 (ex.: limite de consumo)
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and data[0].get("erro"):
            return False, {"erro": data[0]["erro"]}
    if isinstance(data, dict):
        msg = str(data.get("mensagem") or data.get("erro") or "")
        if msg and "registros" not in data:
            return False, {"erro": msg}
        # 'resposta' pode ser lista (benefício ICMS), dict (reforma: resposta.ncm[]) ou
        # string ("Nenhum resultado..."). Só a string vira lista vazia.
        if isinstance(data.get("resposta"), str):
            data["resposta"] = []
    if r.status_code >= 400 and not (isinstance(data, dict) and "registros" in data):
        return False, {"erro": f"HTTP {r.status_code}"}
    res = (True, data)
    _LW_CACHE[ckey] = res
    _lw_cache_save()   # persiste na base separada da LegisWeb
    return res


def _lw_beneficios(ncm=None, descricao=None, codigo=None, estado=None, categorias=None):
    estado = (estado or _LW_UF).upper()
    cats = categorias or list(_LW_CATS.keys())
    itens, erros = [], []
    for cat in cats:
        ok, data = _lw_get("beneficio-fiscal", estado=estado, categoria=cat,
                           ncm=ncm, descricao=descricao, codigo=codigo)
        if not ok:
            erros.append({"categoria": _LW_CATS.get(cat, cat), "erro": data.get("erro")})
            continue
        for it in (data.get("resposta") or []):
            it["_categoria_num"] = cat
            it["_categoria"] = _LW_CATS.get(cat, str(cat))
            itens.append(it)
    return {"ok": not (erros and not itens), "uf": estado, "fonte": "LegisWeb (oficial)",
            "total": len(itens), "itens": itens, "erros": erros}


def _lw_reforma(ncm=None, descricao=None):
    """Benefícios da REFORMA TRIBUTÁRIA (IBS/CBS) por NCM (tipo-busca=1) ou descrição (=4)."""
    if ncm:
        ok, data = _lw_get("reforma_tributaria_beneficios", **{"tipo-busca": 1, "ncm": ncm})
    else:
        ok, data = _lw_get("reforma_tributaria_beneficios", **{"tipo-busca": 4, "descricao": descricao})
    if not ok:
        return {"ok": False, "erro": data.get("erro"), "itens": []}
    resp = data.get("resposta")
    itens = []
    if isinstance(resp, dict):
        for key in ("ncm", "nbs", "cnae", "descricao"):
            itens.extend(resp.get(key) or [])
    elif isinstance(resp, list):
        itens = resp
    # achatar p/ exibição (com CST IBS/CBS, cClassTrib, NCM e carga)
    def _limpa(t):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(t or ""))).strip()
    out = []
    for it in itens:
        item = it.get("item", {}) if isinstance(it, dict) else {}
        ben = it.get("beneficio", {}) if isinstance(it, dict) else {}
        red = it.get("reducoes", []) if isinstance(it, dict) else []
        clas = it.get("classificacoes", []) if isinstance(it, dict) else []
        c0 = clas[0] if clas else {}
        # carga/redução: valor% quando percentual == "Sim"
        red_txt = ""
        for r in red:
            v = str(r.get("valor", "") or "").strip()
            if v:
                try:
                    v = ("%g" % float(v))
                except Exception:
                    pass
                red_txt = (v + "%") if str(r.get("percentual", "")).lower() == "sim" else v
                break
        out.append({
            "ncm": _limpa(item.get("codigo", "")),
            "descricao": _limpa(item.get("item_descricao") or item.get("descricao", "")),
            "tipo_beneficio": _limpa(ben.get("tipo_beneficio", "")),
            "reducao": red_txt,
            "cst": _limpa(c0.get("codigo_cst", "")),
            "cst_desc": _limpa(c0.get("descricao_cst", "")),
            "cclasstrib": _limpa(c0.get("codigo_classificacao", "")),
            "cclasstrib_desc": _limpa(c0.get("descricao_classificacao", "")),
            "aplicabilidade": _limpa(item.get("aplicabilidade_descricao") or (it.get("detalhes", {}) or {}).get("aplicabilidade", "")),
            "base_legal": _limpa(item.get("base_legal") or (it.get("detalhes", {}) or {}).get("base_legal", "")),
            "observacao": _limpa(item.get("beneficio_observacao") or ben.get("observacao", "")),
        })
    return {"ok": True, "fonte": "LegisWeb — Reforma Tributária (IBS/CBS)", "total": len(out), "itens": out}


# todos os recursos (endpoints) que a API LegisWeb oferece
_LW_RECURSOS = {
    "beneficio-fiscal", "reforma_tributaria_beneficios", "st-interna", "st-interestadual",
    "aliquota-padrao", "icms", "pauta-fiscal", "ipi", "tipi", "piscofins", "piscofins-importacao",
    "cfop", "cst", "gtin", "correlacao-nbm-ncm-naladi", "ii", "agenda-tributaria",
    "preferencia-tarifaria", "nve", "ptax", "defesa-comercial", "cide-combustivel",
    "tratamento-administrativo-importacao", "tratamento-administrativo-exportacao",
    "produto-ssn", "correlacoes_servicos", "empresas",
}


def _lw_gtin(gtin, cod_cert=None):
    """Resolve GTIN/EAN (código de barras) -> produto/NCM/CEST. Exige certificado A1 (cod_cert)."""
    ok, data = _lw_get("gtin", gtin=gtin, cod_cert=(cod_cert or _LW_CERT or None))
    if not ok:
        return {"ok": False, "erro": data.get("erro")}
    resp = data.get("resposta") or {}
    return {"ok": True, "produto": resp.get("produto", ""), "ncm": resp.get("ncm", ""),
            "cest": resp.get("cest", ""), "gtin": resp.get("gtin", gtin),
            "status": resp.get("motivo_status", ""), "cod_status": resp.get("cod_status", "")}


def _lw_generic(recurso, params):
    """Proxy genérico p/ QUALQUER endpoint da LegisWeb (whitelist). Retorna itens normalizados."""
    if recurso not in _LW_RECURSOS:
        return {"ok": False, "erro": f"Recurso '{recurso}' não disponível."}
    if recurso == "gtin" and _LW_CERT and not params.get("cod_cert"):
        params["cod_cert"] = _LW_CERT
    ok, data = _lw_get(recurso, **params)
    if not ok:
        return {"ok": False, "erro": data.get("erro"), "itens": []}
    resp = data.get("resposta")
    itens = []
    if isinstance(resp, list):
        itens = resp
    elif isinstance(resp, dict):
        listas = [v for v in resp.values() if isinstance(v, list)]
        if listas:
            for v in listas:
                itens.extend(v)
        else:
            itens = [resp]
    return {"ok": True, "recurso": recurso, "registros": data.get("registros", len(itens)), "itens": itens}


legisweb = _types.SimpleNamespace(TOKEN=_LW_TOKEN, CLIENTE=_LW_CLIENTE, UF_PADRAO=_LW_UF, CERT=_LW_CERT,
                                  disponivel=_lw_disponivel, beneficios=_lw_beneficios, reforma=_lw_reforma,
                                  gtin=_lw_gtin, generico=_lw_generic, recursos=sorted(_LW_RECURSOS))
# auditor de NCM (opcional; carrega tabela oficial + TIPI na inicializacao)
import sys as _sys
_sys.path.insert(0, str(BASE_DIR / "data"))
try:
    import auditoria_ncm as _audit
except Exception as _e:
    _audit = None
# ====================================================================

# indice de busca
IDX = []
POR_NCM_APP = {}   # ncm(8 díg) -> lista de benefícios da base (p/ auditoria com contexto)
for b in BENEF:
    IDX.append({
        "prod_n": _norm(b.get("produto", "")),
        "desc_n": _norm(b.get("ncm_descricao", "")),
        "ncm_d": _dig(b.get("ncm", "")),
        "b": b,
    })
    _c = _dig(b.get("ncm", ""))
    if len(_c) == 8:
        POR_NCM_APP.setdefault(_c, []).append(b)

# ---------- perfis (persistencia em arquivo) ----------
def load_perfis():
    if PERFIS_FILE.exists():
        try:
            return json.loads(PERFIS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    seed = (BASE.get("perfis_cliente", {}) or {}).get("cadastrados", []) or []
    return seed

def save_perfis(lst):
    PERFIS_FILE.write_text(json.dumps(lst, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------- busca (entende o termo: NCM, sinonimo, substring e fuzzy por trigramas) ----------
SINONIMOS = {
    "flocao": "farinha milho flocada flocos milho", "flocão": "farinha milho flocada flocos",
    "refri": "refrigerante", "refris": "refrigerante", "cerveja": "cerveja chope",
    "remedio": "medicamento", "remedios": "medicamentos", "agua": "agua mineral",
    "racao": "racao alimento animal pet", "pneu": "pneumatico", "celular": "aparelho celular telefone",
    "absorvente": "absorvente higienico", "creme dental": "creme dental dentifricio",
    "tijolo": "tijolo ceramica vermelha", "telha": "telha ceramica",
    "gado": "gado bovino vacum reprodutor matriz animal vivo",
    "boi": "gado bovino vacum reprodutor matriz", "vaca": "gado bovino matriz reprodutor",
    "bezerro": "gado bovino reprodutor matriz bezerro", "bovino": "gado bovino vacum reprodutor matriz",
    "reprodutor": "reprodutor matriz puro origem cruza gado bovino ovino suino bufalino animal vivo",
    "reprodutores": "reprodutor matriz puro origem cruza gado bovino ovino suino bufalino animal vivo",
    "matriz": "matriz reprodutor puro origem cruza gado bovino ovino suino bufalino",
    "matrizes": "matriz reprodutor puro origem cruza gado bovino ovino suino bufalino",
    "novilha": "matriz reprodutor gado bovino", "touro": "reprodutor gado bovino vacum",
}
def _expand(qn):
    extra = []
    for k, v in SINONIMOS.items():
        if k in qn:
            extra.append(v)
    return (qn + " " + " ".join(extra)).strip()

def _trigrams(s):
    s = " " + s + " "
    return {s[i:i+3] for i in range(len(s) - 2)}

def buscar(query, limite=8):
    q = (query or "").strip()
    qd = _dig(q)
    qn = _expand(_norm(q))
    # consulta é um NCM puro? (só dígitos/pontos/barra/traço) -> NÃO cair na busca textual
    pure_ncm = len(qd) >= 4 and not re.sub(r"[\d.\s/\-]", "", q)
    res = []
    if len(qd) >= 4:  # por NCM (8 dígitos exatos > prefixo > NCM contendo a busca)
        for it in IDX:
            nd = it["ncm_d"]
            if nd and (nd.startswith(qd) or qd.startswith(nd)):
                if nd == qd:
                    score = 200
                elif nd.startswith(qd):
                    score = 150 - min(80, (len(nd) - len(qd)) * 12)
                else:
                    score = 60
                res.append((score, it["b"]))
        res.sort(key=lambda x: -x[0])
        # fallback por POSIÇÃO (4 dígitos) — ex.: 1006.10.10 -> família 1006 (arroz)
        if not res and len(qd) >= 4:
            pref = qd[:4]
            for it in IDX:
                nd = it["ncm_d"]
                if nd and nd[:4] == pref:
                    res.append((40, it["b"]))
    if not res and not pure_ncm and len(qn) >= 3:  # por descricao (token + palavra inteira + relevancia)
        qnorm = _norm(q)
        toks = [t for t in qn.split() if len(t) > 2]        # expandido (com sinonimos)
        otoks = [t for t in qnorm.split() if len(t) > 2]    # termos originais do usuario
        for it in IDX:
            prod = it["prod_n"]
            desc = it["desc_n"]
            hay = prod + " " + desc
            score = 0
            for t in toks:
                if re.search(r"\b" + re.escape(t) + r"\b", hay):
                    score += 3 + (2 if re.search(r"\b" + re.escape(t) + r"\b", prod) else 0)
                elif t in hay:
                    score += 1
            if qnorm and qnorm in hay:
                score += 6
            if qnorm and qnorm in prod:
                score += 4
            # nome do produto começa com o 1º termo da busca
            first = prod.split()[0] if prod else ""
            if otoks and first == otoks[0]:
                score += 5
            elif otoks and first.startswith(otoks[0]):
                score += 2
            # busca multi-palavra: TODOS os termos devem aparecer (AND) — forte sinal
            if otoks and all(o in hay for o in otoks):
                score += 4
            if score:
                res.append((score, it["b"]))
        res.sort(key=lambda x: -x[0])
    if not res and not pure_ncm and len(qn) >= 3:  # FUZZY por trigramas (tolera erro de digitacao)
        qt = _trigrams(_norm(q))
        cand = []
        for it in IDX:
            ht = _trigrams(it["prod_n"]) | _trigrams(it["desc_n"])
            if not ht:
                continue
            sim = len(qt & ht) / max(1, len(qt))
            if sim >= 0.34:
                cand.append((sim, it["b"]))
        cand.sort(key=lambda x: -x[0])
        res = cand
    # dedup por (produto, ncm, beneficio)
    seen, out = set(), []
    for _, b in res:
        k = (b.get("produto", ""), b.get("ncm", ""), b.get("beneficio", ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
        if len(out) >= limite:
            break
    return out

# ---------- CFOP exato ----------
def _pick(cands, prefer):
    """cands: lista de {cfop,significado,confirmado_na_tabela}; prefer: lista de sufixos preferidos."""
    if not cands:
        return None
    norm = {c["cfop"].replace(".", ""): c for c in cands}
    for suf in prefer:
        if suf in norm:
            return norm[suf]
    # senao, o primeiro confirmado
    for c in cands:
        if c.get("confirmado_na_tabela"):
            return c
    return cands[0]

def cfop_exato(benef, etapa, destino):
    """Retorna {cfop, significado} exato para SAIDA do cliente."""
    cpo = benef.get("cfop_por_operacao", {}) or {}
    
    # Mapeamento de destinos para chaves do CFOP
    mapa_chave = {
        "interna": "saida_interna",
        "interna_consumidor": "saida_interna",
        "inter": "saida_interestadual",
        "consumidor": "saida_interestadual",
        "rural_interna": "saida_interna",
        "rural_cooperativa": "saida_interestadual",
        "rural_interestadual": "saida_interestadual",
    }
    
    interna = destino in ("interna", "interna_consumidor", "rural_interna")
    chave = mapa_chave.get(destino, "saida_interna")
    cands = cpo.get(chave, []) or []
    
    tipo = _norm(benef.get("beneficio", ""))
    revenda = etapa in ("varejo", "atacado")
    industria = etapa == "industria"
    produtor = etapa == "produtor"
    
    # define sufixo preferido (5xxx interna / 6xxx interest.)
    p = "5" if interna else "6"
    prefer = []
    
    # Operações rurais específicas
    if destino == "rural_cooperativa":
        # Remessa para cooperativa/industrialização
        prefer = [p + "101", p + "102", p + "111", p + "112"]
    elif destino == "rural_interestadual":
        # Saída interestadual da produção
        prefer = [p + "101", p + "102"]
    elif destino == "interna_consumidor":
        # Venda interna para consumidor final não contribuinte
        prefer = [p + "108", p + "107"]
    elif destino == "consumidor":
        # consumidor final nao contribuinte interestadual
        prefer = [p + "108", p + "107"]
    elif "substitu" in tipo:
        if revenda:
            prefer = [p + "405", p + "409"]          # ICMS ja retido por ST
        else:
            prefer = [p + "401", p + "402", p + "403"]  # industria/importador retem ST
    elif produtor or industria:
        prefer = [p + "101", p + "151"]              # producao propria
    else:
        prefer = [p + "102", p + "101"]              # revenda mercadoria
    escolhido = _pick(cands, prefer)
    # se a base nao trouxe o CFOP desejado (ex.: consumidor), usa o padrao da tabela
    if not escolhido:
        cod = (prefer[0] if prefer else (p + "102"))
        escolhido = {"cfop": cod[0] + "." + cod[1:], "significado": _sig_cfop(cod),
                     "confirmado_na_tabela": bool(_sig_cfop(cod))}
    elif destino in ("consumidor", "interna_consumidor") and prefer:
        cod_pref = prefer[0]
        if _sig_cfop(cod_pref) and cod_pref not in {c["cfop"].replace(".", "") for c in cands}:
            escolhido = {"cfop": cod_pref[0] + "." + cod_pref[1:], "significado": _sig_cfop(cod_pref),
                         "confirmado_na_tabela": True}
    # alternativas (todas do lado escolhido)
    return escolhido, cands

# ---------- montar resultado fiscal deterministico ----------
REGIMES = {
    "lucro_real":      {"categoria": "normal",  "rotulo": "Lucro Real"},
    "lucro_presumido": {"categoria": "normal",  "rotulo": "Lucro Presumido"},
    "normal":          {"categoria": "normal",  "rotulo": "Lucro Real / Presumido"},
    "simples":         {"categoria": "simples", "rotulo": "Simples Nacional"},
    "mei":             {"categoria": "simples", "rotulo": "MEI"},
}

def _categoria_regime(regime):
    """normal (lucro real/presumido) ou simples (simples/mei)."""
    return REGIMES.get(regime, {}).get("categoria", "normal")

def _rotulo_regime(regime):
    return REGIMES.get(regime, {}).get("rotulo", regime)

# ============ ALGORITMO DE SELEÇÃO DO CST/CSOSN ============
# Classifica o produto no CONTEXTO do cliente (tipo de benefício + etapa + regime):
# regime normal (lucro real/presumido) -> CST ICMS; Simples -> CSOSN.
# Usa a base como fonte curada; quando ela traz notação combinada
# ('60 (revenda) / 10 (indústria)', '500 / 201-202'), resolve pela etapa.
_DESC_CST = {
    "00": "tributada integralmente",
    "10": "tributada e com cobrança do ICMS por substituição tributária",
    "20": "redução de base de cálculo",
    "30": "isenta/não tributada e com cobrança do ICMS por ST",
    "40": "isenta",
    "41": "não tributada / imune",
    "51": "diferimento",
    "60": "ICMS já retido por substituição tributária (revenda)",
    "70": "com redução de BC e cobrança do ICMS por ST",
}

def _tipo_beneficio(tipo):
    t = _norm(tipo)
    return {
        "st":  "substitui" in t,
        "red": "reducao" in t,
        "isc": "isenc" in t,
        "dif": "diferi" in t,
        "imn": any(x in t for x in ("imunidade", "imune", "nao tribut", "nao incid")),
        "cred": ("credito presumido" in t or "credito outorgado" in t),
        "mnc": ("manutencao de credito" in t or "manutencao do credito" in t),
    }

def _resolve_cst_sugerido(texto, etapa):
    """Entende formatos da base tipo '60 (revenda) / 10 (indústria)' -> CST da etapa."""
    t = str(texto or "").strip()
    if not t:
        return ""
    revenda = etapa in ("varejo", "atacado")
    origem = etapa in ("industria", "produtor")
    if "/" in t:
        for part in t.split("/"):
            m = re.search(r"(\d{2})", part)
            if not m:
                continue
            rot = _norm(part)
            if revenda and any(k in rot for k in ("revenda", "varejo", "atacado")):
                return m.group(1)
            if origem and any(k in rot for k in ("industria", "producao", "fabrica", "importa", "origem", "retem")):
                return m.group(1)
        m = re.search(r"(\d{2})", t)
        return m.group(1) if m else ""
    m = re.search(r"(\d{2})", t)
    return m.group(1) if m else ""

def _resolve_csosn_sugerido(texto, etapa):
    """Entende formatos da base tipo '500 / 201-202' ou '103/300/400'."""
    t = str(texto or "").strip()
    if not t:
        return ""
    revenda = etapa in ("varejo", "atacado")
    origem = etapa in ("industria", "produtor")
    if "/" in t:
        for part in t.split("/"):
            if revenda and re.search(r"\b500\b", part):
                return "500"
            if origem and re.search(r"20[1-3]", part):
                return "201/202"
        m = re.search(r"\d{3}", t)
        return m.group(0) if m else ""
    cods = re.findall(r"\d{3}", t)
    return "/".join(cods[:2]) if cods else ""

def seleciona_trib(benef, etapa, regime):
    """Devolve (codigo, campo, descricao) — o CST/CSOSN CORRETO para o contexto."""
    tp = _tipo_beneficio(benef.get("beneficio", ""))
    revenda = etapa in ("varejo", "atacado")

    if _categoria_regime(regime) == "simples":
        if tp["st"]:
            cod, obs = ("500", "ICMS já retido por ST") if revenda else ("201/202", "com cobrança do ICMS por ST")
        elif tp["imn"]:
            cod, obs = "300", "imune"
        elif tp["isc"]:
            cod, obs = "103/300/400", "isenção na faixa do Simples"
        elif tp["red"] or tp["dif"]:
            cod, obs = "102", "no Simples o benefício estadual não se aplica"
        else:
            cod = _resolve_csosn_sugerido(benef.get("csosn_sugerido"), etapa) or "102"
            obs = ""
        return cod, "CSOSN", obs

    # regime normal (lucro real / presumido) -> CST ICMS (base é fonte curada)
    cod = _resolve_cst_sugerido(benef.get("cst_icms_sugerido"), etapa)
    if not cod:  # base não traz -> classifica pelas regras
        if tp["st"]:
            cod = "60" if revenda else "10"
        elif tp["red"]:
            cod = "20"
        elif tp["isc"]:
            cod = "40"
        elif tp["dif"]:
            cod = "51"
        elif tp["imn"]:
            cod = "41"
        else:
            cod = "00"
    return cod, "CST ICMS", _DESC_CST.get(cod, "")

def _como_funciona(b, etapa, operacao, regime):
    """Monta o bloco 'COMO FUNCIONA / requisitos p/ ter o benefício' — simples e organizado.
    Retorna lista de {t, d} (d pode ser texto ou lista). Sempre inclui Base legal."""
    tp = _tipo_beneficio(b.get("beneficio", ""))
    resumo = (b.get("beneficio_resumo") or b.get("beneficio", "") or "").strip()
    cat = _categoria_regime(regime)
    revenda = etapa in ("varejo", "atacado")
    blocos = []

    # 1) COMO FUNCIONA (mecanismo)
    if tp["st"]:
        if revenda:
            mech = ("O ICMS desta mercadoria já foi retido na origem (substituição tributária). "
                    "Na sua venda NÃO destaque/recolha ICMS: use CST 60 (ou CSOSN 500 no Simples).")
        else:
            mech = ("Você é o responsável pela retenção do ICMS-ST: destaca o ICMS próprio (CST 10 / CSOSN 201-202) "
                    "e retém o ICMS-ST sobre a pauta, informando o CEST correto na nota.")
    elif tp["red"]:
        if cat == "simples":
            mech = ("A redução de base é benefício de ICMS estadual e NÃO se aplica ao Simples Nacional — "
                    "a operação é tributada normalmente pelo Simples (CSOSN 102).")
        else:
            redu = b.get("reducao_bc", "")
            carga = b.get("carga_final", "")
            mech = ("O ICMS é calculado sobre uma base de cálculo REDUZIDA"
                    + (f" de {redu}" if redu else "")
                    + " — em vez dos 23% cheios"
                    + (f", carga efetiva de {carga}" if carga else "") + ".")
    elif tp["isc"]:
        mech = ("A operação é ISENTA de ICMS (CST 40) — o imposto não é destacado na nota."
                if cat != "simples"
                else "Isenção de ICMS estadual: no Simples o enquadramento segue o CSOSN do produto (103/300/400).")
    elif tp["dif"]:
        mech = ("O ICMS é DIFERIDO (CST 51): o lançamento do imposto fica transferido para uma etapa futura da cadeia "
                "— você não recolhe agora."
                if cat != "simples"
                else "Diferimento é benefício de ICMS estadual; no Simples não se aplica (CSOSN 102).")
    elif tp["imn"]:
        mech = "A operação é NÃO TRIBUTADA / IMUNE (CST 41 / CSOSN 300) — não há ICMS a recolher."
    elif tp["cred"]:
        carga = b.get("carga_final", "")
        mech = ("Você apura o ICMS com CRÉDITO PRESUMIDO/OUTORGADO: em vez do crédito normal das entradas, "
                "usa um crédito fixado pela lei, o que reduz a carga a recolher"
                + (f" ({carga})" if carga else "") + ". Em regra é OPCIONAL e VEDA o aproveitamento de outros créditos "
                "— compare com a tributação normal antes de optar.")
    elif tp["mnc"]:
        mech = ("Benefício de MANUTENÇÃO DE CRÉDITO: você NÃO precisa estornar o crédito da entrada, mesmo quando a "
                "saída é isenta ou com base reduzida. O crédito é mantido, aumentando o saldo aproveitável.")
    else:
        mech = f"Enquadramento conforme o benefício {resumo}."
    blocos.append({"t": "Como funciona", "d": f"{resumo}. {mech}"})

    # 2) REQUISITOS / O QUE CUMPRIR
    cond = (b.get("condicao_texto_integral") or b.get("condicao_resumo") or "").strip()
    req = [p.strip() for p in re.split(r"[;\n]", cond) if p.strip()] if cond else []
    if cat == "simples" and (tp["red"] or tp["isc"] or tp["dif"]):
        req.insert(0, "Benefício de ICMS estadual NÃO se aplica ao Simples Nacional — enquadre como CSOSN 102.")
    if etapa == "produtor" and operacao.startswith("rural_"):
        req.append("Produtor rural: emitir a Nota Fiscal do Produtor com o enquadramento indicado (ver CFOP).")
    if req:
        blocos.append({"t": "Requisitos / o que cumprir", "d": req})

    # 3) NÚMEROS DO ENQUADRAMENTO
    nums = []
    trib, campo, obs = seleciona_trib(b, etapa, regime)
    nums.append(f"{campo}: {trib}" + (f" — {obs}" if obs else ""))
    if b.get("reducao_bc"):
        nums.append(f"Redução de BC: {b['reducao_bc']}")
    if b.get("carga_final"):
        nums.append(f"Carga efetiva: {b['carga_final']}")
    if nums:
        blocos.append({"t": "Números do enquadramento", "d": nums})

    # 4) BASE LEGAL (sempre) + fonte
    legal = (b.get("base_legal") or "").strip() or "Confirmar na norma vigente do ICMS/MA."
    fonte = (b.get("fonte_texto_integral") or "").strip()
    blocos.append({"t": "Base legal", "d": legal + (f"  |  Fonte: {fonte}" if fonte else "")})
    return blocos

_MAPA_OP = {"interna": "interna", "interna_consumidor": "interna_consumidor", "inter": "inter",
            "consumidor": "consumidor", "rural_interna": "rural_interna",
            "rural_cooperativa": "rural_cooperativa", "rural_interestadual": "rural_interestadual"}

def montar_item(b, etapa, operacao, regime):
    """Enquadra 1 benefício da base no CONTEXTO do cliente (etapa+operação+regime):
    CFOP exato + CST/CSOSN + carga + checklist. Reutilizado pela Consulta e pela Auditoria."""
    destino = _MAPA_OP.get(operacao, "interna")
    if operacao.startswith("rural_"):
        etapa = "produtor"
    esc, cands = cfop_exato(b, etapa, destino)
    trib, campo, trib_obs = seleciona_trib(b, etapa, regime)
    item = {
        "produto": b.get("produto", ""), "ncm": b.get("ncm", ""), "cest": b.get("cest", ""),
        "beneficio": b.get("beneficio", ""), "beneficio_resumo": b.get("beneficio_resumo", ""),
        "regime_rotulo": _rotulo_regime(regime), "campo_trib": campo, "trib": trib, "trib_obs": trib_obs,
        "reducao_bc": b.get("reducao_bc", ""), "carga_final": b.get("carga_final", ""),
        "cst_piscofins": b.get("cst_piscofins", ""),
        "cfop_exato": esc["cfop"] if esc else "", "cfop_significado": esc.get("significado", "") if esc else "",
        "cfop_confirmado": esc.get("confirmado_na_tabela", False) if esc else False,
        "cfop_alternativas": [{"cfop": c["cfop"], "significado": c.get("significado", "")} for c in cands[:6]],
        "base_legal": b.get("base_legal", ""),
        "condicao": b.get("condicao_texto_integral") or b.get("condicao_resumo", ""),
        "condicao_completa": b.get("condicao_texto_integral", ""), "fonte": b.get("fonte_texto_integral", ""),
        "como_funciona": _como_funciona(b, etapa, operacao, regime),
    }
    if etapa == "produtor" and operacao in ("interna", "rural_interna"):
        item["obs_produtor"] = "Produtor rural - saida interna normalmente com DIFERIMENTO (CST 51); emite Nota Fiscal do Produtor sem destaque."
    elif etapa == "produtor" and operacao == "rural_cooperativa":
        item["obs_produtor"] = "Produtor rural - remessa para cooperativa ou industrialização. CFOP 5.101/6.101 (industrialização) ou 5.102/6.102 (comercialização)."
    elif etapa == "produtor" and operacao == "rural_interestadual":
        item["obs_produtor"] = "Produtor rural - saída interestadual. CFOP 6.101 (produção própria) ou 6.102 (revenda). Verificar Acordo ICMS 142/18."
    # escopo de operação: alguns benefícios só valem numa operação específica
    esc = (b.get("escopo_operacao") or "").lower()
    if not esc:
        _t = _norm(" ".join(str(b.get(k, "")) for k in ("beneficio_resumo", "condicao_resumo", "condicao_texto_integral")))
        if "interestadu" in _t and "interna" not in _t and "cesta basica" not in _t:
            esc = "interestadual"
        elif ("cesta basica" in _t or " interna" in _t) and "interestadu" not in _t and "reduc" in _norm(b.get("beneficio", "")):
            esc = "interna"
    fam = "inter" if operacao in ("inter", "consumidor", "rural_interestadual", "rural_cooperativa") else "interna"
    if esc == "interestadual" and fam == "interna":
        item["aviso_operacao"] = "Este benefício vale para SAÍDA INTERESTADUAL. Na operação selecionada (interna) ele NÃO se aplica — tribute normalmente (CST 00 / CSOSN 102 no Simples)."
        item["nao_aplica"] = True
    elif esc == "interna" and fam == "inter":
        item["aviso_operacao"] = "Este benefício vale para operação INTERNA (dentro do MA). Na operação interestadual selecionada ele NÃO se aplica dessa forma."
        item["nao_aplica"] = True
    # escopo por ETAPA/contribuinte: benefício só de produtor NÃO vale p/ empresa (varejo/atacado), etc.
    _te = _norm(" ".join(str(b.get(k, "")) for k in ("produto", "beneficio_resumo", "condicao_resumo", "condicao_texto_integral", "base_legal")))
    etp = set()
    _ee = b.get("escopo_etapa")
    if _ee == "todos":
        etp = set()          # explicitamente SEM restrição de etapa — não re-derivar
    elif _ee:
        etp = set(str(_ee).split("|"))
    else:
        _tep = " " + _te + " "
        # guarda: "EXCETO ... produtores" / "promovidos por produtores" é cláusula de EXCLUSÃO —
        # significa que o benefício NÃO vai ao produtor (logo vale p/ empresa). Não marcar escopo produtor.
        _excl_prod = ("promovidos por produtores" in _te) or ("exceto" in _te and "produtores" in _te)
        if (not _excl_prod and (
                any(k in _te for k in ("produtor rural", "produtor rudimentar", "produtores agropecuar", "por produtores", "pelo produtor", " do produtor", "agricultor familiar", "pronaf", "carcinicultura", "capturados", "pescador"))
                or "cnae 01" in _te or "cnae 0154" in _te or " cae 1 " in _tep)):
            etp.add("produtor")
        if any(k in _te for k in ("atacadista credenciado", "atacadistas de graos", "atacadistas de alimentos", "por atacadistas")):
            etp.add("atacado")
        if (any(k in _te for k in ("estabelecimento industrial", "industria de ", "frigorifico", "industrial credenciado", "ceramista", "1a operacao industrial", "1 operacao industrial", "envasadores"))
                or "cnae 31" in _te or "cnae 3101" in _te or " cae 3 " in _tep):
            etp.add("industria")
    item["quem_pode"] = "|".join(sorted(etp)) if etp else "todos"
    if etp and etapa not in etp and not item.get("nao_aplica"):
        rot = {"produtor": "Produtor Rural", "industria": "Indústria/Importador", "atacado": "Atacado (credenciado)", "varejo": "Varejo"}
        alvo = " ou ".join(rot.get(x, x) for x in sorted(etp))
        item["aviso_operacao"] = (f"Este benefício é específico de {alvo}. Na etapa selecionada "
                                  f"({rot.get(etapa, etapa)}) ele NÃO se aplica — tribute normalmente (CST 00 / CSOSN 102 no Simples).")
        item["nao_aplica"] = True
    if RECURSOS_EXTRAS:
        try:
            item["elegibilidade"] = eligibilidade.gerar_checklist(item, etapa, operacao, regime, _categoria_regime(regime))
        except Exception:
            pass
    return item

_ROTULO_OP = {
    "interna": "Venda interna p/ empresa (contribuinte)",
    "interna_consumidor": "Venda interna p/ consumidor final",
    "inter": "Interestadual p/ empresa (contribuinte)",
    "consumidor": "Interestadual p/ consumidor final não contribuinte",
    "rural_interna": "Saída interna da produção agropecuária",
    "rural_cooperativa": "Remessa p/ cooperativa / industrialização",
    "rural_interestadual": "Saída interestadual da produção",
}

def montar_multi(b, etapa, operacoes, regime):
    """Monta 1 item com o CFOP/enquadramento de CADA operação selecionada (multi-operação)."""
    ops = list(operacoes) if operacoes else ["interna"]
    base = montar_item(b, etapa, ops[0], regime)
    per_op = []
    for op in ops:
        m = montar_item(b, etapa, op, regime)
        per_op.append({
            "operacao": op,
            "rotulo": _ROTULO_OP.get(op, op),
            "cfop_exato": m.get("cfop_exato", ""),
            "cfop_significado": m.get("cfop_significado", ""),
            "cfop_confirmado": m.get("cfop_confirmado", False),
            "nao_aplica": m.get("nao_aplica", False),
            "aviso_operacao": m.get("aviso_operacao", ""),
            "obs_produtor": m.get("obs_produtor", ""),
        })
    base["operacoes"] = per_op
    base["nao_aplica"] = all(o["nao_aplica"] for o in per_op)
    return base

def resolver(produto, etapa, operacao, regime, perfil=None, operacoes=None):
    # operações selecionadas (multi-operação); mantém compat. com chamadas antigas
    ops = list(operacoes) if operacoes else [operacao]
    destino = _MAPA_OP.get(ops[0], "interna")
    
    # Para operações rurais, forçar etapa como produtor
    if any(o.startswith("rural_") for o in ops):
        etapa = "produtor"
    
    matches = buscar(produto)
    out = {
        "encontrou": bool(matches),
        "consulta": {"produto": produto, "etapa": etapa, "operacao": ops[0], "operacoes": ops, "regime": regime,
                     "regime_rotulo": _rotulo_regime(regime)},
        "itens": [],
    }
    if not matches:
        # Mapeamento de CFOP fallback para cada tipo de operação
        cfop_fallback = {
            "interna": "5.102",
            "interna_consumidor": "5.108",
            "inter": "6.102",
            "consumidor": "6.108",
            "rural_interna": "5.101",
            "rural_cooperativa": "6.101",
            "rural_interestadual": "6.101",
        }
        if _categoria_regime(regime) == "simples":
            icms = "CSOSN 102 - tributada pelo Simples (sem beneficio)"
        else:
            icms = "CST 00 - tributada integralmente (aliquota interna 23%)"
        out["fallback"] = {
            "titulo": "SEM BENEFICIO encontrado na base",
            "icms": icms,
            "cfop": cfop_fallback.get(destino, "5.102"),
            "federal": "PIS/COFINS regra geral (CST 01)" + ("; IPI conforme TIPI" if etapa == "industria" else ""),
            "obs": "Produto nao consta nas listas de beneficio do ICMS/MA. Confirmar na norma.",
        }
        return out
    for b in matches[:6]:
        out["itens"].append(montar_multi(b, etapa, ops, regime))
    out["itens"].sort(key=lambda x: 1 if x.get("nao_aplica") else 0)  # aplicáveis primeiro
    return out


def _ncm_desc_local(cod_ncm):
    """Descrição OFICIAL do NCM a partir da própria base (prefixo 8>6>4 díg). '' se não achar."""
    if not cod_ncm or len(cod_ncm) < 4:
        return ""
    for L in (8, 6, 4):
        if len(cod_ncm) < L:
            continue
        pref = cod_ncm[:L]
        for it in IDX:
            nd = it.get("ncm_d") or ""
            if len(nd) >= L and nd[:L] == pref:
                d = (it["b"].get("ncm_descricao") or "").strip()
                if d:
                    return d
    return ""


async def _ncm_desc_oficial(cod_ncm, produto):
    """Descrição oficial do NCM: 1º base local; 2º Reforma/LegisWeb (cacheado, não gasta em repetição).
    Retorna (descricao, fonte). Serve p/ ATERRAR a IA e não deixar chutar o produto."""
    d = _ncm_desc_local(cod_ncm)
    if d:
        return d, "base"
    if cod_ncm and len(cod_ncm) >= 6 and legisweb and legisweb.disponivel():
        try:
            ncm6 = cod_ncm[:6]
            ncmfmt = ncm6[:4] + "." + ncm6[4:6]
            rf = legisweb.reforma(ncm=ncmfmt)
            for it in (rf.get("itens") or []):
                dd = (it.get("descricao") or "").strip().lstrip("-").strip()
                if dd:
                    return dd, "LegisWeb"
        except Exception:
            pass
    return "", ""

# ---------- OpenRouter ----------
def _resposta_valida(texto):
    """Detecta respostas ruins (thinking traces, safety lixo, vazias)."""
    if not texto or not texto.strip():
        return False
    t = texto.strip()
    if len(t) < 4:
        return False
    baixo = t.lower()
    # traces de raciocinio / safety hallucination
    if baixo.startswith(("okay, ", "ok, ", "the user ", "the assistant ", "first, ",
                         "user safety", "response safety", "the client ", "so the ")):
        return False
    if baixo.count(" ") < 2 and len(t) < 20:
        return False
    return True

async def _chamar_modelo(modelo, messages, temperature=0.1, max_tokens=900):
    """Chama um modelo no provedor configurado (Groq ou OpenRouter — API compativel)."""
    headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json",
               "HTTP-Referer": "http://localhost", "X-Title": "Assistente Fiscal ICMS-MA"}
    payload = {"model": modelo, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.post(AI_URL, headers=headers, json=payload)
    if r.status_code == 429:
        raise Exception("RATE_LIMIT")
    if r.status_code != 200:
        return None, f"{PROVIDER} {r.status_code}: {r.text[:200]}"
    data = r.json()
    conteudo = data["choices"][0]["message"].get("content", "")
    # se o modelo retornou so pensamento, tenta extrair a parte util
    if not _resposta_valida(conteudo) and data["choices"][0]["message"].get("reasoning"):
        conteudo = data["choices"][0]["message"]["reasoning"]
    if not _resposta_valida(conteudo):
        return None, "Resposta vazia ou invalida do modelo."
    return conteudo, None

async def chamar_ia(messages, temperature=0.1, max_tokens=900):
    if not AI_KEY:
        return None, "Nenhuma chave de IA configurada no servidor (.env)."
    modelos = [MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL != MODEL:
        modelos.append(FALLBACK_MODEL)
    ultimo_erro = None
    for modelo in modelos:
        for tentativa in range(2):
            try:
                resultado = await _chamar_modelo(modelo, messages, temperature, max_tokens)
                if resultado[0]:  # sucesso
                    return resultado
                ultimo_erro = resultado[1]
            except Exception as e:
                ultimo_erro = str(e)
                if "RATE_LIMIT" in str(e):
                    import asyncio
                    await asyncio.sleep(3)
                    continue
                break
    return None, ultimo_erro or "Modelos IA indisponiveis no momento."

SYS_PROMPT = """Voce e um assistente tecnico de tributacao fiscal (ICMS) do estado do Maranhao, Brasil.

Funcao: explicar tributacao de produtos (CFOP, CST/CSOSN, reducao de base de calculo, carga tributaria, base legal).

REGRAS:
- Responda SOMENTE com base nos DADOS fornecidos no contexto.
- NUNCA invente NCM, CFOP, CST, aliquota, reducao ou base legal.
- Se a informacao nao estiver nos dados, diga que nao consta e oriente confirmar com contador.
- Aliquota interna geral do MA = 23%.
- No Simples Nacional, beneficios estaduais de reducao/isencao em regra NAO se aplicam (usa CSOSN 102); excecao e ST (CSOSN 500).
- Sempre informe o CFOP EXATO da operacao, com o significado.
- EXPLIQUE os REQUISITOS/CONDICOES para ter direito ao beneficio (quem pode usar, o que deve ser cumprido, base legal). Use o campo 'condicao', 'base_legal' e 'fonte' dos dados.
- ESCOPO DE OPERACAO: se um item tiver o campo 'aviso_operacao', o beneficio NAO se aplica a operacao escolhida pelo cliente — deixe isso EXPLICITO ("nesta operacao NAO se aplica; tribute normalmente") e NAO o recomende como valido. Ex.: reducao de 60% de insumos agropecuarios so vale em saida INTERESTADUAL, entao em venda INTERNA (varejo) NAO se aplica.
- Seja objetivo e direto. Portugues do Brasil."""

# ---------- modelos ----------
class ConsultaIn(BaseModel):
    produto: str
    etapa: str = "varejo"
    operacao: str = "interna"
    operacoes: list = []
    regime: str = "lucro_presumido"
    perfil_id: str | None = None
    explicar_ia: bool = True

class ChatIn(BaseModel):
    mensagem: str
    historico: list = []
    perfil_id: str | None = None

class Perfil(BaseModel):
    id: str | None = None
    nome: str = ""
    tipo_contribuinte: str = ""   # etapa
    regime: str = ""
    ramo: str = ""
    operacoes_habituais: str = ""
    perguntar: list = []          # campos que a IA DEVE perguntar (o resto vem do perfil)
    observacoes: str = ""

# ---------- endpoints ----------
@app.get("/api/perfis")
def get_perfis():
    return load_perfis()

@app.post("/api/perfis")
def post_perfil(p: Perfil):
    lst = load_perfis()
    d = p.dict()
    if not d.get("id"):
        d["id"] = "cli_" + re.sub(r"\W", "", _norm(d.get("nome", "novo")))[:20] + str(len(lst) + 1)
    lst = [x for x in lst if x.get("id") != d["id"]]
    lst.append(d)
    save_perfis(lst)
    return d

@app.delete("/api/perfis/{pid}")
def del_perfil(pid: str):
    lst = [x for x in load_perfis() if x.get("id") != pid]
    save_perfis(lst)
    return {"ok": True}

def _perfil(pid):
    if not pid:
        return None
    for p in load_perfis():
        if p.get("id") == pid:
            return p
    return None

@app.post("/api/consulta")
async def consulta(inp: ConsultaIn):
    perfil = _perfil(inp.perfil_id)
    etapa = inp.etapa; regime = inp.regime
    if perfil:  # perfil pre-preenche
        etapa = perfil.get("tipo_contribuinte") or etapa
        regime = perfil.get("regime") or regime
    ops = list(inp.operacoes) if inp.operacoes else [inp.operacao]
    res = resolver(inp.produto, etapa, inp.operacao, regime, perfil, operacoes=ops)
    if inp.explicar_ia and res.get("encontrou"):
        ctx = json.dumps(res["itens"][:6], ensure_ascii=False)
        instrucao = (
            f"Cliente: etapa={etapa}, operacoes={ops}, regime={regime}.\n"
            f"DADOS DA BASE (use SO isto):\n{ctx}\n\n"
            "Analise a tributacao de CADA produto encontrado, um por vez. Para CADA item dos dados, "
            "escreva um bloco SEPARADO exatamente neste formato Markdown:\n\n"
            "### {produto} — NCM {ncm}\n"
            "- **CFOP (por operação):** quando o item tiver a lista 'operacoes', escreva UMA LINHA por operação: "
            "{rotulo} -> CFOP {cfop_exato} — {cfop_significado}\n"
            "- **Tributacao:** {beneficio}; CST {cst_icms}/CSOSN {csosn}; carga/reducao se houver\n"
            "- **Como funciona (para a operacao ser valida):** explique o MECANISMO do beneficio e liste os "
            "REQUISITOS/CONDICOES CONCRETOS a cumprir para ter direito (quem pode usar, o que precisa constar na "
            "Nota Fiscal, registros/credenciamentos/documentos exigidos, vigencia), citando os campos 'condicao', "
            "'base_legal' e 'fonte' dos dados.\n\n"
            "REGRAS DA ANALISE:\n"
            "- NAO junte tudo numa explicacao unica: um bloco por produto.\n"
            "- Se os produtos tiverem beneficios DIFERENTES (ex.: um com Reducao de BC e outro com Isencao), "
            "deixe explicito que sao enquadramentos distintos, diga QUAL produto exato tem cada beneficio e, "
            "quando houver, aponte o mais vantajoso.\n"
            "- Use SOMENTE os dados fornecidos; nao invente NCM/CFOP/CST/base legal.\n"
            "- No fim, uma linha 'Resumo:' dizendo qual enquadramento se aplica ao caso do cliente."
        )
        msgs = [{"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": instrucao}]
        txt, err = await chamar_ia(msgs, max_tokens=1600)
        res["explicacao_ia"] = txt or f"(IA indisponivel: {err})"
    elif inp.explicar_ia and not res.get("encontrou"):
        # NAO achou beneficio na base -> IA identifica o produto do NCM, orienta a tributacao
        # generica (o que o cliente deve colocar) e SUGERE se ha beneficio p/ ele ou similar.
        fb = res.get("fallback", {})
        cod_ncm = _dig(inp.produto)
        # descricao OFICIAL do NCM (base local -> senao Reforma/LegisWeb, cacheado) p/ ATERRAR a IA
        desc_oficial, desc_fonte = await _ncm_desc_oficial(cod_ncm, inp.produto)
        if desc_oficial:
            res["ncm_descricao_oficial"] = desc_oficial
            res["ncm_descricao_fonte"] = desc_fonte
            if isinstance(res.get("fallback"), dict):
                res["fallback"]["produto_oficial"] = desc_oficial
        if desc_oficial:
            bloco_produto = (
                "### O que e este produto\n"
                f"A descricao OFICIAL (TIPI) do NCM {cod_ncm or inp.produto} e: \"{desc_oficial}\". "
                "Use EXATAMENTE essa mercadoria — NAO invente nem troque por outro produto. "
                "Escreva em 1 frase o que e, em linguagem simples.\n\n"
            )
        else:
            bloco_produto = (
                "### O que e este produto\n"
                "NAO foi possivel identificar a mercadoria deste NCM com seguranca na nossa base. "
                "Diga isso honestamente e peca para o cliente confirmar o produto/NCM na TIPI. "
                "NAO invente o nome do produto nem o capitulo se nao tiver certeza.\n\n"
            )
        instrucao = (
            f"O cliente pesquisou: \"{inp.produto}\""
            + (f" (NCM {cod_ncm})" if cod_ncm else "")
            + f". Contexto: etapa={etapa}, operacoes={ops}, regime={regime}, UF=Maranhao.\n"
            "Este produto NAO consta nas listas de beneficio de ICMS/MA da nossa base. "
            "Escreva uma orientacao ACOLHEDORA e pratica em Markdown, nesta ordem:\n\n"
            + bloco_produto +
            "### Como tributar (regra geral, sem beneficio)\n"
            f"- ICMS: {fb.get('icms','')}\n"
            f"- CFOP: {fb.get('cfop','')} (para a operacao selecionada)\n"
            f"- Federal: {fb.get('federal','')}\n"
            "Explique em 1 frase simples o que o cliente deve colocar na nota.\n\n"
            "### Vale conferir (possiveis beneficios)\n"
            "Com base no PRODUTO OFICIAL acima, SUGIRA onde pode haver beneficio no RICMS/MA "
            "(ex.: alimento da CESTA BASICA -> reducao/isencao; medicamento -> Convenio 87/02; "
            "insumo agropecuario -> Convenio 100/97; material de construcao -> possivel reducao de BC). "
            "Deixe CLARO que e uma SUGESTAO a verificar, nao uma norma confirmada. NAO invente numero de "
            "decreto, aliquota ou artigo: se nao tiver certeza da base legal, diga 'confirmar a base legal'.\n\n"
            "Seja direto, no maximo ~150 palavras. Nao use jargao sem explicar."
        )
        msgs = [{"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": instrucao}]
        txt, err = await chamar_ia(msgs, max_tokens=1400)
        if txt:
            res["sugestao_ia"] = txt
    return res

@app.get("/api/segmentos")  # RECURSOS EXTRAS (removível): atalhos por segmento
async def segmentos():
    if not RECURSOS_EXTRAS:
        return {"segmentos": []}
    return {"segmentos": eligibilidade.SEGMENTOS}


def _produtos_do_segmento(seg_id):
    """Lista 'quais produtos têm benefício' de um segmento, filtrada na base."""
    f = (eligibilidade.SEGMENTO_FILTROS or {}).get(seg_id)
    if not f:
        return []
    pals = f.get("palavras", [])
    pres = [p.replace(".", "") for p in f.get("ncm_prefixos", [])]
    econ = eligibilidade.BENEF_ECONOMIA
    out = []
    vistos = set()
    for b in BENEF:
        hay = _norm((b.get("produto", "") or "") + " " + (b.get("ncm_descricao", "") or ""))
        match_pal = pals and any(p in hay for p in pals)
        match_ncm = pres and any((b.get("ncm") or "").replace(".", "").startswith(p) for p in pres)
        if not (match_pal or match_ncm):
            continue
        ben_norm = _norm(b.get("beneficio", ""))
        if not any(e in ben_norm for e in econ):
            continue  # só benefício econômico (Isenção, Redução BC, Diferimento, Crédito...)
        chave = (b.get("produto", "").strip().lower(), (b.get("ncm") or "").replace(".", ""), ben_norm)
        if chave in vistos:
            continue
        vistos.add(chave)
        out.append({
            "produto": b.get("produto", ""),
            "ncm": b.get("ncm", ""),
            "beneficio": b.get("beneficio", ""),
            "beneficio_resumo": b.get("beneficio_resumo", ""),
            "reducao_bc": b.get("reducao_bc", ""),
            "carga_final": b.get("carga_final", ""),
            "base_legal": b.get("base_legal", ""),
            "condicao": b.get("condicao_resumo", "") or b.get("condicao_texto_integral", ""),
            "condicao_integral": b.get("condicao_texto_integral", ""),
            "fonte": b.get("fonte_texto_integral", ""),
        })
    out.sort(key=lambda x: x["produto"])
    return out


@app.get("/api/segmento_produtos")  # RECURSOS EXTRAS (removível): lista por segmento
async def segmento_produtos(segmento: str = ""):
    if not RECURSOS_EXTRAS:
        return {"ok": False, "itens": []}
    itens = _produtos_do_segmento(segmento)
    return {"ok": True, "segmento": segmento, "total": len(itens), "itens": itens}

# ---- Auditor de NCM (RECURSOS EXTRAS; removível) ----
class AuditoriaIn(BaseModel):
    texto: str | None = None      # linhas "descricao ; ncm"
    itens: list = []              # ou [{"descricao":..,"ncm":..}]
    usar_ia: bool = True          # sugestao de NCM por raciocinio (RGI), validada na tabela oficial
    etapa: str = "varejo"
    operacao: str = "interna"
    regime: str = "lucro_presumido"

def _enriquece_fiscal(row, etapa, operacao, regime):
    """Aplica o CONTEXTO do cliente (etapa+operação+regime) no enquadramento do NCM:
    substitui benefício/CST-CSOSN/CFOP genéricos pelo correto da operação + checklist."""
    cod = re.sub(r"\D", "", row.get("ncm_informado", "") or "")
    itens = POR_NCM_APP.get(cod)
    if not itens:
        return
    it = montar_item(itens[0], etapa, operacao, regime)
    row["beneficio"] = it["beneficio"]
    row["cst"] = it["trib"]
    row["campo_trib"] = it["campo_trib"]
    row["cfop"] = it["cfop_exato"]
    row["cfop_significado"] = it["cfop_significado"]
    row["cest"] = it["cest"] or row.get("cest", "")
    row["carga_final"] = it.get("carga_final", "")
    if it.get("elegibilidade"):
        row["elegibilidade"] = it["elegibilidade"]

MAX_IA = 15  # limite de linhas problematicas que chamam a IA por requisicao (custo/tempo)

async def _sugestao_ia_ncm(descricao, ncm_atual, status):
    """IA raciocina o NCM (RGI); o codigo devolvido e VALIDADO na tabela oficial (nunca inventa)."""
    cands = _audit.candidatos_ia(descricao, 8)
    ctx = "\n".join(f"- {c['ncm']}: {c['desc']}" for c in cands) or "(sem candidatos)"
    sys_p = ("Voce e classificador fiscal de mercadorias na NCM/SH, aplicando as Regras Gerais de "
             "Interpretacao (RGI 1 a 6) e notas de secao/capitulo. Escolha o NCM de 8 digitos mais "
             "adequado. Prefira um dos candidatos listados; so proponha outro se nenhum servir. "
             "NUNCA invente codigo. Responda APENAS JSON.")
    usr = (f"Produto: \"{descricao}\".\nNCM informado: {ncm_atual or '(nenhum)'} — situacao: {status}.\n"
           f"Candidatos da tabela oficial (NCM: descricao completa):\n{ctx}\n\n"
           "Responda: {\"ncm\":\"NNNNNNNN\",\"justificativa\":\"curta, citando a RGI\",\"confianca\":\"alta|media|baixa\"}")
    txt, err = await chamar_ia([{"role": "system", "content": sys_p}, {"role": "user", "content": usr}],
                               temperature=0.0, max_tokens=260)
    if not txt:
        return None
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
    except Exception:
        return None
    cod = re.sub(r"\D", "", str(o.get("ncm", "")))
    if len(cod) != 8 or not _audit.existe_vigente(cod):
        return {"ncm": "", "aviso": "IA sugeriu código inexistente/revogado — descartado.",
                "justificativa": str(o.get("justificativa", ""))[:200], "confianca": "—"}
    return {"ncm": f"{cod[:4]}.{cod[4:6]}.{cod[6:8]}", "oficial": _audit.desc_de(cod)[:120],
            "justificativa": str(o.get("justificativa", ""))[:240],
            "confianca": str(o.get("confianca", "")).lower()}

def _parse_linhas(texto):
    linhas = []
    for ln in (texto or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = re.split(r"\s*[;\t|]\s*", ln)          # separadores: ; tab |
        if len(parts) >= 2 and re.search(r"\d", parts[-1]):
            linhas.append((";".join(parts[:-1]).strip(), parts[-1].strip()))
        else:
            m = re.search(r"([0-9][0-9.\s]{5,})\s*$", ln)  # NCM no fim da linha
            if m:
                linhas.append((ln[:m.start()].strip(" -\t"), m.group(1).strip()))
            else:
                linhas.append((ln, ""))
    return linhas

@app.post("/api/auditoria")
async def auditoria(inp: AuditoriaIn):
    if _audit is None:
        return {"ok": False, "erro": "Módulo de auditoria indisponível no servidor.", "linhas": []}
    if inp.itens:
        linhas = [(str(x.get("descricao", "")), str(x.get("ncm", ""))) for x in inp.itens
                  if (x.get("descricao") or x.get("ncm"))]
    else:
        linhas = _parse_linhas(inp.texto)
    linhas = linhas[:300]
    res = [_audit.audita(d, n) for d, n in linhas]
    for r in res:  # aplica o contexto (regime/operação) no enquadramento fiscal
        _enriquece_fiscal(r, inp.etapa, inp.operacao, inp.regime)
    def _cat(r):
        cl = r.get("classe", "ok")
        if cl == "invalido":
            return "erro"
        if cl == "incompleto":
            return "atencao"
        return "ok"
    cats = [_cat(r) for r in res]
    # IA: sugere NCM (RGI) só nas linhas problemáticas, validando o código na tabela oficial
    if inp.usar_ia and AI_KEY:
        usados = 0
        for r, cat in zip(res, cats):
            if cat in ("erro", "atencao") and usados < MAX_IA:
                sug = await _sugestao_ia_ncm(r["descricao"], r["ncm_informado"], r["status_ncm"])
                if sug:
                    r["sugestao_ia"] = sug
                usados += 1
        if usados >= MAX_IA:
            resumo_ia = f"IA aplicada nas primeiras {MAX_IA} linhas com pendência (limite por consulta)."
        else:
            resumo_ia = ""
    else:
        resumo_ia = ""
    resumo = {"total": len(res), "ok": cats.count("ok"),
              "atencao": cats.count("atencao"), "erro": cats.count("erro"), "nota_ia": resumo_ia}
    return {"ok": True, "resumo": resumo, "linhas": res}

# ---- Upload de planilha: servidor lê o .xlsx e audita (job em background) ----
AUD_JOBS = {}

def _slim(a):
    return {k: a.get(k) for k in ("descricao", "ncm_informado", "status_ncm", "aderencia",
            "diag_aderencia", "sugestoes", "cest", "beneficio", "cst", "cfop", "recomendacao")}

def _job_audit(job_id, linhas, etapa, operacao, regime):
    try:
        itens, dc, nc = _audit.extrair_itens(linhas)
        AUD_JOBS[job_id]["total"] = len(itens)
        res = []
        for i, (d, n) in enumerate(itens):
            a = _audit.audita(d, n)
            _enriquece_fiscal(a, etapa, operacao, regime)
            res.append(a)
            if i % 40 == 0:
                AUD_JOBS[job_id]["progresso"] = i
        def cl(a):
            c = a.get("classe", "ok")
            return "erro" if c == "invalido" else ("atencao" if c == "incompleto" else "ok")
        cats = [cl(a) for a in res]
        out = str(Path(tempfile.gettempdir()) / f"auditoria_{job_id}.xlsx")
        _audit.escreve_auditoria(res, out)
        AUD_JOBS[job_id].update(estado="pronto", progresso=len(res), arquivo=out,
            resumo={"total": len(res), "ok": cats.count("ok"), "atencao": cats.count("atencao"),
                    "erro": cats.count("erro"), "col_desc": dc, "col_ncm": nc},
            preview=[_slim(a) for a in res[:80]])
    except Exception as e:
        AUD_JOBS[job_id].update(estado="erro", erro=str(e))

@app.post("/api/auditoria_upload")
async def auditoria_upload(arquivo: UploadFile = File(...),
                           etapa: str = "varejo", operacao: str = "interna", regime: str = "lucro_presumido"):
    if _audit is None:
        return {"ok": False, "erro": "Módulo de auditoria indisponível."}
    data = await arquivo.read()
    try:
        import _xlsx_read
        linhas = _xlsx_read.ler_xlsx(bytes(data))
    except Exception as e:
        return {"ok": False, "erro": f"Não consegui ler o arquivo. Envie um .xlsx. ({e})"}
    if not linhas:
        return {"ok": False, "erro": "Planilha vazia."}
    job_id = uuid.uuid4().hex[:12]
    AUD_JOBS[job_id] = {"estado": "processando", "progresso": 0, "total": 0}
    threading.Thread(target=_job_audit, args=(job_id, linhas, etapa, operacao, regime), daemon=True).start()
    return {"ok": True, "job": job_id, "linhas_arquivo": len(linhas)}

@app.get("/api/auditoria_status")
async def auditoria_status(job: str):
    j = AUD_JOBS.get(job)
    if not j:
        return {"ok": False, "erro": "job não encontrado"}
    return {"ok": True, **{k: v for k, v in j.items() if k != "arquivo"}}

@app.get("/api/auditoria_baixar")
async def auditoria_baixar(job: str):
    j = AUD_JOBS.get(job)
    if not j or j.get("estado") != "pronto":
        raise HTTPException(404, "resultado não disponível")
    return FileResponse(j["arquivo"], filename="Auditoria_NCM_resultado.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------- parser local determinístico (funciona SEM IA) ----------
STOP_WORDS = set(_norm(w) for w in (
    "vou quero preciso vender vendo vende vendendo compro comprando comprar consultar queria gostaria sou temos "
    "estou uma um uns da do das dos de pra para pro com aqui no na meu minha estado maranhao ma interna "
    "interestadual consumidor final varejo atacado industria produtor rural regime normal simples lucro real "
    "presumido cooperativa outro outros mei microempreendedor contribuinte fiscal icms operacao produto sobre "
    "qual como me te os as o a e se por em pelo pela nao sim ser sei nos voce vcs seu sua faria farei faco "
    "fazer mercadoria mercadorias produtos venda vendas informacoes info pode poderia ajuda tributario "
    "tributacao beneficio beneficios cliente clientes usar emitir nota nf nfe destinar destino sobre aquele "
    "aquela toda todo quero saber precisava bom dia boa tarde boa noite ola olá obrigado obrigada".split()
))

def _achar_etapa(tn):
    for kw in ("varejo", "loja", "mercado", "supermercado", "farmacia", "revenda", "retail", "pdv", "atacarejo"):
        if kw in tn: return "varejo"
    for kw in ("atacado", "atacadista", "distribuidor"):
        if kw in tn: return "atacado"
    for kw in ("industria", "fabrica", "fabricante", "importador", "manufatura"):
        if kw in tn: return "industria"
    for kw in ("produtor rural", "produtor", "fazendeiro", "fazenda", "pecuarista", "agricultor", "criador", "sitiante", "rural"):
        if kw in tn: return "produtor"
    return None

def _achar_operacao(tn, etapa):
    if etapa == "produtor":
        for kw in ("cooperativa", "industrializar", "industrializacao", "esmagamento"):
            if kw in tn: return "rural_cooperativa"
        for kw in ("outro estado", "interestadual", "fora do estado", "outra uf", "pra sp", "pra go", "para sp", "para go"):
            if kw in tn: return "rural_interestadual"
        for kw in ("aqui no ma", "aqui no maranhao", "dentro do estado", "no ma", "no maranhao", "interna", "local", "mesmo estado", "no estado"):
            if kw in tn: return "rural_interna"
    # venda interna para consumidor final (dentro do MA)
    tem_consumidor = any(k in tn for k in ("consumidor final", "consumidor", "pessoa fisica", "cliente final", "nao contribuinte", "nao-contribuinte"))
    tem_interna = any(k in tn for k in ("aqui no ma", "aqui no maranhao", "dentro do estado", "no ma", "no maranhao", "interna", "local", "mesmo estado", "no estado"))
    if tem_consumidor and tem_interna:
        return "interna_consumidor"
    for kw in ("consumidor final", "pessoa fisica", "cliente final", "nao contribuinte", "consumidor"):
        if kw in tn: return "consumidor"
    for kw in ("outro estado", "interestadual", "fora do estado", "outra uf", "pra sp", "pra go", "para sp", "para go"):
        if kw in tn: return "inter"
    for kw in ("aqui no ma", "aqui no maranhao", "dentro do estado", "no ma", "no maranhao", "interna", "local", "mesmo estado", "no estado"):
        if kw in tn: return "interna"
    return None

def _achar_regime(tn):
    for kw in ("lucro real", "lucro real ou presumido"):
        if kw in tn: return "lucro_real"
    for kw in ("lucro presumido", "presumido"):
        if kw in tn: return "lucro_presumido"
    for kw in ("microempreendedor",):
        if kw in tn: return "mei"
    if re.search(r"\bmei\b", tn): return "mei"
    for kw in ("simples nacional", "simples"):
        if kw in tn: return "simples"
    if "normal" in tn:
        return "lucro_real"
    return None

# indice de nomes de produto da base para extração
_PROD_NORMS = {}
for _b in BENEF:
    _p = _norm(_b.get("produto", ""))
    if len(_p) >= 4:
        _PROD_NORMS.setdefault(_p, _b.get("produto", ""))
_PROD_KEYS = sorted(_PROD_NORMS.keys(), key=len, reverse=True)

def _achar_produto(msg_original):
    digits = re.sub(r"\D", "", msg_original or "")
    if len(digits) >= 8 and digits.isdigit():
        return digits[:8]
    tn = _norm(msg_original)
    toks = [t for t in tn.split() if t not in STOP_WORDS and len(t) > 1]
    if not toks:
        return None
    for chave in _PROD_KEYS[:2000]:
        if chave in tn:
            return _PROD_NORMS[chave]
    for n in (4, 3, 2, 1):
        for i in range(len(toks) - n + 1):
            frase = " ".join(toks[i:i + n])
            if frase in _PROD_NORMS:
                return _PROD_NORMS[frase]
    return " ".join(toks[:2])

def _parse_local(texto, perfil=None):
    tn = _norm(texto)
    campos = {}
    etapa = _achar_etapa(tn) or (perfil or {}).get("tipo_contribuinte")
    if etapa:
        campos["etapa"] = etapa
    op = _achar_operacao(tn, etapa)
    if op:
        campos["operacao"] = op
        if op.startswith("rural_"):
            campos["etapa"] = "produtor"
    reg = _achar_regime(tn) or (perfil or {}).get("regime")
    if reg:
        campos["regime"] = reg
    prod = _achar_produto(texto)
    if prod:
        campos["produto"] = prod
    return campos

def _proxima_pergunta(faltando):
    for c in ("produto", "etapa", "operacao", "regime"):
        if c not in faltando:
            continue
        if c == "produto":
            return "Qual é o produto (nome ou NCM)?"
        if c == "etapa":
            return "Qual a etapa: varejo, atacado, indústria ou produtor rural?"
        if c == "operacao":
            return "Para onde vai a mercadoria? (1) aqui no MA  (2) outro estado  (3) consumidor final"
        if c == "regime":
            return "Qual o regime: lucro real, lucro presumido, simples nacional ou MEI?"
    return "Pode informar o produto (nome ou NCM)?"


@app.post("/api/chat")
async def chat(inp: ChatIn):
    perfil = _perfil(inp.perfil_id)
    msgs_user = [m.get("content", "") for m in inp.historico if m.get("role") == "user"]
    msgs_user.append(inp.mensagem)
    texto = " ".join(msgs_user)
    perfil_txt = ""
    if perfil:
        perfil_txt = (f"\nPERFIL DO CLIENTE (ja definido, NAO pergunte estes): "
                      f"etapa/tipo={perfil.get('tipo_contribuinte','?')}, regime={perfil.get('regime','?')}, "
                      f"ramo={perfil.get('ramo','')}, operacoes={perfil.get('operacoes_habituais','')}. "
                      f"Campos que VOCE DEVE perguntar ao cliente: {perfil.get('perguntar') or ['produto/NCM','operacao']}.")
    # 1) tenta IA (entende linguagem natural)
    pergunta_ia, campos_ia = None, None
    if AI_KEY:
        controller = [{"role": "system", "content": f"""Voce e um assistente fiscal que coleta dados para consulta ICMS/MA.

CAMPOS OBRIGATORIOS: produto, etapa, operacao, regime.

REGRAS DE INFERENCIA AUTOMATICA (NAO pergunte o que ja da pra inferir):
- PRODUTO: o nome ou NCM que o cliente falar. Ex: "arroz", "gado", "1006.30.21"
- ETAPA: 
  * "varejo"/"loja"/"mercado"/"supermercado"/"farmacia" => varejo
  * "atacado"/"distribuidor"/"atacarejo" => atacado
  * "fabrica"/"industria"/"fabricante"/"importador" => industria
  * "produtor rural"/"fazendeiro"/"pecuarista"/"agricultor"/"criador"/"produtor" => produtor
  * gado/boi/vaca/carne/leite/bovino SEM dizer produtor => varejo (nao inferir produtor sozinho)
- OPERACAO:
  * "aqui no MA"/"dentro do estado"/"interna"/"no Maranhao" sem consumidor => interna
  * "consumidor final"/"pessoa fisica" + "aqui no MA"/"dentro do estado" => interna_consumidor
  * "outro estado"/"interestadual"/"pra SP"/"pra GO"/"fora do estado" => inter
  * "consumidor final"/"pessoa fisica"/"cliente final de fora" => consumidor
  * produtor + "dentro do MA" => rural_interna
  * produtor + "cooperativa"/"industrializar" => rural_cooperativa
  * produtor + "outro estado" => rural_interestadual
- REGIME:
  * "lucro real"/"lucro presumido" => lucro_real / lucro_presumido
  * "simples"/"simples nacional"/"MEI" => simples / mei
  * Se nao souber, pergunte.

ORDEM DE PERGUNTAS (uma por vez, so o que faltar):
1. produto -> 2. etapa -> 3. operacao -> 4. regime
Se faltarem etapa e regime, pergunte ETAPA primeiro.
Se o cliente der TUDO de uma vez, va direto pro PRONTO.

Responda APENAS com a pergunta, sem explicacoes, sem pensar em voz alta, sem "okay".

Quando tiver TODOS os 4 campos, responda EXATAMENTE (uma linha, sem texto extra):
PRONTO {{"produto":"...","etapa":"...","operacao":"...","regime":"..."}}

{perfil_txt}"""}]
        controller += inp.historico[-10:]
        controller.append({"role": "user", "content": inp.mensagem})
        txt, err = await chamar_ia(controller, temperature=0.0, max_tokens=400)
        if not err:
            m = re.search(r"PRONTO\s*(\{.*\})", txt or "", re.S)
            if m:
                try:
                    campos_ia = json.loads(m.group(1))
                except Exception:
                    campos_ia = None
            else:
                pergunta_ia = (txt or "").strip() or None
    # 2) parser local SEMPRE roda como reserva (nao depende de IA)
    campos_local = _parse_local(texto, perfil)
    campos = {}
    for k in ("produto", "etapa", "operacao", "regime"):
        campos[k] = (campos_ia or {}).get(k) or campos_local.get(k) or ""
    # 3) campos efetivos (perfil pre-preenche)
    efetivo = {
        "produto": campos["produto"],
        "etapa": (perfil or {}).get("tipo_contribuinte") or campos["etapa"] or "varejo",
        "operacao": campos["operacao"] or "interna",
        "regime": (perfil or {}).get("regime") or campos["regime"] or "normal",
    }
    faltando = []
    if not efetivo["produto"]: faltando.append("produto")
    if not campos["etapa"] and not (perfil or {}).get("tipo_contribuinte"): faltando.append("etapa")
    if not campos["operacao"]: faltando.append("operacao")
    if not campos["regime"] and not (perfil or {}).get("regime"): faltando.append("regime")
    if faltando:
        if pergunta_ia:
            return {"tipo": "pergunta", "mensagem": pergunta_ia}
        return {"tipo": "pergunta", "mensagem": _proxima_pergunta(faltando)}
    # 4) resolve deterministico na base
    res = resolver(efetivo["produto"], efetivo["etapa"], efetivo["operacao"], efetivo["regime"], perfil)
    # 5) explica com IA se possivel; senao resposta local
    fin = None
    if AI_KEY:
        ctx = json.dumps(res.get("itens", res.get("fallback", {}))[:3] if res.get("encontrou") else res.get("fallback", {}), ensure_ascii=False)
        msgs = [{"role": "system", "content": SYS_PROMPT},
                {"role": "user", "content": f"Consulta: {json.dumps(efetivo, ensure_ascii=False)}.\nDADOS DA BASE (use SO isto):\n{ctx}\n\nResponda ao cliente com: (1) CFOP EXATO + significado; (2) tributacao (CST/CSOSN), carga/reducao; (3) os REQUISITOS/CONDICOES para ter direito ao beneficio (quem pode usar e o que cumprir, citando base_legal e condicao dos dados). Objetivo e claro."}]
        fin, err2 = await chamar_ia(msgs, max_tokens=700)
    return {"tipo": "resultado", "mensagem": fin or _resposta_fallback(res, efetivo, efetivo["etapa"], efetivo["regime"]), "campos": efetivo, "dados": res}


def _resposta_fallback(res, campos, etapa, regime):
    """Gera resposta mesmo sem IA."""
    if not res.get("encontrou"):
        f = res.get("fallback", {})
        return (f"Sem beneficio encontrado na base.\n"
                f"CFOP: {f.get('cfop','-')}\n"
                f"ICMS: {f.get('icms','-')}\n"
                f"Federal: {f.get('federal','-')}\n"
                f"Obs: {f.get('obs','Confirme com o contador.')}")
    itens = res.get("itens", [])
    if not itens:
        return "Nenhum resultado encontrado. Confirme com o contador."
    it = itens[0]
    partes = [f"CFOP: {it.get('cfop_exato','')} — {it.get('cfop_significado','')}"]
    if it.get('campo_trib') and it.get('trib'):
        partes.append(f"{it['campo_trib']}: {it['trib']}{(' (' + it['trib_obs'] + ')') if it.get('trib_obs') else ''}")
    if it.get('reducao_bc'):
        partes.append(f"Reducao BC: {it['reducao_bc']}")
    if it.get('carga_final'):
        partes.append(f"Carga efetiva: {it['carga_final']}")
    if it.get('beneficio_resumo'):
        partes.append(f"Beneficio: {it.get('beneficio','')} — {it['beneficio_resumo']}")
    if it.get('como_funciona'):
        partes.append("COMO FUNCIONA (p/ ter o beneficio):")
        for bl in it['como_funciona']:
            d = bl['d']
            if isinstance(d, list):
                partes.append(f"* {bl['t']}:")
                partes += [f"  - {i}" for i in d]
            else:
                partes.append(f"* {bl['t']}: {d}")
    else:
        if it.get('condicao'):
            partes.append(f"Requisitos/condicoes: {it['condicao'][:300]}")
        if it.get('base_legal'):
            partes.append(f"Base legal: {it['base_legal']}")
        if it.get('fonte'):
            partes.append(f"Fonte: {it['fonte']}")
    return "\n".join(partes)

@app.get("/api/status")
def status():
    import os as _os
    diag = {
        "modulo_importado": bool(legisweb),
        "import_erro": _LEGISWEB_IMPORT_ERR,
        # nomes de variáveis LEGISWEB* que o servidor enxerga (revela erro de digitação; sem valores)
        "vars_no_ambiente": sorted([k for k in _os.environ if k.upper().startswith("LEGISWEB")]),
    }
    if legisweb:
        diag["token_presente"] = bool(legisweb.TOKEN)
        diag["cliente_presente"] = bool(legisweb.CLIENTE)
        diag["uf"] = legisweb.UF_PADRAO
        diag["base_salva"] = len(_LW_CACHE)   # consultas já salvas (base separada, não gastam cota)
        _tk = legisweb.TOKEN or ""
        diag["token_hint"] = (_tk[:4] + "…" + _tk[-4:]) if len(_tk) >= 8 else ("(vazio)" if not _tk else _tk)
        diag["cliente"] = legisweb.CLIENTE
    return {"ok": True, "beneficios": len(BENEF), "modelo": MODEL, "provedor": PROVIDER,
            "chave_configurada": bool(AI_KEY),
            "legisweb": bool(legisweb and legisweb.disponivel()),
            "legisweb_diag": diag}


@app.get("/api/legisweb")
def legisweb_consulta(ncm: str = "", descricao: str = "", codigo: str = "", uf: str = ""):
    """Consulta a FONTE OFICIAL (API LegisWeb) por NCM/descrição: benefícios fiscais da UF
    (redução, isenção, crédito, diferimento) com base legal, CBENEF e vigência."""
    if not (legisweb and legisweb.disponivel()):
        return {"ok": False, "erro": "Integração LegisWeb não configurada (defina LEGISWEB_TOKEN e "
                "LEGISWEB_CLIENTE no .env do servidor)."}
    ncm = (ncm or "").strip()
    descricao = (descricao or "").strip()
    codigo = (codigo or "").strip()
    if not (ncm or descricao or codigo):
        return {"ok": False, "erro": "Informe ncm, descricao ou codigo."}
    try:
        res = legisweb.beneficios(ncm=ncm or None, descricao=descricao or None,
                                  codigo=codigo or None, estado=(uf or None))
        return res
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao consultar LegisWeb: {e}"}


@app.get("/api/reforma")
def reforma_consulta(ncm: str = "", descricao: str = ""):
    """Benefícios da REFORMA TRIBUTÁRIA (IBS/CBS) por NCM ou descrição — fonte oficial LegisWeb."""
    if not (legisweb and legisweb.disponivel()):
        return {"ok": False, "erro": "Integração LegisWeb não configurada."}
    ncm = (ncm or "").strip()
    descricao = (descricao or "").strip()
    if not (ncm or descricao):
        return {"ok": False, "erro": "Informe ncm ou descricao."}
    try:
        return legisweb.reforma(ncm=ncm or None, descricao=descricao or None)
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao consultar Reforma: {e}"}


@app.get("/api/gtin")
def gtin_consulta(gtin: str = "", cod_cert: str = ""):
    """Resolve GTIN/EAN (código de barras) -> produto/NCM/CEST (exige certificado A1)."""
    if not (legisweb and legisweb.disponivel()):
        return {"ok": False, "erro": "LegisWeb não configurada."}
    gtin = (gtin or "").strip()
    if not gtin:
        return {"ok": False, "erro": "Informe o GTIN/EAN."}
    try:
        return legisweb.gtin(gtin, cod_cert=(cod_cert or None))
    except Exception as e:
        return {"ok": False, "erro": f"Falha na consulta GTIN: {e}"}


@app.get("/api/lw")
def lw_generic_endpoint(recurso: str = "", ncm: str = "", descricao: str = "", codigo: str = "",
                        gtin: str = "", estado: str = "", categoria: str = "", nbm: str = "",
                        cod_cert: str = ""):
    """Proxy genérico p/ qualquer recurso da API LegisWeb (ICMS, ST, IPI, PIS/COFINS, CFOP, CST, TIPI...)."""
    if not (legisweb and legisweb.disponivel()):
        return {"ok": False, "erro": "LegisWeb não configurada."}
    if not recurso:
        return {"ok": False, "erro": "Informe o recurso.", "recursos": legisweb.recursos}
    params = {k: v for k, v in dict(ncm=ncm, descricao=descricao, codigo=codigo, gtin=gtin,
              estado=(estado or _LW_UF), categoria=categoria, nbm=nbm, cod_cert=cod_cert).items() if v}
    try:
        return legisweb.generico(recurso, params)
    except Exception as e:
        return {"ok": False, "erro": f"Falha na consulta: {e}"}

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")

app.mount("/", StaticFiles(directory=BASE_DIR / "static"), name="static")
