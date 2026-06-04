"""
Funcionário Digital — Infinity Bot
Correções SEGURAS e determinísticas (sem IA, sem adivinhação), por anúncio:
  1) GARANTIA  -> "90 dias" PRESERVANDO o tipo (fábrica/vendedor)
  2) INMETRO   -> "N/A" (33966573) onde está vazio/inválido (respeita número real)
  3) ORIGEM    -> "China" (96381) onde está vazia
  4) OEM x Nº DE PEÇA -> normaliza pra VÍRGULA e sincroniza (copia se um está vazio)
  5) TÍTULO    -> só SINALIZA palavra de peça genuína (não reescreve)

MODOS:
  MODO=full         -> varre TODOS (status escolhido). Use na 1ª passada.
  MODO=incremental  -> varre só os MAIS NOVOS (cap MAX_ITENS, padrão 500). Use no recorrente.

AGENDADOR:
  INTERVALO_DIAS=0  -> roda uma vez e encerra.
  INTERVALO_DIAS=2  -> roda, dorme 2 dias, repete (fica ligado).

MEMÓRIA (NOVO):
  Anota os itens já tratados num arquivo em CHECKPOINT_DIR (padrão /data).
  Se reiniciar no meio da passada, RETOMA de onde parou em vez de recomeçar do zero.
  Ao terminar a passada completa, limpa a memória (a próxima varredura começa fresca).
  ⚠️ Precisa de um Volume do Railway montado em /data pra sobreviver a redeploys.

SEGURANÇA: APLICAR=false (DRY-RUN) por padrão; só escreve com APLICAR=true.
É idempotente: só mexe onde realmente há correção; re-passar num anúncio já certo não muda nada.
Tudo por merge (PUT) — não apaga nada, não toca em compatibilidade nem foto.
"""
import os, re, time, logging, requests
from urllib.parse import quote

MAC_API_KEY = os.environ.get("MAC_API_KEY", "")
MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"

APLICAR        = os.environ.get("APLICAR", "false").lower() == "true"
MODO           = os.environ.get("MODO", "full").lower()          # full | incremental
STATUS_ALVO    = os.environ.get("STATUS_ALVO", "active")          # active | paused | all
SUB_STATUS     = os.environ.get("SUB_STATUS", "").strip()         # ex.: waiting_for_patch (vazio = todos)
MAX_ITENS      = int(os.environ.get("MAX_ITENS", "0"))            # 0 = todos (full) / 500 (incremental)
INTERVALO_DIAS = float(os.environ.get("INTERVALO_DIAS", "0"))     # 0 = roda 1x
PAUSA          = float(os.environ.get("PAUSA", "0.25"))

# 🧠 MEMÓRIA — checkpoint que sobrevive a reinício (se houver Volume em /data)
CHECKPOINT_DIR  = os.environ.get("CHECKPOINT_DIR", "/data")
CHECKPOINT_FILE = os.path.join(CHECKPOINT_DIR, "faxineiro_checkpoint.txt")
_aviso_volume = False

INMETRO_NA   = "33966573"
ORIGEM_CHINA = "96381"
GENUINAS = ["original", "genuin", "bosch", "denso", "keihin", "ngk", "valeo",
            "mahle", "sachs", "skf", "gates", "delphi", "magneti"]

CONTAS_ML = [
    {"id": 60771984,  "nome": "INFINITY AUTOPARTS"},
    {"id": 233798434, "nome": "FREEDOM"},
    {"id": 554248644, "nome": "AUTOPARTSLIBERTY"},
    {"id": 1994875400,"nome": "DESTINYAUTOPARTS"},
]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("FuncDigital")


# 🧠 ───────── MEMÓRIA (checkpoint) ─────────
def carregar_checkpoint():
    """Lê os itens já tratados nesta passada. Vazio se não existir."""
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(l.strip() for l in f if l.strip())
    except Exception:
        return set()


def marcar_feito(chave):
    """Anexa 1 item à memória (append — barato). Avisa 1x se não der pra gravar."""
    global _aviso_volume
    try:
        with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
            f.write(chave + "\n")
    except Exception as e:
        if not _aviso_volume:
            log.warning(f"🧠 Sem persistência em {CHECKPOINT_FILE} ({e}). "
                        f"A memória só sobrevive a reinício se houver um Volume montado em {CHECKPOINT_DIR}.")
            _aviso_volume = True


def limpar_checkpoint():
    """Apaga a memória ao terminar a passada completa (a próxima começa fresca)."""
    try:
        os.remove(CHECKPOINT_FILE)
    except Exception:
        pass


def mac(action, params=None, meli_user_id=None):
    payload = {"action": action, "params": params or {}}
    if meli_user_id:
        payload["meli_user_id"] = meli_user_id
    try:
        r = requests.post(MAC_BASE_URL, json=payload,
                          headers={"Content-Type": "application/json", "x-api-key": MAC_API_KEY},
                          timeout=40)
        return r.json()
    except Exception as e:
        return {"status": 0, "error": str(e)}


