"""
Reativador v1 — O MAPA (somente leitura) — Infinity Bot

Varre os anúncios travados (status under_review / sub_status waiting_for_patch) das 4 contas ML,
classifica o MOTIVO de cada um e entrega um raio-x: quantos são foto, compatibilidade/posição,
INMETRO, título suspeito, etc. NÃO escreve nada — é diagnóstico puro pra decidir as próximas ações.

Saída: CSV (e-mail/Resend) + resumo no Telegram.
v2 (depois): aplica os consertos GRÁTIS (INMETRO, origem, título, posição) em DRY_RUN -> real.
"""
import os, csv, io, time, base64, logging, requests

MAC_API_KEY    = os.environ.get("MAC_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_DESTINO  = os.environ.get("EMAIL_DESTINO", "carloszagallo@gmail.com")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "Infinity Bot <onboarding@resend.dev>")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAC_BASE_URL   = "https://mcp.tiops.com.br/marketplace"

LOTE        = int(os.environ.get("LOTE", "50"))
MAX_ITENS   = int(os.environ.get("MAX_ITENS", "300"))   # por conta; 0 = todos
PAUSA       = float(os.environ.get("PAUSA", "0.2"))
SUB_STATUS  = os.environ.get("SUB_STATUS", "waiting_for_patch")

# Palavras que sugerem peça GENUÍNA/original. Se aparecem no título e a descrição
# NÃO confirma, provavelmente é erro (Carlos: originais são ~2% e estão descritos).
GENUINAS = ["original", "genuin", "bosch", "denso", "keihin", "ngk", "valeo",
            "mahle", "sachs", "skf", "gates", "delphi", "magneti"]

CONTAS_ML = [
    {"id": 60771984,  "nome": "INFINITY AUTOPARTS"},
    {"id": 233798434, "nome": "FREEDOM"},
    {"id": 554248644, "nome": "AUTOPARTSLIBERTY"},
    {"id": 1994875400,"nome": "DESTINYAUTOPARTS"},
]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("Reativador")


def mac_call(action, params=None, meli_user_id=None):
    # roteamento por conta vai DENTRO de params (no topo eh ignorado -> token INFINITY)
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    payload = {"action": action, "params": params}
    headers = {"Content-Type": "application/json", "x-api-key": MAC_API_KEY}
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=40)
        return r.json()
    except Exception as e:
        return {"status": 0, "error": f"exception: {e}"}


def listar_travados(cid):
    """IDs dos anúncios travados (paginado, leve: só ids)."""
    ids, offset = [], 0
    while True:
        path = f"/users/{cid}/items/search?sub_status={SUB_STATUS}&limit={LOTE}&offset={offset}"
        res = mac_call("raw", {"method": "GET", "path": path}, meli_user_id=cid)
        if res.get("status") != 200:
            log.warning(f"  falha search offset={offset}: {res.get('error')}")
            break
        data = res.get("data", {})
        lote = data.get("results", [])
        if not lote:
            break
        ids.extend(lote)
        total = (data.get("paging") or {}).get("total", 0)
        offset += LOTE
        if MAX_ITENS and len(ids) >= MAX_ITENS:
            ids = ids[:MAX_ITENS]; break
        if offset >= total:
            break
        time.sleep(PAUSA)
    return ids


def get_leve(cid, item_id):
    path = f"/items/{item_id}?attributes=id,title,tags,attributes,status,sub_status"
    res = mac_call("raw", {"method": "GET", "path": path}, meli_user_id=cid)
    return res.get("data") if res.get("status") == 200 else None


def attr_map(item):
    m = {}
    for a in item.get("attributes", []):
        m[a.get("id")] = a
    return m


def vazio(attr):
    if not attr:
        return True
    vid = attr.get("value_id")
    vn = attr.get("value_name")
    return vid in (None, "-1", "") and vn in (None, "", " ")


def classificar(item):
    """Retorna (motivos[], detalhes{}). Motivos possíveis:
    FOTO, INMETRO, ORIGEM, TITULO, COMPAT/POSICAO?, OUTRO."""
    tags = item.get("tags", []) or []
    titulo = (item.get("title") or "")
    am = attr_map(item)
    motivos, det = [], {}

    # FOTO (marca d'água / logo / baixa qualidade) — sinal forte nas tags
    if any(t in tags for t in ("poor_quality_thumbnail", "poor_quality_picture")):
        motivos.append("FOTO")

    # INMETRO vazio -> conserto grátis (marcar N/A)
    if vazio(am.get("INMETRO_CERTIFICATION_REGISTRATION_NUMBER")):
        motivos.append("INMETRO")

    # ORIGEM vazia -> conserto grátis (China)
    if "ORIGIN" in am and vazio(am.get("ORIGIN")):
        motivos.append("ORIGEM")

    # TÍTULO suspeito (palavra de peça genuína) -> revisar (confirmar na descrição na v2)
    tl = titulo.lower()
    achou = [g for g in GENUINAS if g in tl]
    if achou:
        motivos.append("TITULO")
        det["titulo_palavras"] = ",".join(achou)

    # COMPAT/POSIÇÃO: sinal fraco pela API; se não caiu em nada acima, marca p/ verificar
    hc = am.get("HAS_COMPATIBILITIES")
    if hc and (hc.get("value_name") == "Não"):
        motivos.append("COMPAT")

    if not motivos:
        motivos.append("VERIFICAR")  # provável compatibilidade/posição (precisa olhar fundo na v2)

    return motivos, det


