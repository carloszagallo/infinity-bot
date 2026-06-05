"""
CORRETOR FAXINEIRO — Infinity (antigo funcionario_digital)
Correções SEGURAS e determinísticas (sem IA, sem adivinhação) + RELATÓRIO diário.

CORREÇÕES (já existiam, mantidas):
  1) GARANTIA  -> "90 dias" PRESERVANDO o tipo (fábrica/vendedor)
  2) INMETRO   -> "N/A" onde está vazio/inválido
  3) ORIGEM    -> "China" onde está vazia
  4) OEM x Nº DE PEÇA -> normaliza pra VÍRGULA e sincroniza
  5) TÍTULO    -> só SINALIZA palavra de peça genuína (não reescreve)

NOVO NA v2:
  6) RELATÓRIO diário no TELEGRAM + EMAIL (read-only, seguro):
        - precisa_foto     (saúde < HEALTH_MIN ou foto < 1200px)
        - repor_ja         (estoque não dura COVERAGE_DAYS dias pelo giro de SALES_WINDOW)
        - revisao_humana   (pausado por moderação/duplicado/ficha travada — NÃO estoque)
        - compat_faltando  (peça com compatibilidade ausente/incompleta)
        - migrar_full      (vende bem e está fora do Full)
  7) COMPATIBILIDADE — copia de um IRMÃO de mesmo SKU completo (sua dor nº1).
        ⚠️ DESLIGADA por padrão (COMPAT_APLICAR=false). Enquanto off, só LISTA candidatos
        no relatório. Só escreve quando COMPAT_APLICAR=true E com guardas de segurança
        (mesmo SKU, fonte com compat, e SEM moderation_penalty no alvo nem na fonte).

SEGURANÇA:
  - APLICAR=false (DRY-RUN) por padrão -> não escreve correção nenhuma.
  - COMPAT_APLICAR=false por padrão -> não escreve compatibilidade.
  - Idempotente, por merge (PUT). Nunca toca em foto. Nunca contorna moderação.
  - Relatório é independente e seguro (só leitura). Falha de envio não derruba a rodada.
"""
# DEPLOY STAMP: 2026-06-05 — fix de roteamento por conta (meli_user_id dentro de params).
# Este comentário existe pra forçar o watch path do Railway a rebuildar este serviço.
import os, io, re, csv, time, json, smtplib, logging, requests
from urllib.parse import quote
from email.message import EmailMessage

MAC_API_KEY  = os.environ.get("MAC_API_KEY", "")
MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"

APLICAR        = os.environ.get("APLICAR", "false").lower() == "true"
COMPAT_APLICAR = os.environ.get("COMPAT_APLICAR", "false").lower() == "true"   # escrita de compat
RELATORIO      = os.environ.get("RELATORIO", "true").lower() == "true"         # envia Telegram/email
REPOR_JA       = os.environ.get("REPOR_JA", "false").lower() == "true"         # liga a passada (pesada) de pedidos p/ 'repor já'
MODO           = os.environ.get("MODO", "full").lower()          # full | incremental
STATUS_ALVO    = os.environ.get("STATUS_ALVO", "active")          # active | paused | all
SUB_STATUS     = os.environ.get("SUB_STATUS", "").strip()
MAX_ITENS      = int(os.environ.get("MAX_ITENS", "0"))
INTERVALO_DIAS = float(os.environ.get("INTERVALO_DIAS", "0"))
PAUSA          = float(os.environ.get("PAUSA", "0.25"))

# Parâmetros do relatório (validados com você)
HEALTH_MIN    = float(os.environ.get("HEALTH_MIN", "0.70"))
COVERAGE_DAYS = int(os.environ.get("COVERAGE_DAYS", "30"))
SALES_WINDOW  = int(os.environ.get("SALES_WINDOW", "60"))
MIN_PHOTO_PX  = 1200

# Avisos (Telegram/email) — preencha nas variáveis do Railway
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_USER       = os.environ.get("EMAIL_USER", "")
EMAIL_PASS       = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO         = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

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
log = logging.getLogger("CorretorFaxineiro")


# ───────── MEMÓRIA (checkpoint) ─────────
def carregar_checkpoint():
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(l.strip() for l in f if l.strip())
    except Exception:
        return set()

def marcar_feito(chave):
    global _aviso_volume
    try:
        with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
            f.write(chave + "\n")
    except Exception as e:
        if not _aviso_volume:
            log.warning(f"🧠 Sem persistência em {CHECKPOINT_FILE} ({e}). "
                        f"Precisa de um Volume montado em {CHECKPOINT_DIR}.")
            _aviso_volume = True

def limpar_checkpoint():
    try:
        os.remove(CHECKPOINT_FILE)
    except Exception:
        pass