def listar_via_action(cid):
    """Plano B: usa a AÇÃO list_items do conector (que rota multi-conta) quando o scan volta vazio.
    Limitada a ~1000 por conta (teto de offset da ML), mas alcança contas que o scan não pega."""
    ids, offset = [], 0
    while offset < 1000:
        res = mac("list_items", {"limit": 50, "offset": offset, "status": (STATUS_ALVO or "active")}, meli_user_id=cid)
        if res.get("status") != 200:
            break
        items = (res.get("data") or {}).get("items") or []
        if not items:
            break
        ids += [it.get("id") for it in items if it.get("id")]
        offset += 50
        time.sleep(PAUSA)
    return ids


def listar(cid):
    cap = MAX_ITENS if MAX_ITENS else (500 if MODO == "incremental" else 0)
    ids = []

    if MODO == "incremental":
        # Mais novos primeiro (offset). A ML limita offset a ~1000 — ok pro incremental.
        offset = 0
        while True:
            q = ["sort=start_time_desc", "limit=50", f"offset={offset}"]
            if STATUS_ALVO and STATUS_ALVO.lower() != "all":
                q.append(f"status={STATUS_ALVO}")
            if SUB_STATUS:
                q.append(f"sub_status={SUB_STATUS}")
            p = f"/users/{cid}/items/search?" + "&".join(q)
            res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
            if res.get("status") != 200:
                break
            lote = (res.get("data") or {}).get("results", [])
            if not lote:
                break
            ids += lote
            offset += 50
            if cap and len(ids) >= cap:
                return ids[:cap]
            if offset >= 1000:   # teto duro de offset da ML
                break
            time.sleep(PAUSA)
        return ids

    # MODO full: search_type=scan (scroll) FURA o teto de 1000 e pega TODOS os anúncios.
    scroll_id = None
    while True:
        q = ["search_type=scan", "limit=100"]
        if STATUS_ALVO and STATUS_ALVO.lower() != "all":
            q.append(f"status={STATUS_ALVO}")
        if SUB_STATUS:
            q.append(f"sub_status={SUB_STATUS}")
        if scroll_id:
            q.append(f"scroll_id={quote(scroll_id, safe='')}")
        p = f"/users/{cid}/items/search?" + "&".join(q)
        res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
        if res.get("status") != 200:
            break
        data = res.get("data") or {}
        scroll_id = data.get("scroll_id") or scroll_id
        lote = data.get("results", [])
        if not lote:
            break
        ids += lote
        if cap and len(ids) >= cap:
            return ids[:cap]
        time.sleep(PAUSA)

    if not ids:
        # O scan não alcançou esta conta → tenta pela ação list_items (rota multi-conta).
        fb = listar_via_action(cid)
        if fb:
            log.info(f"[{cid}] scan vazio — plano B (list_items) pegou {len(fb)} anúncios.")
        return fb
    return ids


def get_item(cid, iid):
    p = f"/items/{iid}?attributes=id,title,attributes,sale_terms,status"
    res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
    return res.get("data") if res.get("status") == 200 else None


def amap(item):
    return {a.get("id"): a for a in item.get("attributes", [])}


def vazio(a):
    if not a:
        return True
    return a.get("value_id") in (None, "-1", "") and (a.get("value_name") or "").strip() in ("", "33")


def normaliza_codigos(txt):
    if not txt:
        return ""
    bruto = txt.replace("|", "/").replace(";", "/").replace(",", "/")
    partes, vistos = [], set()
    for tok in bruto.split("/"):
        t = tok.strip()
        if not t:
            continue
        # Chave ignora hífen/espaço/ponto e maiúsculas → CDH210 == CDH-210 (o ML trata como duplicado)
        k = re.sub(r"[^A-Z0-9]", "", t.upper())
        if k and k not in vistos:
            vistos.add(k); partes.append(t)
    return ", ".join(partes)


