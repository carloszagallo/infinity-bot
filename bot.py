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
    # roteamento por conta vai DENTRO de params (no topo eh ignorado -> token INFINITY)
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    payload = {"action": action, "params": params}
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


# ── PROMOÇÕES — inscrição item a item ──────────────────────────
MARCAS_BLOQUEADAS  = ["kers", "KERS", "Kers"]
PRECO_MINIMO       = float(os.environ.get("PRECO_MINIMO", "19.0"))

# Tipos em que se entra ITEM A ITEM (SMART: ML co-participa; DEAL: preço por item)
TIPOS_ITEM_PROMO   = {"SMART", "DEAL"}
# Tipos geridos pela ML / cupom / auto-financiados / pré-acordo — NÃO mexer
TIPOS_PROMO_IGNORAR = {
    "LIGHTNING", "UNHEALTHY_STOCK", "PRICE_MATCHING", "PRE_NEGOTIATED",
    "SELLER_COUPON_CAMPAIGN", "PRICE_DISCOUNT", "MARKETPLACE_CAMPAIGN",
}

# Trava de segurança: começa SIMULANDO. Só escreve quando você ligar (DRY_RUN_PROMO=false).
DRY_RUN_PROMO     = os.environ.get("DRY_RUN_PROMO", "true").lower() == "true"
TESTE_N_PROMO     = int(os.environ.get("TESTE_N_PROMO", "0"))      # máx. inscrições por conta/rodada no modo REAL (0 = sem limite)
CONTA_PROMO       = os.environ.get("CONTA_PROMO", "").strip()       # restringe a 1 conta (id); vazio = todas
CHECAR_KERS       = os.environ.get("CHECAR_KERS", "true").lower() == "true"
PROMO_PAGE        = int(os.environ.get("PROMO_PAGE", "50"))
PAUSA_ITEM_PROMO  = float(os.environ.get("PAUSA_ITEM_PROMO", "0.4"))
PAUSA_LOTE_PROMO  = float(os.environ.get("PAUSA_LOTE_PROMO", "1.2"))
MAX_PAGINAS_PROMO = int(os.environ.get("MAX_PAGINAS_PROMO", "100"))


def _marca_kers(item_id, cid):
    """True se a marca do item for KERS (bloqueada). Faz 1 get_item."""
    res = mac_call("get_item", {"itemId": item_id}, meli_user_id=cid)
    if res.get("status") != 200:
        return False
    for a in ((res.get("data") or {}).get("attributes") or []):
        if a.get("id") == "BRAND":
            marca = (a.get("value_name") or "").strip().lower()
            return any(k.lower() in marca for k in MARCAS_BLOQUEADAS)
    return False


def _oferta_do_item(it, tipo):
    """(minha_parte_%, deal_price|None, preco_final) ou None se não dá pra entrar <= teto."""
    orig = float(it.get("original_price", 0) or 0)
    if orig <= 0:
        return None
    if tipo == "SMART":
        meu   = float(it.get("seller_percentage", 0) or 0)    # ML já define a oferta; sua parte vem pronta
        preco = float(it.get("price", 0) or 0)
        return (round(meu, 2), None, round(preco, 2))         # SMART entra por offer_id
    if tipo == "DEAL":
        maxp = float(it.get("max_discounted_price", 0) or 0)  # maior preço aceito = MENOR desconto exigido
        if maxp <= 0:
            return None
        meu = round((orig - maxp) / orig * 100, 2)
        return (meu, round(maxp, 2), round(maxp, 2))
    return None


def _inscrever_item(cid, item_id, promo, it, deal_price):
    """POST que inscreve o item na campanha. Retorna (ok, msg_erro)."""
    tipo = promo.get("type")
    body = {"promotion_id": promo.get("id"), "promotion_type": tipo}
    if tipo == "SMART":
        body["offer_id"] = it.get("offer_id")
    elif tipo == "DEAL":
        body["deal_price"] = deal_price
    r = mac_call("raw", {
        "method": "POST",
        "path": f"/seller-promotions/items/{item_id}?app_version=v2",
        "body": body,
    }, meli_user_id=cid)
    if r.get("status") in (200, 201):
        return True, ""
    return False, ((r.get("data") or {}).get("message") or r.get("error", "") or "erro")