# ───────── API (MAC / tiops) ─────────
def mac(action, params=None, meli_user_id=None):
    # ⚠️ O roteamento por conta da Tiops lê o meli_user_id de DENTRO de params.
    # No topo do payload ele é IGNORADO -> cai no token padrao (INFINITY).
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    payload = {"action": action, "params": params}
    try:
        r = requests.post(MAC_BASE_URL, json=payload,
                          headers={"Content-Type": "application/json", "x-api-key": MAC_API_KEY},
                          timeout=40)
        return r.json()
    except Exception as e:
        return {"status": 0, "error": str(e)}


def listar_via_action(cid):
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
            if offset >= 1000:
                break
            time.sleep(PAUSA)
        return ids

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
        fb = listar_via_action(cid)
        if fb:
            log.info(f"[{cid}] scan vazio — plano B (list_items) pegou {len(fb)} anúncios.")
        return fb
    return ids


def get_item(cid, iid):
    # Ampliado: pega também os campos que o RELATÓRIO precisa.
    attrs = ("id,title,attributes,sale_terms,status,sub_status,tags,"
             "health,available_quantity,pictures,shipping")
    p = f"/items/{iid}?attributes={attrs}"
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
        # Chave ignora hífen/espaço/ponto e maiúsculas → CDH210 == CDH-210 (ML trata como duplicado)
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
    if not oem_n and pn_n: oem_n = pn_n
    if not pn_n and oem_n: pn_n = oem_n
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
    if attrs: body["attributes"] = attrs
    if sale_terms: body["sale_terms"] = sale_terms
    if not body: return None
    r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body}, meli_user_id=cid)
    if r and r.get("status") != 200 and attrs:
        sem_inmetro = [a for a in attrs if a.get("id") != "INMETRO_CERTIFICATION_REGISTRATION_NUMBER"]
        if sem_inmetro != attrs and (sem_inmetro or sale_terms):
            body2 = {}
            if sem_inmetro: body2["attributes"] = sem_inmetro
            if sale_terms: body2["sale_terms"] = sale_terms
            r = mac("raw", {"method": "PUT", "path": f"/items/{iid}", "body": body2}, meli_user_id=cid)
    return r


# ───────── COMPATIBILIDADE (copiar de irmão de mesmo SKU) ─────────
def _compat_count(cid, iid):
    res = mac("raw", {"method": "GET", "path": f"/items/{iid}/compatibilities"}, meli_user_id=cid)
    if res.get("status") != 200: return None
    return (res.get("data") or {}).get("products", [])

def copiar_compatibilidade(cid, alvo, sku, item_alvo):
    """Acha um irmão de MESMO SKU com compat completa e SEM penalidade, e copia.
    Só escreve se COMPAT_APLICAR=true. Retorna texto de status."""
    if not sku:
        return "sem SKU"
    if "moderation_penalty" in (item_alvo.get("tags") or []):
        return "alvo com moderação — pulado"
    res = mac("raw", {"method": "GET", "path": f"/users/{cid}/items/search?seller_sku={quote(sku)}&status=active"}, meli_user_id=cid)
    irmaos = (res.get("data") or {}).get("results", []) if res.get("status") == 200 else []
    fonte = None
    for sid in irmaos:
        if sid == alvo: continue
        det = get_item(cid, sid)
        if not det or "moderation_penalty" in (det.get("tags") or []): continue
        prods = _compat_count(cid, sid)
        if prods:
            fonte = (sid, prods); break
        time.sleep(PAUSA)
    if not fonte:
        return "sem irmão completo"
    sid, prods = fonte
    payload = [{"id": p["catalog_product_id"], "domain_id": p["domain_id"]}
               for p in prods if p.get("catalog_product_id")]
    if not COMPAT_APLICAR:
        return f"candidato (fonte {sid}, {len(payload)} veículos) — escrita OFF"
    r = mac("raw", {"method": "POST", "path": f"/items/{alvo}/compatibilities", "body": {"products": payload}}, meli_user_id=cid)
    return f"copiado de {sid} ({len(payload)}) status {(r or {}).get('status')}"


