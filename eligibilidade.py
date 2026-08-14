# -*- coding: utf-8 -*-
"""RECURSOS EXTRAS (opcional) — checklist de elegibilidade + perfis por segmento.
   Facilita pro cliente: "esse benefício vale pra mim e como aplico?".

   >>> COMO DESLIGAR TUDO: em app.py, defina RECURSOS_EXTRAS = False
       (ou simplesmente apague este arquivo). O frontend some sozinho. <<<
"""
import re, unicodedata


def _n(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


# perguntas de elegibilidade disparadas por palavra-chave na condicao do beneficio
GATILHOS = [
    (("registro genealogico", "genealogia", "puros de origem", "por cruza"),
     "O animal possui REGISTRO GENEALÓGICO OFICIAL (puro de origem, por cruza ou sob controle de genealogia)?"),
    (("estabelecimento agropecuario", "cadastro de contribuintes", "inscrito no cadastro"),
     "O destinatário é ESTABELECIMENTO AGROPECUÁRIO inscrito (ou tem inscrição no CNPJ/ITR/outro meio de prova)?"),
    (("mapa", "ministerio da agricultura", "registrado no ministerio"),
     "O produto está REGISTRADO NO MAPA, com o número indicado na embalagem e na NF?"),
    (("credenciamento", "credenciado", "cegaf", "termo de acordo", "regime especial"),
     "A empresa tem o CREDENCIAMENTO / regime especial exigido pela SEFAZ-MA?"),
    (("pmc", "preco maximo", "preco tabelado"),
     "A base de cálculo usa o PMC / preço tabelado exigido (produto farmacêutico)?"),
    (("cesta basica",),
     "O produto está na lista da CESTA BÁSICA do MA (Anexo 1.4)?"),
    (("exportacao", "exterior", "exportar"),
     "A operação é destinada à EXPORTAÇÃO / ao exterior?"),
    (("ativo imobilizado", "bem do ativo"),
     "O bem é para o ATIVO IMOBILIZADO (uso próprio, não revenda)?"),
    (("demonstr", "constar", "indicacao do dispositivo", "valor do imposto dispensado", "abatimento", "deduz"),
     "A Nota Fiscal vai INDICAR o dispositivo legal e DEMONSTRAR o abatimento/valor do imposto dispensado?"),
    (("uso na pecuaria", "alimentacao animal", "racao"),
     "O produto destina-se EXCLUSIVAMENTE ao uso na pecuária / alimentação animal?"),
    (("agricultura", "corretivo", "recuperador do solo"),
     "O insumo destina-se EXCLUSIVAMENTE ao uso na agricultura?"),
]


def gerar_checklist(item, etapa, operacao, regime, categoria_regime):
    """Devolve um checklist SIM/NÃO. O sistema não decide sozinho — ele pergunta
    ao cliente o que só ele sabe e conclui a partir das respostas."""
    ben = _n(item.get("beneficio", ""))
    cond_raw = item.get("condicao") or item.get("condicao_completa") or ""
    cond = _n(cond_raw)
    perguntas = []

    # 1) porta do regime (Simples x Normal)
    if categoria_regime == "simples":
        if "substitu" in ben:
            perguntas.append({"q": "(Simples) Se o ICMS-ST já foi retido, revenda com CSOSN 500.", "tipo": "info"})
        elif any(x in ben for x in ("reduc", "isenc", "credito", "diferi")):
            perguntas.append({"q": "ATENÇÃO (Simples Nacional): benefício estadual de redução/isenção em regra NÃO se aplica — "
                                   "recolhe pelo DAS (CSOSN 102). Confirme com o contador.", "tipo": "alerta"})

    # 2) substituto x substituído (ST)
    if "substitu" in ben:
        if etapa == "industria":
            perguntas.append({"q": "Você é INDÚSTRIA/importador (substituto)? Então VOCÊ retém o ICMS-ST (CST 10 / CFOP 5.401).", "tipo": "info"})
        else:
            perguntas.append({"q": "O ICMS-ST já foi retido por quem te vendeu (indústria/distribuidor)?", "tipo": "sim"})

    # 3) requisitos específicos por palavra-chave (casa por PALAVRA inteira p/ evitar
    #    falso positivo, ex.: "racao" dentro de "operacao")
    def _tem(chave):
        return re.search(r"\b" + re.escape(chave) + r"\b", cond) is not None
    ja = set()
    for chaves, pergunta in GATILHOS:
        if pergunta not in ja and any(_tem(c) for c in chaves):
            perguntas.append({"q": pergunta, "tipo": "sim"})
            ja.add(pergunta)

    # 4) fallback: cláusula "desde que ..." quando nada específico disparou
    if not any(p["tipo"] == "sim" for p in perguntas):
        m = re.search(r"desde que ([^.;]{8,140})", cond_raw, re.I)
        if m:
            perguntas.append({"q": "Você cumpre a condição: “" + m.group(1).strip() + "”?", "tipo": "sim"})

    # resultado (o que fazer se todos SIM / se algum NÃO)
    if "isenc" in ben:
        ok = "✅ ISENTO — emita com CST 40 (Simples: CSOSN 103/300/400). Não aproveite crédito na entrada."
    elif "reduc" in ben:
        ok = "✅ REDUÇÃO DE BASE — use o CST/carga indicados e guarde a base legal na escrita fiscal."
    elif "diferi" in ben:
        ok = "✅ DIFERIMENTO — sem destaque agora (CST 51); o imposto é recolhido na etapa seguinte."
    elif "substitu" in ben:
        ok = "✅ ST — se já retido, revenda com CST 60 (CSOSN 500 no Simples), sem novo destaque."
    else:
        ok = "✅ Aplique o enquadramento indicado acima."
    nao = ("⚠️ Faltando QUALQUER requisito, o benefício NÃO se aplica: tribute normalmente "
           "(CST 00, alíquota interna 23%) e confirme com o contador.")

    return {"titulo": "Esse benefício vale pra você? Marque para confirmar:",
            "perguntas": perguntas, "se_todos_sim": ok, "se_algum_nao": nao}


# ---- perfis por segmento (atalhos que já entram no modo certo) ----
SEGMENTOS = [
    {"id": "supermercado", "label": "🛒 Supermercado / Mercearia", "etapa": "varejo", "operacao": "interna_consumidor",
     "buscas": ["arroz", "feijao", "cesta basica", "refrigerante", "carne"],
     "alertas": ["Muitos itens são ST — você revende com CST 60 (sem novo destaque).", "Cesta básica: carga reduzida (~7%)."]},
    {"id": "farmacia", "label": "💊 Farmácia / Drogaria", "etapa": "varejo", "operacao": "interna_consumidor",
     "buscas": ["medicamento", "preservativo", "fralda", "soro"],
     "alertas": ["Medicamento é ST (base PMC).", "Alguns têm isenção; PIS/COFINS monofásico é comum."]},
    {"id": "autopecas", "label": "🔧 Autopeças", "etapa": "varejo", "operacao": "interna",
     "buscas": ["pneu", "autopeca", "bateria", "oleo lubrificante"],
     "alertas": ["Autopeças são ST.", "PIS/COFINS monofásico em vários itens."]},
    {"id": "construcao", "label": "🧱 Material de Construção", "etapa": "varejo", "operacao": "interna",
     "buscas": ["tijolo", "telha", "cimento", "tinta", "ferro"],
     "alertas": ["Tintas/vernizes e cimento são ST.", "Cerâmica vermelha pode ter redução."]},
    {"id": "agro", "label": "🌱 Agropecuária (loja de insumos)", "etapa": "varejo", "operacao": "interna",
     "buscas": ["racao", "semente", "adubo", "defensivo", "calcario"],
     "alertas": ["Insumos: redução 60% ou isenção (Conv. 100/97).", "Exige registro MAPA e uso comprovado."]},
    {"id": "produtor", "label": "🚜 Produtor Rural", "etapa": "produtor", "operacao": "rural_interna",
     "buscas": ["gado", "soja", "arroz em casca", "milho", "reprodutor"],
     "alertas": ["Grãos costumam ter DIFERIMENTO (exige credenciamento CEGAF).", "Reprodutores/matrizes com registro têm ISENÇÃO."]},
    {"id": "atacado", "label": "📦 Atacado / Distribuidor", "etapa": "atacado", "operacao": "inter",
     "buscas": ["bebida", "refrigerante", "cerveja", "agua mineral"],
     "alertas": ["Como atacadista você pode ser SUBSTITUTO (retém ST).", "Confira MVA e destino."]},
    {"id": "industria", "label": "🏭 Indústria / Fabricante", "etapa": "industria", "operacao": "interna",
     "buscas": ["maquina", "embalagem", "materia prima"],
     "alertas": ["Como indústria você RETÉM o ICMS-ST (CST 10).", "Veja crédito de insumos e Conv. 52/91 (máquinas)."]},
]

# ---- lista "quais produtos têm benefício" por segmento (palavras e NCMs da base) ----
SEGMENTO_FILTROS = {
    "supermercado": {
        "palavras": ["arroz", "feijao", "farinha", "oleo", "acucar", "cafe", "leite", "macarrao", "biscoito",
                     "refrigerante", "suco", "carne", "frango", "peixe", "sardinha", "queijo", "iogurte", "milho",
                     "trigo", "cebola", "alho", "batata", "mandioca", "ovo", "chocolate", "bebida", "agua mineral",
                     "cerveja", "manteiga", "margarina", "salsicha", "linguica", "salame", "presunto", "panetone",
                     "bolacha", "pao", "achocolatado", "geleia", "mel", "cereais", "tempero", "molho"],
        "ncm_prefixos": [],
    },
    "farmacia": {
        "palavras": ["medicament", "remedio", "farmaceutic", "preservativo", "fralda", "soro", "vitamina",
                     "vacina", "analgesico", "insulina", "capsula", "comprimido", "higienico", "absorvente",
                     "curativo", "lanceta", "termometro", "adstringente"],
        "ncm_prefixos": ["30"],
    },
    "autopecas": {
        "palavras": ["pneu", "autopeca", "bateria", "lubrificante", "filtro", "amortecedor", "pastilha",
                     "vela de ignicao", "retrovisor", "freio", "embreagem", "radiador", "escapamento", "correia",
                     "rolamento", "pneumatico", "eixo", "direcao", "transmissao", "motor"],
        "ncm_prefixos": ["4011", "4012", "8708", "8507", "271019"],
    },
    "construcao": {
        "palavras": ["tijolo", "telha", "cimento", "tinta", "ferro", "ceramica", "areia", "brita", "argamassa",
                     "gesso", "porta", "janela", "cano", "tubo", "vidro", "verniz", "esmalte", "piso", "revestimento",
                     "louca", "torneira", "maquinas de aterro", "massa", "pedra", "cal", "impermeabilizante"],
        "ncm_prefixos": ["25", "68", "69", "7001", "7005", "72", "73"],
    },
    "agro": {
        "palavras": ["racao", "semente", "adubo", "defensivo", "calcario", "fertilizante", "inseticida",
                     "herbicida", "fungicida", "corretivo", "alimento animal", "insumo agropecuario", "bubalino"],
        "ncm_prefixos": ["2309", "31", "3808"],
    },
    "produtor": {
        "palavras": ["gado", "bovino", "suino", "ovino", "caprino", "soja", "arroz", "milho", "reprodutor",
                     "matriz", "trigo", "algodao", "cafe", "feijao", "animal", "bezerro", "vaca", "cavalo", "bufalino"],
        "ncm_prefixos": ["01", "02", "10"],
    },
    "atacado": {
        "palavras": ["refrigerante", "cerveja", "agua mineral", "bebida", "suco", "vinho", "cachaca",
                     "energetico", "chope"],
        "ncm_prefixos": ["22"],
    },
    "industria": {
        "palavras": ["maquina", "embalagem", "materia prima", "equipamento", "ferramenta", "motor", "gerador",
                     "componente", "aparelho", "caldeira", "turbina", "bomba", "compressor", "maquina de", "reator",
                     "fabrica", "insumo industrial"],
        "ncm_prefixos": ["84", "85"],
    },
}

# quais benefícios são "economia real" (listados primeiro; o resto é ST/regra)
BENEF_ECONOMIA = ("reducao de", "isenc", "diferi", "nao incidencia", "nao tributado", "credito", "creditamento", "nao incid")
