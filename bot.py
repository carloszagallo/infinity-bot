import os
import time
import requests
import logging
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ_BR = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ_BR = None

# ── Configurações ──────────────────────────────────────────────
MAC_API_KEY      = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY   = os.environ.get("CLAUDE_API_KEY", "")
EMAIL_DESTINO    = os.environ.get("EMAIL_DESTINO", "carloszagallo@gmail.com")
# E-mail agora via HTTP (Resend) — SMTP é bloqueado no Railway
RESEND_API_KEY   = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM       = os.environ.get("EMAIL_FROM", "Infinity Bot <onboarding@resend.dev>")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
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


def agora_br():
    """Horário do Brasil — o Railway roda em UTC, então sem isso a saudação saía 3h adiantada."""
    if TZ_BR:
        return datetime.now(TZ_BR)
    return datetime.utcnow() - timedelta(hours=3)   # Brasil é UTC-3 (sem horário de verão)


def saudacao_do_horario():
    h = agora_br().hour
    if 5 <= h < 12:
        return "bom dia"
    if 12 <= h < 18:
        return "boa tarde"
    return "boa noite"


# Apelidos já buscados e clientes já saudados NESTA sessão (saudação só na 1ª resposta)
_nick_cache = {}
_ja_saudados = set()
# Perguntas que a IA já avaliou e não soube responder — não re-loga nem re-consulta toda volta
_sem_resposta = set()

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

# Atributos do anúncio que ajudam a responder
ATTR_UTEIS = {"BRAND", "MODEL", "PART_NUMBER", "OEM", "VEHICLE_TYPE",
              "VEHICLE_BRAND", "VEHICLE_MODEL", "VEHICLE_YEAR", "LINE", "VERSION"}

SYSTEM_PROMPT = """Você é o atendente de uma loja de autopeças no Mercado Livre, respondendo a pergunta de um cliente.

Use o ANÚNCIO COMPLETO fornecido (título, descrição, frete, estoque, preço, condição, atributos) e as
informações fixas da loja. RESPONDA sempre que a informação estiver no anúncio ou nas regras fixas.
Só responda NAO_RESPONDER quando a pergunta for sobre algo que realmente NÃO está no anúncio.
Nunca invente; nunca chute.

INFORMAÇÕES FIXAS DA LOJA (valem para TODOS os produtos):
- Todos os produtos são NOVOS.
- Todos acompanham Nota Fiscal.
- Todos têm 90 dias de garantia.
- A marca está informada no anúncio.

VOCÊ SEMPRE CONSEGUE RESPONDER (com os dados do anúncio):
- Frete/envio: se "Frete grátis: Sim", confirme com simpatia que enviamos COM FRETE GRÁTIS pelo Mercado Livre.
- Estoque: se houver quantidade disponível, confirme que tem em estoque, pronta entrega.
- Prazo ("quando chega?"): NÃO prometa data exata. Confirme o frete grátis, explique que o prazo previsto
  aparece na tela do anúncio ao calcular o frete (depende de quando comprar) e tranquilize que postamos
  assim que o Mercado Livre liberar a etiqueta.
- É novo? / Tem nota fiscal? / Tem garantia? → use as informações fixas (Novo / NF / 90 dias).

SÓ NAO_RESPONDER QUANDO:
- Compatibilidade com veículo/ano/motor que NÃO esteja no título, descrição ou atributos.
- Especificação técnica (medida, material, etc.) ausente no anúncio.
- Qualquer coisa que dependa de informação fora do anúncio.

TOM: simpático, direto e profissional, em português do Brasil, no máximo 2 frases.

IMPORTANTE: NÃO inclua saudação (bom dia / olá / etc.) nem despedida no texto — responda direto ao ponto.
A saudação com o nome do cliente é adicionada automaticamente quando for a primeira resposta.

EXEMPLOS:
Pergunta: "Vocês enviam?"
Resposta: "Sim! Enviamos com frete grátis pelo Mercado Livre para todo o Brasil. 😊"

Pergunta: "Se sim, quando chega para mim? 88301-400"
Resposta: "Enviamos com frete grátis pelo Mercado Livre! O prazo previsto para o seu CEP aparece aqui no anúncio ao calcular o frete, e postamos assim que o Mercado Livre liberar a etiqueta. 😊"

Pergunta: "Tem garantia?"
Resposta: "Sim! Todos os nossos produtos têm 90 dias de garantia e acompanham Nota Fiscal. 😊"

Pergunta: "Serve no Gol 2008?" (quando 2008 não está no anúncio)
Resposta: NAO_RESPONDER

Formato da resposta: SOMENTE o texto final para o cliente, OU a palavra NAO_RESPONDER."""