def planeja(item):
    am = amap(item)
    novos_attrs, sale_terms, alertas = [], None, []

    if "INMETRO_CERTIFICATION_REGISTRATION_NUMBER" in am and vazio(am["INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]):
        novos_attrs.append({"id": "INMETRO_CERTIFICATION_REGISTRATION_NUMBER", "value_name": "N/A"})

    if "ORIGIN" in am and vazio(am["ORIGIN"]):
        novos_attrs.append({"id": "ORIGIN", "value_id": ORIGEM_CHINA})

    oem = (am.get("OEM") or {}).get("value_name") or ""
    pn  = (am.get("PART_NUMBER") or {}).get("value_name") or ""
    oem_n, pn_n = normaliza_codigos(oem), normaliza_codigos(pn)
    if not oem_n and pn_n:
        oem_n = pn_n
    if not pn_n and oem_n:
        pn_n = oem_n
    # OEM tem que terminar com vírgula pra subir como código unitário no ML
    oem_final = (oem_n + ",") if (oem_n and not oem_n.endswith(",")) else oem_n
    if oem_final and oem_final != oem and "OEM" in am:
        novos_attrs.append({"id": "OEM", "value_name": oem_final})
    if pn_n and pn_n != pn and "PART_NUMBER" in am:
        novos_attrs.append({"id": "PART_NUMBER", "value_name": pn_n})

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

    return novos_attrs, sale_terms, alertas


def aplica(cid, iid, attrs, sale_terms):
    body = {}
    if attrs:
        body["attributes"] = attrs
    if sale_terms:
        body["sale_terms"] = sale_terms
    if not body:
        return None
    r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body}, meli_user_id=cid)
    # Se falhou e o INMETRO estava no meio, tenta DE NOVO sem o INMETRO —
    # pra um INMETRO problemático não derrubar as correções boas (OEM, Nº de peça, etc).
    if r and r.get("status") != 200 and attrs:
        sem_inmetro = [a for a in attrs if a.get("id") != "INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]
        if sem_inmetro != attrs and (sem_inmetro or sale_terms):
            body2 = {}
            if sem_inmetro:
                body2["attributes"] = sem_inmetro
            if sale_terms:
                body2["sale_terms"] = sale_terms
            r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body2}, meli_user_id=cid)
    return r


def run_once():
    modo_txt = "APLICANDO (escrita real)" if APLICAR else "DRY-RUN (só simula)"
    alvo = f"{STATUS_ALVO}" + (f"/{SUB_STATUS}" if SUB_STATUS else "")
    log.info(f"🤖 Funcionário Digital — modo={MODO} | alvo={alvo} | {modo_txt}")

    done = carregar_checkpoint()                       # 🧠 retoma de onde parou
    if done:
        log.info(f"🧠 Memória: retomando — {len(done)} itens já tratados serão pulados.")

    tot = {"itens": 0, "inmetro": 0, "origem": 0, "codigos": 0, "garantia": 0,
           "titulo_alerta": 0, "escritos": 0, "erros": 0, "pulados": 0}
    for c in CONTAS_ML:
        ids = listar(c["id"])
        log.info(f"[{c['nome']}] {len(ids)} anúncios ({STATUS_ALVO}, {MODO}).")
        for i, iid in enumerate(ids, 1):
            chave = f"{c['id']}:{iid}"
            if chave in done:                          # 🧠 já tratado nesta passada → pula
                tot["pulados"] += 1
                continue
            item = get_item(c["id"], iid)
            if not item:
                tot["erros"] += 1
                continue                               # não marca: tenta de novo num próximo resume
            tot["itens"] += 1
            attrs, sale_terms, alertas = planeja(item)
            for a in attrs:
                if a["id"] == "INMETRO_CERTIFICATION_REGISTRATION_NUMBER": tot["inmetro"] += 1
                if a["id"] == "ORIGIN": tot["origem"] += 1
                if a["id"] in ("OEM", "PART_NUMBER"): tot["codigos"] += 1
            if sale_terms: tot["garantia"] += 1
            if alertas: tot["titulo_alerta"] += 1
            if attrs or sale_terms:
                resumo = ", ".join([a["id"] for a in attrs] + (["GARANTIA"] if sale_terms else []))
                log.info(f"  {iid}: {resumo}" + (f" | ALERTA {alertas}" if alertas else ""))
                if APLICAR:
                    r = aplica(c["id"], iid, attrs, sale_terms)
                    if r and r.get("status") == 200:
                        tot["escritos"] += 1
                    else:
                        tot["erros"] += 1
                        log.warning(f"    falha: {(r or {}).get('data')}")
            done.add(chave); marcar_feito(chave)       # 🧠 marca como tratado (sobrevive a reinício)
            if i % 50 == 0:
                log.info(f"[{c['nome']}] {i}/{len(ids)}...")
            time.sleep(PAUSA)
    limpar_checkpoint()                                # 🧠 passada completa → zera a memória
    log.info(f"🤖 Passada concluída. {tot}")


def main():
    if not MAC_API_KEY:
        log.error("MAC_API_KEY ausente"); return
    try:
        os.makedirs(CHECKPOINT_DIR, exist_ok=True)     # 🧠 garante a pasta da memória
    except Exception:
        pass
    run_once()
    while INTERVALO_DIAS > 0:
        log.info(f"😴 Dormindo {INTERVALO_DIAS} dia(s) até a próxima varredura...")
        time.sleep(INTERVALO_DIAS * 86400)
        run_once()


if __name__ == "__main__":
    main()
