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

SEGURANÇA: APLICAR=false (DRY-RUN) por padrão; só escreve com APLICAR=true.
É idempotente: só mexe onde realmente há correção; re-passar num anúncio já certo não muda nada.
Tudo por merge (PUT) — não apaga nada, não toca em compatibilidade nem foto.
"""
import os, time, logging, requests

MAC_API_KEY = os.environ.get("MAC_API_KEY", "")
MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"

APLICAR        = os.environ.get("APLICAR", "false").lower() == "true"
MODO           = os.environ.get("MODO", "full").lower()          # full | incremental
STATUS_ALVO    = os.environ.get("STATUS_ALVO", "active")          # active | paused
MAX_ITENS      = int(os.environ.get("MAX_ITENS", "0"))            # 0 = todos (full) / 500 (incremental)
INTERVALO_DIAS = float(os.environ.get("INTERVALO_DIAS", "0"))     # 0 = roda 1x
PAUSA          = float(os.environ.get("PAUSA", "0.25"))

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


def listar(cid):
    sort = "start_time_desc" if MODO == "incremental" else "stop_time_asc"
    cap = MAX_ITENS if MAX_ITENS else (500 if MODO == "incremental" else 0)
    ids, offset = [], 0
    while True:
        p = f"/users/{cid}/items/search?status={STATUS_ALVO}&sort={sort}&limit=50&offset={offset}"
        res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
        if res.get("status") != 200:
            break
        lote = (res.get("data") or {}).get("results", [])
        if not lote:
            break
        ids += lote
        total = ((res.get("data") or {}).get("paging") or {}).get("total", 0)
        offset += 50
        if cap and len(ids) >= cap:
            return ids[:cap]
        if offset >= total:
            break
        time.sleep(PAUSA)
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
        if t and t.upper() not in vistos:
            vistos.add(t.upper()); partes.append(t)
    return ", ".join(partes)


def planeja(item):
    am = amap(item)
    novos_attrs, sale_terms, alertas = [], None, []

    if "INMETRO_CERTIFICATION_REGISTRATION_NUMBER" in am and vazio(am["INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]):
        novos_attrs.append({"id": "INMETRO_CERTIFICATION_REGISTRATION_NUMBER", "value_id": INMETRO_NA})

    if "ORIGIN" in am and vazio(am["ORIGIN"]):
        novos_attrs.append({"id": "ORIGIN", "value_id": ORIGEM_CHINA})

    oem = (am.get("OEM") or {}).get("value_name") or ""
    pn  = (am.get("PART_NUMBER") or {}).get("value_name") or ""
    oem_n, pn_n = normaliza_codigos(oem), normaliza_codigos(pn)
    if not oem_n and pn_n:
        oem_n = pn_n
    if not pn_n and oem_n:
        pn_n = oem_n
    if oem_n and oem_n != oem and "OEM" in am:
        novos_attrs.append({"id": "OEM", "value_name": oem_n})
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
    return mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body}, meli_user_id=cid)


def run_once():
    modo_txt = "APLICANDO (escrita real)" if APLICAR else "DRY-RUN (só simula)"
    log.info(f"🤖 Funcionário Digital — modo={MODO} | status={STATUS_ALVO} | {modo_txt}")
    tot = {"itens": 0, "inmetro": 0, "origem": 0, "codigos": 0, "garantia": 0,
           "titulo_alerta": 0, "escritos": 0, "erros": 0}
    for c in CONTAS_ML:
        ids = listar(c["id"])
        log.info(f"[{c['nome']}] {len(ids)} anúncios ({STATUS_ALVO}, {MODO}).")
        for i, iid in enumerate(ids, 1):
            item = get_item(c["id"], iid)
            if not item:
                tot["erros"] += 1; continue
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
            if i % 50 == 0:
                log.info(f"[{c['nome']}] {i}/{len(ids)}...")
            time.sleep(PAUSA)
    log.info(f"🤖 Passada concluída. {tot}")


def main():
    if not MAC_API_KEY:
        log.error("MAC_API_KEY ausente"); return
    run_once()
    while INTERVALO_DIAS > 0:
        log.info(f"😴 Dormindo {INTERVALO_DIAS} dia(s) até a próxima varredura...")
        time.sleep(INTERVALO_DIAS * 86400)
        run_once()


if __name__ == "__main__":
    main()