# ───────── RELATÓRIO ─────────
def velocidade_vendas(cid):
    """Vendas/dia por item nos últimos SALES_WINDOW dias (para 'repor já')."""
    from datetime import datetime, timedelta, timezone
    desde = (datetime.now(timezone.utc) - timedelta(days=SALES_WINDOW)).strftime("%Y-%m-%dT00:00:00.000-00:00")
    log.info(f"[{cid}] calculando giro de vendas ({SALES_WINDOW} dias)... (pode demorar)")
    sold, offset = {}, 0
    while offset < 4000:
        p = (f"/orders/search?seller={cid}&order.status=paid"
             f"&order.date_created.from={quote(desde)}&limit=50&offset={offset}")
        res = mac("raw", {"method": "GET", "path": p}, meli_user_id=cid)
        if res.get("status") != 200: break
        data = res.get("data") or {}
        results = data.get("results", [])
        for o in results:
            for it in o.get("order_items", []):
                k = it.get("item", {}).get("id")
                if k: sold[k] = sold.get(k, 0) + it.get("quantity", 0)
        offset += 50
        if offset >= data.get("paging", {}).get("total", 0) or not results: break
        time.sleep(PAUSA)
    log.info(f"[{cid}] giro calculado: {len(sold)} itens com venda no período.")
    return {k: v / SALES_WINDOW for k, v in sold.items()}

def foto_px(item):
    pics = item.get("pictures", [])
    if not pics: return 0
    try:
        w, h = (int(x) for x in pics[0].get("max_size", "0x0").lower().split("x"))
        return min(w, h)
    except Exception:
        return 0

def to_csv(rows, header):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(header); w.writerows(rows)
    return buf.getvalue().encode("utf-8")

def tg_text(msg):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4000], "parse_mode": "HTML"}, timeout=30)
    except Exception as e:
        log.warning(f"Telegram texto falhou: {e}")

def tg_file(fname, content):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                      data={"chat_id": TELEGRAM_CHAT_ID}, files={"document": (fname, content)}, timeout=60)
    except Exception as e:
        log.warning(f"Telegram arquivo falhou: {e}")

def enviar_email(assunto, corpo, anexos):
    if not (EMAIL_USER and EMAIL_PASS and EMAIL_TO): return
    try:
        m = EmailMessage(); m["From"] = EMAIL_USER; m["To"] = ", ".join(EMAIL_TO); m["Subject"] = assunto
        m.set_content(corpo)
        for fname, content in anexos:
            m.add_attachment(content, maintype="text", subtype="csv", filename=fname)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_USER, EMAIL_PASS); s.send_message(m)
    except Exception as e:
        log.warning(f"Email falhou: {e}")

HEADERS = {
    "precisa_foto":    ["Conta", "MLB", "Titulo", "Saude", "Foto_px", "Estoque"],
    "repor_ja":        ["Conta", "MLB", "Titulo", "Estoque", "Venda_dia", "Dura_dias"],
    "revisao_humana":  ["Conta", "MLB", "Titulo", "Motivo", "Tags"],
    "compat_faltando": ["Conta", "MLB", "Titulo", "SKU"],
    "migrar_full":     ["Conta", "MLB", "Titulo", "Venda_dia", "Logistica"],
}
EXPLICA = {
    "precisa_foto":    "Foto fraca (&lt;1200px) ou saúde baixa. Precisa de foto melhor.",
    "repor_ja":        "Campeão cujo estoque NÃO dura 30 dias pelo ritmo de venda. Repor já.",
    "revisao_humana":  "Pausado por moderação/duplicado/ficha travada. Decisão de vocês.",
    "compat_faltando": "Peça com compatibilidade ausente ou incompleta.",
    "migrar_full":     "Vende bem mas está fora do Full. Candidato a migrar.",
}

def enviar_relatorio(buckets, tot):
    from datetime import date
    hoje = date.today().strftime("%d/%m/%Y")
    modo = "APLICANDO" if APLICAR else "DRY-RUN"
    linhas = [f"<b>🧹 Corretor Faxineiro — {hoje} ({modo})</b>",
              f"Itens varridos: {tot['itens']} | correções escritas: {tot['escritos']}"]
    for nome in HEADERS:
        linhas.append(f"• {nome.replace('_',' ')}: <b>{len(buckets[nome])}</b>")
    linhas.append("\n<b>O que cada lista significa:</b>")
    for k, v in EXPLICA.items():
        linhas.append(f"• <b>{k.replace('_',' ')}</b>: {v}")
    corpo = "\n".join(linhas)
    anexos = [(f"{nome}.csv", to_csv(rows, HEADERS[nome])) for nome, rows in buckets.items() if rows]
    tg_text(corpo)
    for fname, content in anexos:
        tg_file(fname, content)
    enviar_email(f"Corretor Faxineiro {hoje} ({modo})",
                 corpo.replace("<b>", "").replace("</b>", "").replace("&lt;", "<"), anexos)


