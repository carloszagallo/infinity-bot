import os
import time
import requests
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# ── Configurações ──────────────────────────────────────────────
MAC_API_KEY      = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY   = os.environ.get("CLAUDE_API_KEY", "")
GMAIL_USER       = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS   = os.environ.get("GMAIL_APP_PASS", "")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO", "carloszagallo@gmail.com")
INTERVALO_SEG    = int(os.environ.get("INTERVALO_SEG", "30"))
MAX_DESCONTO_MEU = float(os.environ.get("MAX_DESCONTO_MEU", "7.0"))
MAC_BASE_URL     = "https://mcp.tiops.com.br/marketplace"

# ── Contas ML ──────────────────────────────────────────────────
CONTAS_ML = [
    {"id": 60771984,  "nome": "INFINITY AUTOPARTS"},
    {"id": 233798434, "nome": "FREEDOM"},
    {"id": 554248644, "nome": "AUTOPARTSLIBERTY"},
    {"id": 1994875400,"nome": "DESTINYAUTOPARTS"},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("InfinityBot")

# ── Stats por conta ────────────────────────────────────────────
def novo_stats():
    return {
        "perguntas_respondidas": 0,
        "perguntas_ignoradas": 0,
        "avaliacoes_respondidas": 0,
        "promocoes_ativadas": 0,
        "promocoes_ignoradas": 0,
    }

stats = {c["id"]: novo_stats() for c in CONTAS_ML}
ultima_promo       = None
ultimo_rel_manha   = None
ultimo_rel_tarde   = None

SYSTEM_PROMPT = """Você é um assistente especializado em autopeças no Mercado Livre.

INFORMAÇÕES FIXAS (válidas para TODOS os produtos):
- Todos os produtos são NOVOS
- Todos acompanham Nota Fiscal
- Todos possuem 90 dias de garantia
- A marca está descrita no anúncio

Regras ESTRITAS:
1. Compatibilidade: verifique se o modelo/ano/motor está EXPLICITAMENTE no anúncio. Se não → NAO_RESPONDER.
2. Condição → NOVO.
3. NF → acompanha Nota Fiscal.
4. Garantia → 90 dias.
5. Marca → use a do anúncio.
6. Qualquer outra info não disponível → NAO_RESPONDER.
7. Dúvida → NAO_RESPONDER.
8. Máximo 2 frases, educado e direto.

Formato: texto da resposta OU NAO_RESPONDER"""

PROMPT_AVALIACAO = """Você é assistente de uma loja de autopeças no Mercado Livre.
Escreva uma resposta calorosa e educada para uma avaliação positiva.
Objetivo: agradecer e convidar o cliente a voltar.
Máximo 2 frases. Varie o texto. Responda apenas com o texto."""


# ── MAC API ────────────────────────────────────────────────────
def mac_call(action, params=None, meli_user_id=None):
    payload = {"action": action, "params": params or {}}
    if meli_user_id:
        payload["meli_user_id"] = meli_user_id
    headers = {"Content-Type": "application/json", "x-api-key": MAC_API_KEY}
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=30)
        return r.json()
    except Exception as e:
        log.error(f"Erro MAC API ({action}): {e}")
        return {"status": 500, "error": str(e)}


# ── Claude AI ──────────────────────────────────────────────────
def chamar_claude(system, user_msg):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, headers=headers, timeout=30)
        data = r.json()
        if "content" in data and data["content"]:
            return data["content"][0]["text"].strip()
        return "NAO_RESPONDER"
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        return "NAO_RESPONDER"