PROMPT_AVALIACAO = """Você é assistente de uma loja de autopeças no Mercado Livre.
Escreva uma resposta calorosa e educada para uma avaliação positiva.
Objetivo: agradecer e convidar o cliente a voltar.
Máximo 2 frases. Varie o texto. Responda apenas com o texto."""

# Frases que indicam incerteza — rede de segurança: se a IA escorregar e gerar um "não sei"
# em prosa (sem a palavra NAO_RESPONDER), tratamos como pular e NÃO publicamos.
SINAIS_DE_INCERTEZA = [
    "nao_responder", "não sei", "nao sei", "não consigo", "nao consigo",
    "não tenho certeza", "nao tenho certeza", "não posso responder", "nao posso responder",
    "recomendo entrar em contato", "não menciona", "nao menciona", "não há informação",
    "nao ha informacao", "consulte o vendedor", "outras plataformas", "não informa", "nao informa",
]

def deve_pular(resposta):
    """True = NÃO publicar. Pega o NAO_RESPONDER e qualquer 'não sei' disfarçado em prosa."""
    if not resposta:
        return True
    t = resposta.strip().lower()
    return any(s in t for s in SINAIS_DE_INCERTEZA)


def montar_contexto(anuncio):
    """Transforma o anúncio inteiro num texto claro — incluindo frete, estoque e preço."""
    ship = anuncio.get("shipping") or {}
    frete_gratis = "Sim" if ship.get("free_shipping") else "Não"
    envia_ml = "Sim (Mercado Envios)" if ship.get("mode") in ("me1", "me2") else "Verificar"
    retirada = "Sim" if ship.get("local_pick_up") or ship.get("store_pick_up") else "Não"
    estoque  = anuncio.get("available_quantity") or 0
    preco    = anuncio.get("price")
    condicao = "Novo" if anuncio.get("condition") == "new" else (anuncio.get("condition") or "")
    descricao = anuncio.get("description") or ""

    attrs = []
    for a in anuncio.get("attributes", []):
        if a.get("id") in ATTR_UTEIS:
            v = a.get("value_name")
            if v and v not in ("", "null", "N/A"):
                attrs.append(f"{a.get('name', a.get('id'))}: {v}")

    linhas = [
        f"TÍTULO: {anuncio.get('title', '')}",
        f"DESCRIÇÃO: {descricao or '(sem descrição)'}",
        f"Condição: {condicao}",
        f"Preço: R$ {preco}",
        f"Estoque disponível: {estoque} unidade(s)",
        f"Frete grátis: {frete_gratis}",
        f"Enviamos pelo Mercado Livre: {envia_ml}",
        f"Retirada local: {retirada}",
        "Nota Fiscal: Sim | Garantia: 90 dias",
    ]
    if attrs:
        linhas.append("Atributos: " + " | ".join(attrs))
    return "\n".join(linhas)


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


def buscar_apelido(cid, from_id):
    """Apelido (nickname) do comprador no ML. O ML não expõe o nome real, só o apelido."""
    if not from_id:
        return ""
    if from_id in _nick_cache:
        return _nick_cache[from_id]
    apelido = ""
    res = mac_call("raw", {"method": "GET", "path": f"/users/{from_id}"}, meli_user_id=cid)
    if res.get("status") == 200:
        apelido = (res.get("data") or {}).get("nickname", "") or ""
    _nick_cache[from_id] = apelido
    return apelido


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


# ── EMAIL (Resend HTTP) ────────────────────────────────────────
def enviar_email(assunto, corpo_html):
    if not RESEND_API_KEY:
        log.warning("⚠️  RESEND_API_KEY não configurada — relatório não enviado por e-mail")
        return
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [EMAIL_DESTINO], "subject": assunto, "html": corpo_html},
            timeout=30,
        )
        if r.status_code in (200, 201):
            log.info(f"📧 Email enviado: {assunto}")
        else:
            log.error(f"Erro email (Resend {r.status_code}): {r.text[:200]}")
    except Exception as e:
        log.error(f"Erro email: {e}")