def run_once():
    modo_txt = "APLICANDO (escrita real)" if APLICAR else "DRY-RUN (só simula)"
    log.info(f"🤖 Corretor Faxineiro — modo={MODO} | alvo={STATUS_ALVO} | {modo_txt} "
             f"| compat_aplicar={COMPAT_APLICAR} | relatorio={RELATORIO}")

    done = carregar_checkpoint()
    if done:
        log.info(f"🧠 Memória: retomando — {len(done)} itens já tratados serão pulados.")

    tot = {"itens": 0, "inmetro": 0, "origem": 0, "codigos": 0, "garantia": 0,
           "titulo_alerta": 0, "escritos": 0, "erros": 0, "pulados": 0, "compat": 0}
    buckets = {k: [] for k in HEADERS}

    for c in CONTAS_ML:
        log.info(f"[{c['nome']}] iniciando varredura...")
        vel = velocidade_vendas(c["id"]) if (RELATORIO and REPOR_JA) else {}
        ids = listar(c["id"])
        log.info(f"[{c['nome']}] {len(ids)} anúncios ({STATUS_ALVO}, {MODO}).")
        for i, iid in enumerate(ids, 1):
            chave = f"{c['id']}:{iid}"
            if chave in done:
                tot["pulados"] += 1; continue
            item = get_item(c["id"], iid)
            if not item:
                tot["erros"] += 1; continue
            tot["itens"] += 1

            # --- correções determinísticas (como antes) ---
            attrs, sale_terms, alertas = planeja(item)
            for a in attrs:
                if a["id"] == "INMETRO_CERTIFICATION_REGISTRATION_NUMBER": tot["inmetro"] += 1
                if a["id"] == "ORIGIN": tot["origem"] += 1
                if a["id"] in ("OEM", "PART_NUMBER"): tot["codigos"] += 1
            if sale_terms: tot["garantia"] += 1
            if alertas: tot["titulo_alerta"] += 1
            if attrs or sale_terms:
                if APLICAR:
                    r = aplica(c["id"], iid, attrs, sale_terms)
                    if r and r.get("status") == 200: tot["escritos"] += 1
                    else: tot["erros"] += 1; log.warning(f"    falha: {(r or {}).get('data')}")

            # --- relatório (read-only) ---
            if RELATORIO:
                titulo = item.get("title", ""); am = amap(item)
                health = item.get("health"); qty = item.get("available_quantity", 0)
                logistic = (item.get("shipping") or {}).get("logistic_type", "")
                tags = item.get("tags") or []; sub = item.get("sub_status") or []
                sku = (am.get("SELLER_SKU") or {}).get("value_name") or ""
                v = vel.get(iid, 0)
                # precisa_foto
                if foto_px(item) < MIN_PHOTO_PX or (health is not None and health < HEALTH_MIN):
                    buckets["precisa_foto"].append([c["nome"], iid, titulo, health, foto_px(item), qty])
                # repor_ja
                if v > 0 and (qty / v) < COVERAGE_DAYS:
                    buckets["repor_ja"].append([c["nome"], iid, titulo, qty, round(v, 2), int(qty / v)])
                # migrar_full
                if v >= 1 and logistic != "fulfillment":
                    buckets["migrar_full"].append([c["nome"], iid, titulo, round(v, 2), logistic])
                # compat_faltando (tag de incompleto) + tenta candidato/cópia
                if any("incomplete" in t and "compat" in t for t in tags) or "incomplete_position_compatibilities" in tags:
                    buckets["compat_faltando"].append([c["nome"], iid, titulo, sku])
                    st = copiar_compatibilidade(c["id"], iid, sku, item)
                    if st.startswith("copiado"): tot["compat"] += 1
                    log.info(f"  compat {iid}: {st}")
                # revisao_humana (pausado por motivo que não é estoque)
                if item.get("status") == "paused" and "out_of_stock" not in sub:
                    motivo = "moderação/duplicado" if "moderation_penalty" in tags else "ficha/outro"
                    buckets["revisao_humana"].append([c["nome"], iid, titulo, motivo, ";".join(tags)])

            done.add(chave); marcar_feito(chave)
            if i % 50 == 0:
                log.info(f"[{c['nome']}] {i}/{len(ids)}...")
            time.sleep(PAUSA)

    limpar_checkpoint()
    log.info(f"🤖 Passada concluída. {tot}")
    if RELATORIO:
        try:
            enviar_relatorio(buckets, tot)
            log.info("📨 Relatório enviado (Telegram/email).")
        except Exception as e:
            log.warning(f"Relatório não enviado: {e}")


def main():
    if not MAC_API_KEY:
        log.error("MAC_API_KEY ausente"); return
    try:
        os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    except Exception:
        pass
    run_once()
    while INTERVALO_DIAS > 0:
        log.info(f"😴 Dormindo {INTERVALO_DIAS} dia(s) até a próxima varredura...")
        time.sleep(INTERVALO_DIAS * 86400)
        run_once()


if __name__ == "__main__":
    main()
