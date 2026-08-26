# -*- coding: utf-8 -*-
"""Cliente da API LegisWeb (fonte oficial de benefícios/ST/alíquotas por NCM e UF).

Credenciais no .env (NUNCA no código):
    LEGISWEB_TOKEN=<t>         # token
    LEGISWEB_CLIENTE=<c>       # código de cliente (numérico)
    LEGISWEB_UF=MA            # UF padrão

Autenticação: toda chamada leva t (token) e c (código) na URL.
Consumo: só conta consulta com resultado; erros (token/param) não contam.
Doc: documentacao-api-legisweb (2026-08-26).
"""
import os
from pathlib import Path
import httpx
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

BASE = "https://www.legisweb.com.br/api"
TOKEN = os.getenv("LEGISWEB_TOKEN", "").strip()
CLIENTE = os.getenv("LEGISWEB_CLIENTE", "").strip()
UF_PADRAO = os.getenv("LEGISWEB_UF", "MA").strip() or "MA"

# categorias de benefício fiscal (param 'categoria')
CATEGORIAS = {2: "Redução de BC", 3: "Isenção", 4: "Crédito Presumido/Outorgado", 5: "Diferimento"}


def disponivel():
    """True se token e código de cliente estão configurados."""
    return bool(TOKEN and CLIENTE)


_CACHE = {}  # cache p/ conservar a cota (50/mês): mesma consulta não gasta de novo


def _get(endpoint, **params):
    """GET genérico na API LegisWeb. Retorna (ok, dados|erro). Usa cache."""
    if not disponivel():
        return False, {"erro": "LegisWeb não configurada (falta LEGISWEB_TOKEN ou LEGISWEB_CLIENTE no .env)."}
    params = {k: v for k, v in params.items() if v not in (None, "")}
    ckey = endpoint + "|" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if ckey in _CACHE:
        return _CACHE[ckey]
    params["t"] = TOKEN
    params["c"] = CLIENTE
    url = f"{BASE}/{endpoint.strip('/')}/"
    try:
        r = httpx.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        return False, {"erro": f"HTTP {e.response.status_code}", "detalhe": e.response.text[:200]}
    except Exception as e:
        return False, {"erro": f"Falha na chamada: {e}"}
    # a API pode devolver mensagem de erro no corpo
    if isinstance(data, dict):
        msg = str(data.get("mensagem") or data.get("erro") or "")
        if msg and "registros" not in data:
            return False, {"erro": msg}
        # normalizar 'resposta' quando não vem lista (ex.: "Nenhum resultado encontrado.")
        if not isinstance(data.get("resposta"), list):
            data["resposta"] = []
    res = (True, data)
    _CACHE[ckey] = res      # cacheia p/ não gastar cota em repetição
    return res


def beneficios(ncm=None, descricao=None, codigo=None, estado=None, categorias=None):
    """Consulta benefícios fiscais oficiais (todas as categorias por padrão) para a UF.
    Informe ncm OU descricao OU codigo. Retorna dict {ok, uf, itens:[...], erros:[...]}."""
    estado = (estado or UF_PADRAO).upper()
    cats = categorias or list(CATEGORIAS.keys())
    itens, erros = [], []
    for cat in cats:
        ok, data = _get("beneficio-fiscal", estado=estado, categoria=cat,
                        ncm=ncm, descricao=descricao, codigo=codigo)
        if not ok:
            erros.append({"categoria": CATEGORIAS.get(cat, cat), "erro": data.get("erro")})
            continue
        for it in (data.get("resposta") or []):
            it["_categoria_num"] = cat
            it["_categoria"] = CATEGORIAS.get(cat, str(cat))
            itens.append(it)
    return {"ok": not (erros and not itens), "uf": estado, "fonte": "LegisWeb (oficial)",
            "total": len(itens), "itens": itens, "erros": erros}


def aliquota_padrao(estado=None):
    ok, data = _get("aliquota-padrao", estado=(estado or UF_PADRAO).upper())
    return data if ok else {"erro": data.get("erro")}


def st_interna(ncm=None, descricao=None, estado=None):
    ok, data = _get("st-interna", estado=(estado or UF_PADRAO).upper(), ncm=ncm, descricao=descricao)
    return data if ok else {"erro": data.get("erro")}


def cfop(codigo=None, descricao=None):
    ok, data = _get("cfop", codigo=codigo, descricao=descricao)
    return data if ok else {"erro": data.get("erro")}


def cst(codigo=None, descricao=None):
    ok, data = _get("cst", codigo=codigo, descricao=descricao)
    return data if ok else {"erro": data.get("erro")}
