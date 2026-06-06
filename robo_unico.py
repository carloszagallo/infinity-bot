"""
ROBÔ ÚNICO — fusão CORRETOR_FAXINEIRO + FICHÁRIO (Infinity Autopeças)
=====================================================================
Um robô só, para as 4 contas, com:
  • REGRAS FIXAS primeiro (sem IA): INMETRO→N/A, origem→China, OEM/PART_NUMBER
    normaliza+dedup+vírgula, garantia 90d, alerta de título "original".
  • IA (Claude Haiku) só no que SOBRA: atributos abertos da ficha (marca, modelo,
    tipo de veículo, é-kit...), e SÓ para itens com health < HEALTH_MINIMO.
  • MEMÓRIA POR SKU (persistente, com validade): a ficha derivada pela IA é guardada
    por SKU e reaproveitada em todos os anúncios daquele produto, nas 4 contas
    → deriva 1x por produto, não 1x por anúncio. Mata o retrabalho e economiza token.
  • ROTEAMENTO por conta: meli_user_id vai DENTRO de params (senão cai na INFINITY).
  • LISTAGEM por scan (fura o teto de 1.000) + leitura em multiget (20 por chamada).
  • UMA escrita (PUT) por item, juntando regra + ficha.

SEGURANÇA:
  • DRY_RUN=true (padrão) → NÃO escreve nada, só simula e conta.
  • MEDIR_N>0 → roda uma amostra de N itens e cospe um RELATÓRIO DE MEDIÇÃO
    (taxa real, % que precisa de escrita, SKUs únicos, chamadas de IA) e a
    ESTIMATIVA de quantos dias o backfill vai levar. Depois PARA.
  • Só ligamos a escrita (DRY_RUN=false) depois que a medição confirmar os números.

DEPLOY STAMP: 2026-06-06 — escrita auditável (log por ID), RESET_CHECKPOINT, contadores no relatório.
"""
import os, re, csv, io, json, time, logging, requests
from datetime import datetime, timezone
from urllib.parse import quote

# ───────────────────────── Config ─────────────────────────
MAC_API_KEY    = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
MAC_BASE_URL   = "https://mcp.tiops.com.br/marketplace"

# Segurança / modo
DRY_RUN        = os.environ.get("DRY_RUN", "true").lower() == "true"     # true = só simula
MEDIR_N        = int(os.environ.get("MEDIR_N", "1000"))                  # >0 = amostra de medição e para; 0 = passada inteira
RUN_ON_START   = os.environ.get("RUN_ON_START", "true").lower() == "true"
INTERVALO_HORAS = float(os.environ.get("INTERVALO_HORAS", "24"))

# Quais etapas ligar
FAZ_REGRAS     = os.environ.get("FAZ_REGRAS", "true").lower() == "true"  # regras fixas do Faxineiro
FAZ_FICHA      = os.environ.get("FAZ_FICHA", "true").lower() == "true"   # IA da ficha (Fichário)
HEALTH_MINIMO  = float(os.environ.get("HEALTH_MINIMO", "0.80"))          # ficha só se health < isto

# Ritmo / cobertura
PAUSA          = float(os.environ.get("PAUSA", "0.25"))
MAX_NOVOS_POR_CONTA = int(os.environ.get("MAX_NOVOS_POR_CONTA", "0"))    # round-robin: itens novos por conta por rodada (0 = sem limite)
STATUS_ALVO    = os.environ.get("STATUS_ALVO", "active")
WRITES_POR_SEG = float(os.environ.get("WRITES_POR_SEG", "1.5"))          # premissa p/ estimar dias de backfill (escrita não agrupa)

# Memória (Railway Volume em /data)
DATA_DIR        = os.environ.get("CHECKPOINT_DIR", "/data")
CHECKPOINT_FILE = os.path.join(DATA_DIR, "unico_checkpoint.txt")         # itens já tratados NESTA passada (retoma após restart)
SKU_FILE        = os.path.join(DATA_DIR, "unico_skus.json")              # conhecimento por SKU (persistente)
MEMORIA_DIAS    = float(os.environ.get("MEMORIA_DIAS", "30"))            # validade da ficha por SKU
RESET_CHECKPOINT = os.environ.get("RESET_CHECKPOINT", "false").lower() == "true"  # zera o checkpoint no início (one-shot)

