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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("InfinityBot")

# ── Estado diário ──────────────────────────────────────────────
stats = {
    "perguntas_respondidas": 0,
    "perguntas_ignoradas": 0,
    "avaliacoes_respondidas": 0,
    "promocoes_ativadas": 0,
    "promocoes_ignoradas": 0,
    "ultima_verificacao_promo": None,
    "ultimo_relatorio_manha": None,
    "ultimo_relatorio_tarde": None,
}

SYSTEM_PROMPT = """Você é um assistente especializado em autopeças da loja INFINITY AUTO PARTS no Mercado Livre.

INFORMAÇÕES FIXAS DA LOJA (válidas para TODOS os produtos):
- Todos os produtos são NOVOS
- Todos os produtos acompanham Nota Fiscal
- Todos os produtos possuem 90 dias de garantia
- A marca do produto está descrita no anúncio

Sua tarefa é analisar a pergunta do cliente e responder APENAS com informações que estejam EXPLICITAMENTE escritas no título ou descrição do anúncio, ou nas informações fixas acima.

Regras ESTRITAS:
1. Para compatibilidade: verifique se o modelo/ano/motor do cliente está EXPLICITAMENTE listado no anúncio. Se não estiver → NAO_RESPONDER.
2. Para perguntas sobre condição → responda que é NOVO.
3. Para perguntas sobre NF → responda que acompanha Nota Fiscal.
4. Para perguntas sobre garantia → responda que possui 90 dias de garantia.
5. Para perguntas sobre marca → use a marca descrita no anúncio.
6. Para qualquer outra informação que NÃO esteja no anúncio → NAO_RESPONDER.
7. Se houver QUALQUER dúvida → NAO_RESPONDER.
8. Respostas curtas, diretas e educadas. Máximo 2 frases.

Formato:
- Se puder responder com certeza: apenas o texto da resposta
- Se não puder: NAO_RESPONDER"""

PROMPT_AVALIACAO = """Você é um assistente da loja INFINITY AUTO PARTS no Mercado Livre.

Escreva uma resposta curta, calorosa e educada para uma avaliação positiva de um cliente.
O objetivo é agradecer e convidar o cliente a comprar novamente.
Máximo 2 frases. Varie as respostas para não parecer robótico.
Responda apenas com o texto da resposta, sem aspas."""