# ── TELEGRAM ───────────────────────────────────────────────────
def enviar_telegram(texto):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code == 200:
            log.info("📲 Relatório enviado no Telegram")
            return True
        log.error(f"Erro Telegram ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        log.error(f"Erro Telegram: {e}")
    return False


def enviar_relatorio():
    agora = agora_br()

    # Versão texto pro Telegram
    tg = [f"📊 <b>Relatório Infinity Bot</b>", f"🕐 {agora.strftime('%d/%m/%Y às %H:%M')}", ""]
    for c in CONTAS_ML:
        s = stats[c["id"]]
        tg.append(f"<b>{c['nome']}</b>")
        tg.append(f"💬 Respondidas: {s['perguntas_respondidas']}   ⏭️ Pra você: {s['perguntas_ignoradas']}")
        tg.append(f"⭐ Avaliações: {s['avaliacoes_respondidas']}")
        tg.append(f"🏷️ Promoções ativadas: {s['promocoes_ativadas']}   ❌ Ignoradas: {s['promocoes_ignoradas']}")
        tg.append("")
    enviado = enviar_telegram("\n".join(tg))

    # Reserva: se o Telegram não estiver configurado/falhar, manda e-mail
    if not enviado:
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
        <p style="color:#999;font-size:12px">Infinity Bot • 4 contas ML monitoradas</p>
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

    # Tira as que a IA já avaliou e não soube responder — não re-loga nem re-gasta crédito toda volta
    perguntas = [q for q in perguntas if q["id"] not in _sem_resposta]
    if not perguntas:
        return

    log.info(f"[{nome}] 📬 {len(perguntas)} pergunta(s)")

    for q in perguntas:
        # get_item (singular) traz o anúncio COMPLETO (frete, estoque, atributos).
        # O get_items (plural) vinha "magro" (só título/preço) — por isso o bot não via o frete.
        item_res = mac_call("get_item", {"itemId": q["item_id"]}, meli_user_id=cid)
        if item_res.get("status") != 200:
            continue
        anuncio = item_res.get("data") or {}
        if not anuncio:
            continue

        titulo   = anuncio.get("title", "")
        contexto = montar_contexto(anuncio)

        log.info(f'[{nome}] 💬 "{q["text"][:60]}"')
        resposta = chamar_claude(SYSTEM_PROMPT,
            f"PERGUNTA DO CLIENTE: {q['text']}\n\nANÚNCIO:\n{contexto}")

        # Trava: NAO_RESPONDER explícito OU qualquer incerteza em prosa → não publica
        if deve_pular(resposta):
            # Não soube responder: marca e segue calado (sem sinalizar/re-consultar toda volta).
            # A estratégia dos "não respondidos" você define depois.
            _sem_resposta.add(q["id"])
            stats[cid]["perguntas_ignoradas"] += 1
        else:
            # Saudação só na 1ª resposta a esse cliente nesse anúncio (evita repetir no bate-papo)
            from_id = (q.get("from") or {}).get("id")
            chave = f"{cid}:{q.get('item_id')}:{from_id}"
            if chave not in _ja_saudados:
                saud = saudacao_do_horario()
                apelido = buscar_apelido(cid, from_id)
                if apelido:
                    resposta = f"Olá {apelido}, {saud}! {resposta}"
                else:
                    resposta = f"{saud.capitalize()}! {resposta}"

            res2 = mac_call("answer_question", {"question_id": q["id"], "text": resposta}, meli_user_id=cid)
            if res2.get("status") == 200:
                log.info(f'[{nome}] ✅ Respondida: "{resposta[:50]}"')
                stats[cid]["perguntas_respondidas"] += 1
                _ja_saudados.add(chave)
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
    # get_item (singular) traz os atributos; sem eles o check da marca KERS NÃO funcionava.
    res = mac_call("get_item", {"itemId": item_id}, meli_user_id=cid)
    if res.get("status") != 200:
        return False, "não foi possível verificar o item"
    item = res.get("data") or {}
    if not item:
        return False, "item não encontrado"
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
        agora_local = agora_br()
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
        agora = agora_br()
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