# Relatório (reaproveita Telegram do Atendente)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RELATORIO        = os.environ.get("RELATORIO", "true").lower() == "true"

# Constantes de regra (iguais ao Faxineiro)
ORIGEM_CHINA = "96381"
GENUINAS = ["original", "genuin", "bosch", "denso", "keihin", "ngk", "valeo",
            "mahle", "sachs", "skf", "gates", "delphi", "magneti"]
CAMPOS_ALVO = {"BRAND", "MODEL", "PART_NUMBER", "VEHICLE_TYPE", "ORIGIN",
               "OEM", "IS_KIT", "ITEM_CONDITION", "NUMBER_OF_FANS"}

CONTAS_ML = [
    {"id": 60771984,  "nome": "INFINITY AUTOPARTS"},
    {"id": 233798434, "nome": "FREEDOM"},
    {"id": 554248644, "nome": "AUTOPARTSLIBERTY"},
    {"id": 1994875400,"nome": "DESTINYAUTOPARTS"},
]

SYSTEM_PROMPT = """Você é um especialista em autopeças preenchendo atributos de anúncios do Mercado Livre.
Recebe o TÍTULO do anúncio e uma lista de ATRIBUTOS FALTANTES (cada um com seus valores permitidos, quando houver).
Preencha SOMENTE os atributos que conseguir deduzir com CERTEZA ABSOLUTA a partir do título.

Regras ESTRITAS:
1. Na dúvida, NÃO preencha — é melhor deixar de fora do que errar.
2. Quando o atributo tiver lista de valores permitidos, use EXATAMENTE um desses nomes.
3. Condição do item: sempre "Novo" (todos os nossos produtos são novos).
4. NUNCA invente informação que não esteja no título.

Responda APENAS em JSON, sem texto fora dele:
{"atributos": [{"id": "ATRIBUTO_ID", "value_name": "VALOR"}]}
Se não tiver certeza de nenhum, responda: {"atributos": []}"""

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("RoboUnico")

_cache_categoria = {}
_aviso_volume = False


# ───────────────────────── API (MAC / tiops) ─────────────────────────
def mac(action, params=None, meli_user_id=None):
    # roteamento por conta vai DENTRO de params (no topo é ignorado -> token INFINITY)
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    payload = {"action": action, "params": params}
    headers = {"Content-Type": "application/json", "x-api-key": MAC_API_KEY}
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=60)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "error": r.text[:200]}
    except Exception as e:
        return {"status": 0, "error": str(e)}


def listar(cid):
    """Todos os ids da conta via scan/scroll (fura o teto de 1.000)."""
    ids, scroll_id, erros = [], None, 0
    total_logado = [False]
    while True:
        q = ["search_type=scan", "limit=100"]
        if STATUS_ALVO and STATUS_ALVO.lower() != "all":
            q.append(f"status={STATUS_ALVO}")
        if scroll_id:
            q.append(f"scroll_id={quote(scroll_id, safe='')}")
        p = f"/users/{cid}/items/search?" + "&".join(q)
        res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
        if res.get("status") != 200 or "data" not in res:
            erros += 1
            if erros >= 5:
                log.error(f"[{cid}] scan falhou 5x — parando esta conta. {res.get('error')}")
                break
            time.sleep(2); continue
        erros = 0
        data = res["data"]
        if not total_logado[0]:
            total_logado[0] = (data.get("paging") or {}).get("total")
        scroll_id = data.get("scroll_id") or scroll_id
        lote = data.get("results", [])
        if not lote:
            break
        ids += lote
        time.sleep(PAUSA)
    return ids, (total_logado[0] or len(ids))