# ── MAC API ────────────────────────────────────────────────────
def mac_call(action, params=None):
    payload = {"action": action, "params": params or {}}
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
        log.warning("⚠️  Email não configurado (GMAIL_USER ou GMAIL_APP_PASS ausente)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_DESTINO
        msg.attach(MIMEText(corpo_html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())
        log.info(f"📧 Email enviado: {assunto}")
        return True
    except Exception as e:
        log.error(f"Erro ao enviar email: {e}")
        return False


def enviar_relatorio():
    agora = datetime.now()
    assunto = f"📊 Infinity Bot — Relatório {agora.strftime('%d/%m/%Y %H:%M')}"
    corpo = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
    <h2 style="color:#1a1a2e">📊 Relatório INFINITY AUTO PARTS</h2>
    <p style="color:#666">{agora.strftime('%d/%m/%Y às %H:%M')}</p>
    <hr>
    <table width="100%" cellpadding="12" style="border-collapse:collapse">
      <tr style="background:#f0f4ff">
        <td>💬 Perguntas respondidas automaticamente</td>
        <td><b style="color:#2ecc71">{stats['perguntas_respondidas']}</b></td>
      </tr>
      <tr>
        <td>⏭️ Perguntas deixadas para você</td>
        <td><b style="color:#e67e22">{stats['perguntas_ignoradas']}</b></td>
      </tr>
      <tr style="background:#f0f4ff">
        <td>⭐ Avaliações respondidas</td>
        <td><b style="color:#2ecc71">{stats['avaliacoes_respondidas']}</b></td>
      </tr>
      <tr>
        <td>🏷️ Promoções ativadas</td>
        <td><b style="color:#2ecc71">{stats['promocoes_ativadas']}</b></td>
      </tr>
      <tr style="background:#f0f4ff">
        <td>❌ Promoções ignoradas (acima de 7%)</td>
        <td><b style="color:#e74c3c">{stats['promocoes_ignoradas']}</b></td>
      </tr>
    </table>
    <hr>
    <p style="color:#999;font-size:12px">Infinity Bot • Respostas Automáticas Mercado Livre</p>
    </body></html>
    """
    enviar_email(assunto, corpo)
    # Zera stats após relatório da noite
    if agora.hour >= 19:
        for k in ["perguntas_respondidas","perguntas_ignoradas","avaliacoes_respondidas","promocoes_ativadas","promocoes_ignoradas"]:
            stats[k] = 0


# ── PERGUNTAS ──────────────────────────────────────────────────
def processar_perguntas():
    log.info("🔍 Verificando perguntas novas...")
    res = mac_call("list_questions", {"status": "UNANSWERED", "limit": 50})
    if res.get("status") != 200:
        log.error(f"Erro ao buscar perguntas: {res.get('error')}")
        return

    perguntas = res["data"].get("questions", [])
    if not perguntas:
        log.info("✅ Nenhuma pergunta nova")
        return

    log.info(f"📬 {len(perguntas)} pergunta(s) encontrada(s)")

    for q in perguntas:
        item_res = mac_call("get_items", {"ids": [q["item_id"]], "include_description": True})
        if item_res.get("status") != 200:
            continue
        items = item_res.get("data") or []
        if not items or items[0].get("code") != 200:
            continue
        anuncio   = items[0]["body"]
        titulo    = anuncio.get("title", "")
        descricao = anuncio.get("description") or titulo

        log.info(f'💬 Analisando: "{q["text"][:60]}"')
        resposta = chamar_claude(SYSTEM_PROMPT,
            f"PERGUNTA: {q['text']}\n\nTÍTULO: {titulo}\n\nDESCRIÇÃO: {descricao}")

        if "NAO_RESPONDER" in resposta:
            log.warning(f"⏭️  Não respondida | {titulo[:40]}")
            stats["perguntas_ignoradas"] += 1
        else:
            res2 = mac_call("answer_question", {"question_id": q["id"], "text": resposta})
            if res2.get("status") == 200:
                log.info(f'✅ Respondida: "{resposta[:60]}"')
                stats["perguntas_respondidas"] += 1
            else:
                log.error(f"❌ Falha ao responder pergunta {q['id']}")


# ── AVALIAÇÕES ─────────────────────────────────────────────────
def processar_avaliacoes():
    log.info("⭐ Verificando avaliações sem resposta...")
    res = mac_call("raw", {"method": "GET", "path": "/my/received_ratings?limit=20"})
    if res.get("status") != 200:
        log.warning("⚠️  Não foi possível buscar avaliações")
        return

    ratings = (res.get("data") or {}).get("ratings", [])
    for r in ratings:
        if r.get("reply") or r.get("rating", {}).get("value", 0) < 4:
            continue
        comentario = r.get("comment", "Ótima compra!")
        resposta = chamar_claude(PROMPT_AVALIACAO,
            f"Avaliação do cliente: {comentario}")
        if resposta and "NAO_RESPONDER" not in resposta:
            res2 = mac_call("raw", {
                "method": "POST",
                "path": f"/my/received_ratings/{r['id']}/reply",
                "body": {"reply": resposta}
            })
            if res2.get("status") in [200, 201]:
                log.info(f'⭐ Avaliação respondida: "{resposta[:50]}"')
                stats["avaliacoes_respondidas"] += 1


# ── PROMOÇÕES ──────────────────────────────────────────────────
def calcular_meu_desconto(promocao):
    """Retorna quanto % sai do meu bolso nessa promoção."""
    tipo            = promocao.get("type", "")
    valor_desconto  = float(promocao.get("discount_percentage", 0) or 0)
    copart_ml       = float(promocao.get("marketplace_discount_percentage", 0) or 0)
    meu_desconto    = valor_desconto - copart_ml
    return round(meu_desconto, 2)


def processar_promocoes():
    log.info("🏷️  Verificando promoções disponíveis...")
    res = mac_call("ml_list_promotions", {"limit": 50})
    if res.get("status") != 200:
        log.warning(f"⚠️  Não foi possível buscar promoções: {res.get('error')}")
        return

    promocoes = (res.get("data") or {}).get("results", [])
    if not promocoes:
        log.info("✅ Nenhuma promoção disponível")
        return

    log.info(f"🏷️  {len(promocoes)} promoção(ões) encontrada(s)")

    for promo in promocoes:
        promo_id = promo.get("id")
        status   = promo.get("status", "")
        nome     = promo.get("name", promo_id)

        if status == "started":
            log.info(f"✅ Já ativa: {nome}")
            continue

        meu_desconto = calcular_meu_desconto(promo)

        if meu_desconto <= MAX_DESCONTO_MEU:
            res2 = mac_call("ml_update_promotion", {"promotion_id": promo_id, "status": "started"})
            if res2.get("status") in [200, 201]:
                log.info(f"🏷️  Ativada: {nome} (meu desconto: {meu_desconto}%)")
                stats["promocoes_ativadas"] += 1
            else:
                log.warning(f"⚠️  Não foi possível ativar: {nome}")
        else:
            log.warning(f"❌ Ignorada (meu desconto {meu_desconto}% > {MAX_DESCONTO_MEU}%): {nome}")
            stats["promocoes_ignoradas"] += 1


# ── LOOP PRINCIPAL ─────────────────────────────────────────────
def main():
    log.info("🚀 INFINITY BOT iniciado!")
    log.info(f"   Intervalo perguntas : {INTERVALO_SEG}s")
    log.info(f"   Desconto máximo     : {MAX_DESCONTO_MEU}%")

    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY não configurada!"); return
    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY não configurada!"); return

    ciclo = 0
    while True:
        agora = datetime.now()
        try:
            # Perguntas — a cada ciclo (30s)
            processar_perguntas()

            # Avaliações — a cada 10 ciclos (~5 min)
            if ciclo % 10 == 0:
                processar_avaliacoes()

            # Promoções — a cada 720 ciclos (~6h)
            ultima = stats["ultima_verificacao_promo"]
            if ultima is None or (agora - ultima) >= timedelta(hours=6):
                processar_promocoes()
                stats["ultima_verificacao_promo"] = agora

            # Relatório manhã — 07:00
            ultimo_m = stats["ultimo_relatorio_manha"]
            if agora.hour == 7 and agora.minute < 1:
                if ultimo_m is None or ultimo_m.date() < agora.date():
                    enviar_relatorio()
                    stats["ultimo_relatorio_manha"] = agora

            # Relatório tarde — 19:00
            ultimo_t = stats["ultimo_relatorio_tarde"]
            if agora.hour == 19 and agora.minute < 1:
                if ultimo_t is None or ultimo_t.date() < agora.date():
                    enviar_relatorio()
                    stats["ultimo_relatorio_tarde"] = agora

        except Exception as e:
            log.error(f"Erro inesperado: {e}")

        ciclo += 1
        log.info(f"⏳ Aguardando {INTERVALO_SEG}s...")
        time.sleep(INTERVALO_SEG)


if __name__ == "__main__":
    main()

