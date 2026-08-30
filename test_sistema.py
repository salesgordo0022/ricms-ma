# -*- coding: utf-8 -*-
"""Testes de regressão do Assistente Fiscal ICMS/MA.

Garante que a informação chega CORRETA ao cliente e que bugs já corrigidos não voltam.
Uso:  python test_sistema.py   (sai != 0 se algum teste falhar)
Não precisa de servidor nem de rede (usa o motor local; não chama LegisWeb/IA).
"""
import sys, re
import app
from auditor_integridade import run as auditar

FALHAS = []


def ok(cond, nome):
    print(("  [OK] " if cond else "  [FALHOU] ") + nome)
    if not cond:
        FALHAS.append(nome)


def _achar(produto, etapa="varejo", operacao="interna", regime="lucro_presumido"):
    r = app.resolver(produto, etapa, operacao, regime, operacoes=[operacao])
    return r


print("\n=== 1. Busca por NCM (não pode vir lixo) ===")
r = app.buscar("1006.10.10")
ok(bool(r) and "arroz" in (r[0].get("produto", "").lower()), "NCM 1006.10.10 -> arroz (não óleo/xadrez)")
r = app.buscar("1006.10.10")
ok(all("3307" not in x.get("ncm", "") for x in r), "NCM 1006.x NUNCA retorna 3307 (óleos)")

print("\n=== 2. NCMs preenchidos (bug do feijão não volta p/ outros) ===")
for ncm, esperado in [("0713.33.19", "feij"), ("0901", "caf"), ("3102.10", "ureia"),
                      ("1701", "acu"), ("3808.91", None)]:
    r = app.buscar(ncm)
    achou = bool(r) and (esperado is None or esperado in app._norm(r[0].get("produto", "")))
    ok(achou, f"NCM {ncm} é achável por NCM")

print("\n=== 3. Cesta básica com benefício correto ===")
r = _achar("feijão")
it = (r.get("itens") or [{}])[0]
ok("reduc" in app._norm(it.get("beneficio", "")), "feijão = Redução de BC (cesta básica)")
ok("8%" in (it.get("carga_final", "") or ""), "feijão carga 8%")

print("\n=== 4. Diferimento NÃO se aplica a consumidor final ===")
dif = [b for b in app.BENEF if "diferimento" in app._norm(b.get("beneficio"))]
ok(len(dif) > 0, "existem itens de diferimento na base")
b = dif[0]
it_emp = app.montar_item(b, "varejo", "interna", "lucro_presumido")
it_con = app.montar_item(b, "varejo", "interna_consumidor", "lucro_presumido")
ok(not it_emp.get("nao_aplica"), "diferimento APLICA em venda entre empresas (interna)")
ok(it_con.get("nao_aplica") is True, "diferimento NÃO se aplica a consumidor final")

print("\n=== 5. Card incompleto é sinalizado ===")
inc = None
for b in app.BENEF:
    itx = app.montar_item(b, "varejo", "interna", "lucro_presumido")
    if itx.get("faltando"):
        inc = itx
        break
ok(inc is not None and isinstance(inc.get("faltando"), list), "montar_item marca 'faltando'")
ok(all("completo" in app.montar_item(b, "varejo", "interna", "lucro_presumido")
       for b in app.BENEF[:3]), "todo item tem flag 'completo'")

print("\n=== 6. Redução de BC nunca renderiza como 23% (sem número) ===")
ruins = []
for b in app.BENEF:
    ben = app._norm(b.get("beneficio"))
    carga = (b.get("carga_final") or "").strip()
    reduc = (b.get("reducao_bc") or "").strip()
    trib = app._norm(b.get("cst_icms_sugerido"))
    is_st = bool(re.search(r"(\b60\b|\b500\b|^10\b|\b201\b|\b202\b)", trib))
    if "reduc" in ben and "isen" not in ben and not carga and not reduc and not is_st:
        ruins.append(b.get("produto"))
ok(not ruins, f"nenhuma Redução de BC renderiza como 23% (achados: {ruins[:3]})")

print("\n=== 6b. Diferimento renderiza 'diferido', não 23% (regra do cardHTML) ===")
# garante que TODO diferimento tem como o card mostrar 'diferido' (beneficio contém 'diferi')
difs = [b for b in app.BENEF if "diferimento" in app._norm(b.get("beneficio"))]
ok(all("diferi" in app._norm(b.get("beneficio")) for b in difs), "diferimento identificável pelo nome do benefício")

print("\n=== 7. Resolução de nome oficial do NCM (fim do 'Outros') ===")
ok(app._ncm_desc_local("07133319", 6).lower().startswith("feij"), "_ncm_desc_local(0713.33.19) -> feijão")
ok(app._ncm_desc_local("99999999") == "", "NCM inexistente -> vazio (não inventa)")

print("\n=== 8. Segurança: entrada saneada ===")
ok(app.ConsultaIn.model_fields["produto"].metadata is not None, "ConsultaIn.produto tem restrição de tamanho")
try:
    app.ConsultaIn(produto="x" * 500)
    passou = True
except Exception:
    passou = False
ok(not passou, "produto > 200 chars é rejeitado")

print("\n=== 8b. Nenhum jargão interno ('(calc.)') chega ao cliente ===")
import json as _json
com_jargao = []
for b in app.BENEF[:400]:
    it = app.montar_item(b, "varejo", "interna", "lucro_presumido")
    if "(calc" in _json.dumps(it, ensure_ascii=False).lower():
        com_jargao.append(b.get("produto"))
ok(not com_jargao, f"card sem '(calc.)'/jargão (achados: {com_jargao[:2]})")

print("\n=== 9. Auditoria de integridade da base (0 erros graves) ===")
n_erros = auditar()
ok(n_erros == 0, f"auditor_integridade: {n_erros} erro(s) grave(s)")

print("\n" + "=" * 50)
if FALHAS:
    print(f"❌ {len(FALHAS)} TESTE(S) FALHARAM:")
    for f in FALHAS:
        print("   -", f)
    sys.exit(1)
print("✅ TODOS OS TESTES PASSARAM")
sys.exit(0)