def multiget(cid, bloco):
    """Detalhe de até 20 itens por chamada, com os campos que regra+ficha precisam."""
    campos = ("id,title,category_id,attributes,sale_terms,status,sub_status,"
              "tags,health,available_quantity,pictures,shipping")
    p = f"/items?ids={','.join(bloco)}&attributes={campos}"
    res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
    if res.get("status") != 200 or "data" not in res:
        return None
    out = []
    for entry in res["data"]:
        if entry.get("code") == 200 and entry.get("body"):
            out.append(entry["body"])
    return out


# ───────────────────────── Helpers comuns ─────────────────────────
def amap(item):
    return {a.get("id"): a for a in item.get("attributes", [])}

def vazio(a):
    if not a:
        return True
    return a.get("value_id") in (None, "-1", "") and (a.get("value_name") or "").strip() in ("", "33")

def sku_de(item, am):
    s = (am.get("SELLER_SKU") or {}).get("value_name")
    if s and s.strip():
        return s.strip()
    # sem SKU: cai pro título normalizado (ainda reaproveita entre anúncios iguais)
    return "t:" + re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())[:120]


# ───────────────────────── REGRAS FIXAS (Faxineiro) ─────────────────────────
def normaliza_codigos(txt):
    if not txt:
        return ""
    bruto = txt.replace("|", "/").replace(";", "/").replace(",", "/")
    partes, vistos = [], set()
    for tok in bruto.split("/"):
        t = tok.strip()
        if not t:
            continue
        k = re.sub(r"[^A-Z0-9]", "", t.upper())   # CDH210 == CDH-210
        if k and k not in vistos:
            vistos.add(k); partes.append(t)
    return ", ".join(partes)