def processar_promocoes(conta):
    cid  = conta["id"]
    nome = conta["nome"]
    if CONTA_PROMO and str(cid) != CONTA_PROMO:
        return

    res = mac_call("ml_list_promotions", {"limit": 50}, meli_user_id=cid)
    if res.get("status") != 200:
        log.warning(f"[{nome}] ⚠️  Não foi possível buscar campanhas")
        return
    campanhas = (res.get("data") or {}).get("results", []) or []
    modo = "🧪 DRY (simulando)" if DRY_RUN_PROMO else "✍️  REAL"
    log.info(f"[{nome}] 🏷️  {len(campanhas)} campanha(s) | {modo} | teto sua parte {MAX_DESCONTO_MEU}%")

    inscritos_conta = 0
    ja_vistos = set()   # não inscreve o mesmo item em 2 campanhas na mesma rodada

    for promo in campanhas:
        tipo   = promo.get("type", "")
        nome_p = promo.get("name", promo.get("id"))
        status = promo.get("status", "")

        if tipo in TIPOS_PROMO_IGNORAR:
            continue
        if tipo not in TIPOS_ITEM_PROMO:
            log.info(f"[{nome}] ⏭️  Pulada (tipo {tipo} não tratado): {nome_p}")
            continue
        if status not in ("started", "pending"):
            continue

        ins = caros = piso = kers = 0
        search_after = None
        paginas = 0
        parar = False

        while paginas < MAX_PAGINAS_PROMO and not parar:
            path = (f"/seller-promotions/promotions/{promo.get('id')}/items"
                    f"?promotion_type={tipo}&app_version=v2&limit={PROMO_PAGE}")
            if search_after:
                path += f"&search_after={search_after}"
            r = mac_call("raw", {"method": "GET", "path": path}, meli_user_id=cid)
            if r.get("status") != 200:
                log.warning(f"[{nome}] ⚠️  Não listou itens de {nome_p}: {r.get('error','')}")
                break
            data  = r.get("data") or {}
            itens = data.get("results", []) or []
            if not itens:
                break

            for it in itens:
                if it.get("status") != "candidate":
                    continue
                item_id = it.get("id")
                if not item_id or item_id in ja_vistos:
                    continue
                oferta = _oferta_do_item(it, tipo)
                if not oferta:
                    continue
                meu_pct, deal_price, preco_final = oferta

                if meu_pct > MAX_DESCONTO_MEU:
                    caros += 1
                    continue
                if preco_final and preco_final < PRECO_MINIMO:
                    piso += 1
                    continue
                if (not DRY_RUN_PROMO) and CHECAR_KERS and _marca_kers(item_id, cid):
                    kers += 1
                    continue

                if DRY_RUN_PROMO:
                    ins += 1
                    ja_vistos.add(item_id)
                    if ins <= 5:
                        log.info(f"[{nome}] 🧪 inscreveria {item_id} → {nome_p} ({meu_pct}% | R$ {preco_final})")
                else:
                    ok, err = _inscrever_item(cid, item_id, promo, it, deal_price)
                    if ok:
                        ins += 1
                        ja_vistos.add(item_id)
                        log.info(f"[{nome}] ✅ {item_id} → {nome_p} ({meu_pct}% | R$ {preco_final})")
                    else:
                        log.warning(f"[{nome}] ❌ {item_id} → {nome_p}: {err}")
                    time.sleep(PAUSA_ITEM_PROMO)

                if (not DRY_RUN_PROMO) and TESTE_N_PROMO and (inscritos_conta + ins) >= TESTE_N_PROMO:
                    parar = True
                    break

            paginas += 1
            search_after = (data.get("paging") or {}).get("searchAfter")
            if not search_after:
                break
            time.sleep(PAUSA_LOTE_PROMO)

        inscritos_conta += ins
        stats[cid]["promocoes_ativadas"]  += ins
        stats[cid]["promocoes_ignoradas"] += caros
        log.info(f"[{nome}] 🏷️  {nome_p}: inscritos {ins} | caros>{MAX_DESCONTO_MEU}% {caros} | piso {piso} | kers {kers}")

        if (not DRY_RUN_PROMO) and TESTE_N_PROMO and inscritos_conta >= TESTE_N_PROMO:
            log.info(f"[{nome}] 🧪 limite de teste ({TESTE_N_PROMO}) atingido — parando conta.")
            break

    suf = " (simuladas)" if DRY_RUN_PROMO else ""
    log.info(f"[{nome}] 🏁 Promoções: {inscritos_conta} inscrição(ões){suf}")


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