# ── EMAIL ──────────────────────────────────────────────────────
def enviar_email(assunto, corpo_html):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.warning("⚠️  Email não configurado")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_DESTINO
        msg.attach(MIMEText(corpo_html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())
        log.info(f"📧 Email enviado: {assunto}")
    except Exception as e:
        log.error(f"Erro email: {e}")


def enviar_relatorio():
    agora = datetime.now()
    linhas = ""
    for c in CONTAS_ML:
        s = stats[c["id"]]
        linhas += f"""
        <tr><td colspan="2" style="background:#1a1a2e;color:#fff;padding:8px"><b>{c['nome']}</b></td></tr>
        <tr style="background:#f9f9f9"><td>💬 Perguntas respondidas</td><td><b style="color:#2ecc71">{s['perguntas_respondidas']}</b></td></tr>
        <tr><td>⏭️ Deixadas para você</td><td><b style="color:#e67e22">{s['perguntas_ignoradas']}</b></td></tr>
        <tr style="background:#f9f9f9"><td>⭐ Avaliações respondidas</td><td><b style="color:#2ecc71">{s['avaliacoes_respondidas']}</b></td></tr>
        <tr><td>🏷️ Promoções ativadas</td><td><b style="color:#2ecc71">{s['promocoes_ativadas']}</b></td></tr>
        <tr style="background:#f9f9f9"><td>❌ Promoções ignoradas (+7%)</td><td><b style="color:#e74c3c">{s['promocoes_ignoradas']}</b></td></tr>
        """

    corpo = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
    <h2 style="color:#1a1a2e">📊 Relatório Infinity Bot</h2>
    <p style="color:#666">{agora.strftime('%d/%m/%Y às %H:%M')}</p>
    <hr>
    <table width="100%" cellpadding="10" style="border-collapse:collapse">
    {linhas}
    </table>
    <hr>
    <p style="color:#999;font-size:12px">Infinity Bot • 3 contas ML monitoradas</p>
    </body></html>
    """
    enviar_email(f"📊 Infinity Bot — {agora.strftime('%d/%m %H:%M')}", corpo)

    if agora.hour >= 19:
        for c in CONTAS_ML:
            stats[c["id"]] = novo_stats()


# ── PERGUNTAS ──────────────────────────────────────────────────
def processar_perguntas(conta):
    cid  = conta["id"]
    nome = conta["nome"]
    res  = mac_call("list_questions", {"status": "UNANSWERED", "limit": 50}, meli_user_id=cid)
    if res.get("status") != 200:
        log.error(f"[{nome}] Erro perguntas: {res.get('error')}")
        return

    perguntas = res["data"].get("questions", [])
    if not perguntas:
        return

    log.info(f"[{nome}] 📬 {len(perguntas)} pergunta(s)")

    for q in perguntas:
        item_res = mac_call("get_items", {"ids": [q["item_id"]], "include_description": True}, meli_user_id=cid)
        if item_res.get("status") != 200:
            continue
        items = item_res.get("data") or []
        if not items or items[0].get("code") != 200:
            continue

        anuncio   = items[0]["body"]
        titulo    = anuncio.get("title", "")
        descricao = anuncio.get("description") or titulo

        log.info(f'[{nome}] 💬 "{q["text"][:60]}"')
        resposta = chamar_claude(SYSTEM_PROMPT,
            f"PERGUNTA: {q['text']}\n\nTÍTULO: {titulo}\n\nDESCRIÇÃO: {descricao}")

        if "NAO_RESPONDER" in resposta:
            log.warning(f"[{nome}] ⏭️  Não respondida | {titulo[:40]}")
            stats[cid]["perguntas_ignoradas"] += 1
        else:
            res2 = mac_call("answer_question", {"question_id": q["id"], "text": resposta}, meli_user_id=cid)
            if res2.get("status") == 200:
                log.info(f'[{nome}] ✅ Respondida: "{resposta[:50]}"')
                stats[cid]["perguntas_respondidas"] += 1
            else:
                log.error(f"[{nome}] ❌ Falha ao responder {q['id']}")


# ── AVALIAÇÕES ─────────────────────────────────────────────────
def processar_avaliacoes(conta):
    cid  = conta["id"]
    nome = conta["nome"]
    res  = mac_call("raw", {"method": "GET", "path": "/my/received_ratings?limit=20"}, meli_user_id=cid)
    if res.get("status") != 200:
        return

    ratings = (res.get("data") or {}).get("ratings", [])
    for r in ratings:
        if r.get("reply") or r.get("rating", {}).get("value", 0) < 4:
            continue
        comentario = r.get("comment", "Ótima compra!")
        resposta = chamar_claude(PROMPT_AVALIACAO, f"Avaliação: {comentario}")
        if resposta and "NAO_RESPONDER" not in resposta:
            res2 = mac_call("raw", {
                "method": "POST",
                "path": f"/my/received_ratings/{r['id']}/reply",
                "body": {"reply": resposta}
            }, meli_user_id=cid)
            if res2.get("status") in [200, 201]:
                log.info(f'[{nome}] ⭐ Avaliação respondida')
                stats[cid]["avaliacoes_respondidas"] += 1


# ── PROMOÇÕES ──────────────────────────────────────────────────
MARCAS_BLOQUEADAS  = ["kers", "KERS", "Kers"]
PRECO_MINIMO       = float(os.environ.get("PRECO_MINIMO", "19.0"))


def item_permitido_para_promocao(item_id, desconto_pct, cid):
    """Retorna (permitido, motivo) verificando marca KERS e preço mínimo."""
    res = mac_call("get_items", {"ids": [item_id]}, meli_user_id=cid)
    if res.get("status") != 200:
        return False, "não foi possível verificar o item"
    items = res.get("data") or []
    if not items or items[0].get("code") != 200:
        return False, "item não encontrado"

    item  = items[0]["body"]
    preco = float(item.get("price", 0) or 0)
    attrs = item.get("attributes") or []
    marca = ""
    for a in attrs:
        if a.get("id") == "BRAND":
            marca = (a.get("value_name") or "").strip()
            break

    # Verifica marca KERS
    if any(k.lower() in marca.lower() for k in MARCAS_BLOQUEADAS):
        return False, f"marca KERS bloqueada ({marca})"

    # Verifica preço mínimo após desconto
    preco_final = round(preco * (1 - desconto_pct / 100), 2)
    if preco_final < PRECO_MINIMO:
        return False, f"preço final R$ {preco_final:.2f} abaixo do mínimo R$ {PRECO_MINIMO:.2f}"

    return True, "ok"


def processar_promocoes(conta):
    cid  = conta["id"]
    nome = conta["nome"]
    res  = mac_call("ml_list_promotions", {"limit": 50}, meli_user_id=cid)
    if res.get("status") != 200:
        log.warning(f"[{nome}] ⚠️  Não foi possível buscar promoções")
        return

    promocoes = (res.get("data") or {}).get("results", [])
    log.info(f"[{nome}] 🏷️  {len(promocoes)} promoção(ões)")

    for promo in promocoes:
        promo_id = promo.get("id")
        status   = promo.get("status", "")
        nome_p   = promo.get("name", promo_id)

        if status == "started":
            log.info(f"[{nome}] ✅ Já ativa: {nome_p}")
            continue

        desconto  = float(promo.get("discount_percentage", 0) or 0)
        copart_ml = float(promo.get("marketplace_discount_percentage", 0) or 0)
        meu_desc  = round(desconto - copart_ml, 2)

        # Verifica horário comercial (seg-sex 08h-18h) para regras KERS/preço
        agora_local = datetime.now()
        horario_comercial = (agora_local.weekday() < 5 and 8 <= agora_local.hour < 18)

        # Promoção FLEXÍVEL — participa com desconto mínimo (6%)
        sub_type = promo.get("sub_type", "")
        if sub_type == "FLEXIBLE_PERCENTAGE":
            desconto  = 6.0
            copart_ml = float(promo.get("marketplace_discount_percentage", 0) or 0)
            meu_desc  = round(desconto - copart_ml, 2)
            log.info(f"[{nome}] 🔧 Promoção flexível — usando desconto mínimo 6%: {nome_p}")

        # Verifica desconto máximo
        if meu_desc > MAX_DESCONTO_MEU:
            log.warning(f"[{nome}] ❌ Ignorada ({meu_desc}% > {MAX_DESCONTO_MEU}%): {nome_p}")
            stats[cid]["promocoes_ignoradas"] += 1
            continue

        # Busca itens da promoção para verificar KERS e preço mínimo
        bloqueada = False
        try:
            res_items = mac_call("ml_promotion_items", {"promotion_id": promo_id, "limit": 10}, meli_user_id=cid)
            items_promo = []
            if res_items.get("status") == 200:
                items_promo = (res_items.get("data") or {}).get("results", [])

            for it in items_promo[:5]:  # verifica até 5 itens por promoção
                item_id = it.get("item_id") or it.get("id")
                if not item_id:
                    continue
                permitido, motivo = item_permitido_para_promocao(item_id, desconto, cid)
                if not permitido:
                    log.warning(f"[{nome}] 🚫 Bloqueada — {motivo}: {nome_p}")
                    stats[cid]["promocoes_ignoradas"] += 1
                    bloqueada = True
                    break
        except Exception as e:
            log.warning(f"[{nome}] ⚠️  Erro ao verificar itens de {nome_p}: {e}")

        if bloqueada:
            continue

        # Tudo ok — ativa a promoção
        params_ativacao = {"promotion_id": promo_id, "status": "started"}
        if sub_type == "FLEXIBLE_PERCENTAGE":
            params_ativacao["discount_percentage"] = desconto
        res2 = mac_call("ml_update_promotion", params_ativacao, meli_user_id=cid)
        if res2.get("status") in [200, 201]:
            log.info(f"[{nome}] 🏷️  Ativada: {nome_p} ({meu_desc}%)")
            stats[cid]["promocoes_ativadas"] += 1
        else:
            log.warning(f"[{nome}] ⚠️  Não ativou: {nome_p} — {res2.get('error','')}")


# ── LOOP PRINCIPAL ─────────────────────────────────────────────
def main():
    global ultima_promo, ultimo_rel_manha, ultimo_rel_tarde

    log.info("🚀 INFINITY BOT iniciado!")
    log.info(f"   Contas ML   : {[c['nome'] for c in CONTAS_ML]}")
    log.info(f"   Intervalo   : {INTERVALO_SEG}s")
    log.info(f"   Desc. máx.  : {MAX_DESCONTO_MEU}%")

    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY não configurada!"); return
    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY não configurada!"); return

    ciclo = 0
    while True:
        agora = datetime.now()
        try:
            # Perguntas — todas as contas a cada ciclo
            for conta in CONTAS_ML:
                processar_perguntas(conta)

            # Avaliações — a cada 10 ciclos (~5 min)
            if ciclo % 10 == 0:
                for conta in CONTAS_ML:
                    processar_avaliacoes(conta)

            # Promoções — a cada 6h
            if ultima_promo is None or (agora - ultima_promo) >= timedelta(hours=6):
                for conta in CONTAS_ML:
                    processar_promocoes(conta)
                ultima_promo = agora

            # Relatório 07:00
            if agora.hour == 7 and agora.minute < 1:
                if ultimo_rel_manha is None or ultimo_rel_manha.date() < agora.date():
                    enviar_relatorio()
                    ultimo_rel_manha = agora

            # Relatório 19:00
            if agora.hour == 19 and agora.minute < 1:
                if ultimo_rel_tarde is None or ultimo_rel_tarde.date() < agora.date():
                    enviar_relatorio()
                    ultimo_rel_tarde = agora

        except Exception as e:
            log.error(f"Erro inesperado: {e}")

        ciclo += 1
        log.info(f"⏳ Aguardando {INTERVALO_SEG}s...")
        time.sleep(INTERVALO_SEG)


if __name__ == "__main__":
    main()