def planeja(item):
    """Regras determinísticas. Retorna (attrs, sale_terms, alertas)."""
    am = amap(item)
    attrs, sale_terms, alertas = [], None, []
    if "INMETRO_CERTIFICATION_REGISTRATION_NUMBER" in am and vazio(am["INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]):
        attrs.append({"id": "INMETRO_CERTIFICATION_REGISTRATION_NUMBER", "value_name": "N/A"})
    if "ORIGIN" in am and vazio(am["ORIGIN"]):
        attrs.append({"id": "ORIGIN", "value_id": ORIGEM_CHINA})
    oem = (am.get("OEM") or {}).get("value_name") or ""
    pn  = (am.get("PART_NUMBER") or {}).get("value_name") or ""
    oem_n, pn_n = normaliza_codigos(oem), normaliza_codigos(pn)
    if not oem_n and pn_n: oem_n = pn_n
    if not pn_n and oem_n: pn_n = oem_n
    oem_final = (oem_n + ",") if (oem_n and not oem_n.endswith(",")) else oem_n
    if oem_final and oem_final != oem and "OEM" in am:
        attrs.append({"id": "OEM", "value_name": oem_final})
    if pn_n and pn_n != pn and "PART_NUMBER" in am:
        attrs.append({"id": "PART_NUMBER", "value_name": pn_n})
    st = item.get("sale_terms") or []
    if st:
        tipo = next((s for s in st if s.get("id") == "WARRANTY_TYPE"), None)
        precisa = not any(s.get("id") == "WARRANTY_TIME" and
                          (s.get("value_name") or "").strip().lower() == "90 dias" for s in st)
        if precisa:
            novo_st = [{"id": "WARRANTY_TIME", "value_name": "90 dias"}]
            if tipo:
                novo_st.append({"id": "WARRANTY_TYPE", "value_id": tipo.get("value_id"),
                                "value_name": tipo.get("value_name")})
            sale_terms = novo_st
    tl = (item.get("title") or "").lower()
    achadas = [g for g in GENUINAS if g in tl]
    if achadas:
        alertas.append("titulo:" + ",".join(achadas))
    return attrs, sale_terms, alertas


# ───────────────────────── FICHA (Fichário + IA) ─────────────────────────
def campos_da_categoria(category_id):
    if category_id in _cache_categoria:
        return _cache_categoria[category_id]
    res = mac("category_attributes", {"categoryId": category_id})
    if res.get("status") != 200 or not isinstance(res.get("data"), list):
        _cache_categoria[category_id] = []
        return []
    campos = []
    for a in res["data"]:
        tags = a.get("tags") or {}
        if tags.get("read_only") or tags.get("hidden"):
            continue
        if a.get("id") not in CAMPOS_ALVO:
            continue
        campos.append({"id": a.get("id"), "name": a.get("name", a.get("id")),
                       "value_type": a.get("value_type"), "values": a.get("values") or []})
    _cache_categoria[category_id] = campos
    return campos


def montar_attr(campo, value_name):
    attr = {"id": campo["id"], "value_name": value_name}
    if campo["values"]:
        for v in campo["values"]:
            if (v.get("name") or "").strip().lower() == (value_name or "").strip().lower():
                attr["value_id"] = v.get("id"); attr["value_name"] = v.get("name")
                return attr
        return None  # valor fora dos permitidos → descarta
    return attr


def analisar_com_claude(titulo, faltantes):
    linhas = []
    for f in faltantes:
        if f["values"]:
            permitidos = ", ".join(v.get("name", "") for v in f["values"])
            linhas.append(f"- {f['id']} ({f['name']}) — valores permitidos: {permitidos}")
        else:
            linhas.append(f"- {f['id']} ({f['name']}) — texto livre")
    body = {"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user",
                          "content": f"TÍTULO: {titulo}\n\nATRIBUTOS FALTANTES:\n" + "\n".join(linhas)}]}
    headers = {"Content-Type": "application/json", "x-api-key": CLAUDE_API_KEY,
               "anthropic-version": "2023-06-01"}
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=40)
        data = r.json()
        if data.get("content"):
            texto = data["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
            ini = texto.find("{")
            if ini > 0:
                texto = texto[ini:]
            try:
                return json.loads(texto).get("atributos", [])
            except json.JSONDecodeError:
                obj, _ = json.JSONDecoder().raw_decode(texto)
                return obj.get("atributos", [])
    except Exception as e:
        log.error(f"Erro Claude: {e}")
    return []


def _det_fill(campo, titulo):
    """Preenchimentos determinísticos (sem IA): condição, lista única, é-kit."""
    if campo["id"] == "ITEM_CONDITION":
        return montar_attr(campo, "Novo")
    if campo["value_type"] == "list" and len(campo["values"]) == 1:
        return montar_attr(campo, campo["values"][0].get("name"))
    if campo["id"] == "IS_KIT":
        ehkit = any(k in titulo.lower() for k in ["kit", "par", "jogo", "conjunto", "c/ 4", "c/4"])
        return montar_attr(campo, "Sim" if ehkit else "Não")
    return None


def derivar_ficha_sku(sku, titulo, abertos, skus):
    """Deriva (via IA) os atributos abertos de um PRODUTO. Cacheia por SKU.
    Retorna lista [{id, value_name, value_id?}]. Conta se foi IA nova ou reuso."""
    reg = skus.get(sku)
    if reg and reg.get("ts"):
        idade = (datetime.now(timezone.utc) - datetime.fromisoformat(reg["ts"])).days
        if idade <= MEMORIA_DIAS:
            return reg.get("attrs", []), False   # reuso
    # deriva tudo de uma vez pra este produto (cache cobre todos os anúncios do SKU)
    derivados = []
    for sug in analisar_com_claude(titulo, abertos):
        campo = next((c for c in abertos if c["id"] == sug.get("id")), None)
        if campo:
            a = montar_attr(campo, sug.get("value_name"))
            if a:
                derivados.append(a)
    skus[sku] = {"ts": datetime.now(timezone.utc).isoformat(), "attrs": derivados}
    return derivados, True   # IA nova


def ficha_do_item(item, sku, skus, contador):
    """Atributos de ficha que FALTAM neste anúncio (determinísticos + IA por SKU)."""
    cat = item.get("category_id")
    if not cat:
        return []
    campos = campos_da_categoria(cat)
    if not campos:
        return []
    am = amap(item)
    ja_tem = {aid for aid, a in am.items() if (a.get("value_name") or "") not in ("", "null", "N/A")}
    faltantes = [c for c in campos if c["id"] not in ja_tem]
    if not faltantes:
        return []

    novos, abertos = [], []
    titulo = item.get("title", "")
    for c in faltantes:
        a = _det_fill(c, titulo)
        if a is not None:
            novos.append(a)
        else:
            abertos.append(c)

    if abertos:
        derivados, foi_ia = derivar_ficha_sku(sku, titulo, abertos, skus)
        contador["ia_novas" if foi_ia else "ia_reuso"] += 1
        falt_ids = {c["id"] for c in abertos}
        for a in derivados:
            if a["id"] in falt_ids:
                novos.append(a)
    return novos


# ───────────────────────── Escrita (1 PUT por item) ─────────────────────────
def aplica(cid, iid, attrs, sale_terms):
    body = {}
    if attrs: body["attributes"] = attrs
    if sale_terms: body["sale_terms"] = sale_terms
    if not body:
        return None
    r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body}, meli_user_id=cid)
    if r and r.get("status") not in (200, 201) and attrs:
        # tenta de novo sem o INMETRO (campo que às vezes trava)
        sem = [a for a in attrs if a.get("id") != "INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]
        if sem != attrs and (sem or sale_terms):
            body2 = {}
            if sem: body2["attributes"] = sem
            if sale_terms: body2["sale_terms"] = sale_terms
            r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body2}, meli_user_id=cid)
    return r