def free_fixavel(motivos):
    """É reativável de graça? (sem foto, sem incógnita)"""
    if "FOTO" in motivos:
        return False
    if set(motivos) <= {"INMETRO", "ORIGEM", "TITULO", "COMPAT", "POSICAO"}:
        return True
    return False


def processar_conta(conta, linhas, stats):
    cid, nome = conta["id"], conta["nome"]
    log.info(f"[{nome}] listando travados ({SUB_STATUS})...")
    ids = listar_travados(cid)
    log.info(f"[{nome}] {len(ids)} travados (amostra MAX_ITENS={MAX_ITENS or 'todos'}). Classificando...")
    for i, item_id in enumerate(ids, 1):
        item = get_leve(cid, item_id)
        if not item:
            stats["erros"] += 1; continue
        am = attr_map(item)
        sku = (am.get("SELLER_SKU") or {}).get("value_name") or ""
        motivos, det = classificar(item)
        gratis = free_fixavel(motivos)
        for m in motivos:
            stats["motivos"][m] = stats["motivos"].get(m, 0) + 1
        stats["gratis" if gratis else "pagos_ou_verificar"] += 1
        if sku:
            stats["skus"].setdefault(sku, set()).add(item_id)
        linhas.append({
            "conta": nome, "mlb": item_id, "sku": sku,
            "titulo": item.get("title", "")[:70],
            "motivos": "+".join(motivos),
            "gratis": "SIM" if gratis else "não",
            "obs": det.get("titulo_palavras", ""),
        })
        if i % 50 == 0:
            log.info(f"[{nome}] {i}/{len(ids)} classificados...")
        time.sleep(PAUSA)


def enviar_csv(linhas):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Conta", "MLB", "SKU", "Título", "Motivo(s)", "Reativa grátis?", "Obs"])
    for l in linhas:
        w.writerow([l["conta"], l["mlb"], l["sku"], l["titulo"], l["motivos"], l["gratis"], l["obs"]])
    csv_bytes = buf.getvalue().encode("utf-8-sig")
    if not RESEND_API_KEY:
        log.warning("Sem RESEND_API_KEY — CSV não enviado por e-mail."); return
    try:
        requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [EMAIL_DESTINO],
                  "subject": "🔧 Mapa dos anúncios travados (para revisar)",
                  "html": "<p>Raio-x dos anúncios travados (waiting_for_patch), classificados por motivo. CSV anexo.</p>",
                  "attachments": [{"filename": "mapa_travados.csv", "content": base64.b64encode(csv_bytes).decode()}]},
            timeout=60)
        log.info("📧 CSV enviado!")
    except Exception as e:
        log.error(f"Erro email: {e}")


def enviar_telegram(stats, total):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.info(f"RESUMO: total={total} {stats['motivos']} grátis={stats['gratis']}")
        return
    m = stats["motivos"]
    skus_unicos = len(stats["skus"])
    t = [f"🔧 <b>Mapa dos travados (para revisar)</b>",
         f"Total analisado: <b>{total}</b> | SKUs únicos: <b>{skus_unicos}</b>", "",
         f"🟢 <b>Reativam de GRÁTIS: {stats['gratis']}</b>",
         f"🟡 Foto ou a verificar: {stats['pagos_ou_verificar']}", "",
         "<b>Por motivo:</b>",
         f"  📷 Foto (marca d'água/logo): {m.get('FOTO', 0)}",
         f"  🚗 Compatibilidade (declarada Não): {m.get('COMPAT', 0)}",
         f"  📋 INMETRO vazio: {m.get('INMETRO', 0)}",
         f"  🌍 Origem vazia: {m.get('ORIGEM', 0)}",
         f"  🏷️ Título suspeito ('Original'/marca): {m.get('TITULO', 0)}",
         f"  ❓ A verificar (provável compat./posição): {m.get('VERIFICAR', 0)}", "",
         "Próximo: v2 conserta os grátis em simulação → você revisa → solta."]
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(t), "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        log.error(f"Erro Telegram: {e}")


def main():
    log.info("🔧 Reativador v1 (MAPA — somente leitura) iniciado!")
    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY ausente!"); return
    linhas = []
    stats = {"motivos": {}, "gratis": 0, "pagos_ou_verificar": 0, "erros": 0, "skus": {}}
    for c in CONTAS_ML:
        try:
            processar_conta(c, linhas, stats)
        except Exception as e:
            log.error(f"Erro conta {c['nome']}: {e}")
    enviar_csv(linhas)
    enviar_telegram(stats, len(linhas))
    log.info(f"🔧 Concluído! Total={len(linhas)} | motivos={stats['motivos']} | "
             f"grátis={stats['gratis']} | foto/verificar={stats['pagos_ou_verificar']}")


if __name__ == "__main__":
    main()