# ───────────────────────── Memória (arquivos) ─────────────────────────
def _avisa_volume(e):
    global _aviso_volume
    if not _aviso_volume:
        log.warning(f"🧠 Sem persistência em {DATA_DIR} ({e}). Monte um Volume em {DATA_DIR}.")
        _aviso_volume = True

def carregar_checkpoint():
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(l.strip() for l in f if l.strip())
    except Exception:
        return set()

def marcar_feito(chave):
    try:
        with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
            f.write(chave + "\n")
    except Exception as e:
        _avisa_volume(e)

def limpar_checkpoint():
    try:
        os.remove(CHECKPOINT_FILE)
    except Exception:
        pass

def carregar_skus():
    try:
        with open(SKU_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def salvar_skus(skus):
    try:
        tmp = SKU_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(skus, f, ensure_ascii=False)
        os.replace(tmp, SKU_FILE)
    except Exception as e:
        _avisa_volume(e)


# ───────────────────────── Relatório ─────────────────────────
def telegram(msg):
    if not (RELATORIO and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log.warning(f"Telegram não enviado: {e}")


# ───────────────────────── Passada ─────────────────────────
def run_once():
    modo = "DRY-RUN (só simula)" if DRY_RUN else "APLICANDO (escrita real)"
    medindo = MEDIR_N > 0
    log.info(f"🤝 Robô Único — {modo} | regras={FAZ_REGRAS} ficha={FAZ_FICHA} "
             f"| {'MEDINDO ' + str(MEDIR_N) + ' itens' if medindo else 'passada inteira'}")

    done = carregar_checkpoint()
    skus = carregar_skus()
    _reset_marker = os.path.join(DATA_DIR, "reset_done.flag")
    if RESET_CHECKPOINT and not os.path.exists(_reset_marker):
        limpar_checkpoint(); done = set()
        try: open(_reset_marker, "w").close()
        except Exception: pass
        log.info("🧠 RESET_CHECKPOINT — checkpoint zerado UMA vez (restarts daqui pra frente retomam).")
    if done:  log.info(f"🧠 Checkpoint: {len(done)} itens já tratados nesta passada.")
    if skus:  log.info(f"🧠 Memória SKU: {len(skus)} produtos já conhecidos.")

    tot = {"itens": 0, "precisa_escrita": 0, "regras": 0, "ficha": 0, "escritos": 0,
           "erros": 0, "pulados": 0, "titulo_alerta": 0, "ia_novas": 0, "ia_reuso": 0}
    por_conta = {}

    # 1) scan dos ids de todas as contas
    fila = []
    total_geral = 0
    for c in CONTAS_ML:
        log.info(f"[{c['nome']}] varrendo ids...")
        ids, total = listar(c["id"])
        log.info(f"[{c['nome']}] {len(ids)} ids ({total} no total da conta).")
        fila.append({"c": c, "ids": ids, "pos": 0})
        por_conta[c["nome"]] = {"total": total, "vistos": 0, "escritas": 0}
        total_geral += total

    # 2) round-robin: N novos por conta por rodada (default = conta inteira por vez)
    chunk = MAX_NOVOS_POR_CONTA or (50 if medindo else 10**9)
    t0 = time.time()
    parar = False
    restam = True
    while restam and not parar:
        restam = False
        for f in fila:
            if parar: break
            c, ids = f["c"], f["ids"]
            feitos = 0
            while f["pos"] < len(ids) and feitos < chunk:
                # busca o próximo bloco de até 20 ids ainda não tratados
                bloco, idxs = [], []
                while f["pos"] < len(ids) and len(bloco) < 20:
                    iid = ids[f["pos"]]; f["pos"] += 1
                    if f"{c['id']}:{iid}" in done:
                        tot["pulados"] += 1; continue
                    bloco.append(iid)
                if not bloco:
                    break
                itens = multiget(c["id"], bloco)
                if itens is None:
                    tot["erros"] += 1; time.sleep(1); continue

                for item in itens:
                    iid = item.get("id")
                    chave = f"{c['id']}:{iid}"
                    tot["itens"] += 1; por_conta[c["nome"]]["vistos"] += 1; feitos += 1
                    am = amap(item)
                    sku = sku_de(item, am)

                    attrs, sale_terms = [], None
                    # --- regras fixas ---
                    if FAZ_REGRAS:
                        a, st, alertas = planeja(item)
                        attrs += a; sale_terms = st
                        if a or st: tot["regras"] += 1
                        if alertas: tot["titulo_alerta"] += 1
                    # --- ficha (IA por SKU), só p/ health baixo ---
                    if FAZ_FICHA:
                        health = item.get("health")
                        if health is None or health < HEALTH_MINIMO:
                            fa = ficha_do_item(item, sku, skus, tot)
                            if fa:
                                # mescla sem duplicar id (regra tem prioridade)
                                ja = {x["id"] for x in attrs}
                                attrs += [x for x in fa if x["id"] not in ja]
                                tot["ficha"] += 1

                    if attrs or sale_terms:
                        tot["precisa_escrita"] += 1
                        if not DRY_RUN:
                            r = aplica(c["id"], iid, attrs, sale_terms)
                            if r and r.get("status") in (200, 201):
                                tot["escritos"] += 1; por_conta[c["nome"]]["escritas"] += 1
                                log.info(f"  ✅ [{c['nome']}] {iid} | {[a['id'] for a in attrs]}"
                                         f"{' +garantia90d' if sale_terms else ''}")
                            else:
                                tot["erros"] += 1
                                log.warning(f"  ❌ [{c['nome']}] falha {iid}: {(r or {}).get('data') or (r or {}).get('error')}")
                            time.sleep(PAUSA)

                    done.add(chave)
                    if not DRY_RUN:
                        marcar_feito(chave)   # em DRY_RUN não suja o checkpoint
                    if tot["itens"] % 50 == 0:
                        log.info(f"  {tot['itens']} itens | precisam_ajuste:{tot['precisa_escrita']} "
                                 f"| gravados:{tot['escritos']} falhas:{tot['erros']} "
                                 f"| IA nova:{tot['ia_novas']} reuso:{tot['ia_reuso']}")
                    if medindo and tot["itens"] >= MEDIR_N:
                        parar = True; break
                if parar: break
                salvar_skus(skus)   # salva o aprendizado em lotes
            if f["pos"] < len(ids):
                restam = True

    salvar_skus(skus)
    dt = max(time.time() - t0, 0.001)
    taxa = tot["itens"] / dt

    if medindo:
        relatorio_medicao(tot, total_geral, taxa, len(skus))
    else:
        limpar_checkpoint()   # passada completa: zera p/ re-checar amanhã (memória SKU PERSISTE)
        resumo = (f"🤝 <b>Robô Único — passada concluída</b>\n{tot}\n"
                  + "\n".join(f"• {n}: {d['vistos']} vistos, {d['escritas']} escritas" for n, d in por_conta.items()))
        log.info(f"🤝 Passada concluída. {tot}")
        telegram(resumo)


def relatorio_medicao(tot, total_geral, taxa, skus_conhecidos):
    n = max(tot["itens"], 1)
    frac_escrita = tot["precisa_escrita"] / n
    est_escritas = int(total_geral * frac_escrita)
    # IA: chamadas novas tendem a CAIR conforme o cache enche; isto é teto (pessimista)
    frac_ia = tot["ia_novas"] / n
    est_ia = int(total_geral * frac_ia)
    # backfill: escrita é o gargalo (1 PUT por item, ~WRITES_POR_SEG/s)
    seg_escrita = est_escritas / max(WRITES_POR_SEG, 0.1)
    dias_escrita = seg_escrita / 86400
    # ao ritmo medido (inclui IA fria = pior caso)
    seg_taxa = total_geral / max(taxa, 0.001)
    dias_taxa = seg_taxa / 86400

    msg = (
        "📏 <b>MEDIÇÃO — Robô Único</b>\n"
        f"Amostra: <b>{n}</b> itens em {tot['itens']/max(taxa,0.001):.0f}s "
        f"(<b>{taxa:.2f} itens/s</b>)\n"
        f"Acervo total (4 contas): <b>{total_geral:,}</b>\n\n"
        f"• Precisam de escrita: {tot['precisa_escrita']}/{n} = <b>{frac_escrita*100:.0f}%</b>\n"
        f"   - por regra fixa: {tot['regras']} | por ficha (IA): {tot['ficha']}\n"
        f"• IA: {tot['ia_novas']} novas / {tot['ia_reuso']} reuso "
        f"(reuso = SKU já conhecido)\n"
        f"• Alertas de título 'original': {tot['titulo_alerta']}\n\n"
        f"📦 <b>Estimativa do backfill (uma vez só):</b>\n"
        f"• Escritas previstas: ~<b>{est_escritas:,}</b>\n"
        f"• Chamadas de IA (teto): ~{est_ia:,}\n"
        f"• Tempo p/ as escritas (~{WRITES_POR_SEG}/s): ~<b>{dias_escrita:.1f} dias</b>\n"
        f"• Ao ritmo medido (IA fria, pior caso): ~{dias_taxa:.1f} dias\n"
        f"   → na prática fica ENTRE os dois (o cache de SKU acelera ao longo do tempo)\n\n"
        + (f"DRY_RUN=true — nada foi escrito (simulação pura).\n"
           f"Pra valer: DRY_RUN=false e MEDIR_N=0."
           if DRY_RUN else
           f"⚠️ ESCRITA REAL feita nesta amostra: <b>{tot['escritos']} gravados, {tot['erros']} falhas</b>.\n"
           f"Backfill total: DRY_RUN=false e MEDIR_N=0 (+ RESET_CHECKPOINT=true uma vez).")
    )
    log.info("📏 MEDIÇÃO:\n" + re.sub(r"<[^>]+>", "", msg))
    telegram(msg)


def main():
    if FAZ_FICHA and not CLAUDE_API_KEY:
        log.warning("⚠️ CLAUDE_API_KEY não configurada — a ficha (IA) vai ficar vazia. Regras fixas seguem normais.")
    if RUN_ON_START:
        run_once()
    if MEDIR_N > 0:
        log.info("📏 Medição concluída — não entro em loop. Ajuste DRY_RUN/MEDIR_N e suba de novo.")
        return
    if INTERVALO_HORAS > 0:
        while True:
            log.info(f"😴 Dormindo {INTERVALO_HORAS}h até a próxima passada...")
            time.sleep(INTERVALO_HORAS * 3600)
            run_once()


if __name__ == "__main__":
    main()

