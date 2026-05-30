import os
import time
import requests
import logging

# ── Configurações ──────────────────────────────────────────────
MAC_API_KEY    = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
INTERVALO_SEG  = int(os.environ.get("INTERVALO_SEG", "30"))
MAC_BASE_URL   = "https://mcp.tiops.com.br/marketplace"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("InfinityBot")

SYSTEM_PROMPT = """Você é um assistente especializado em autopeças da loja INFINITY AUTO PARTS no Mercado Livre.

Sua tarefa é analisar a pergunta de um cliente e a descrição do anúncio, e decidir se consegue responder com CERTEZA.

Regras:
1. Se a pergunta for sobre compatibilidade (serve no meu carro?), verifique se o carro/motor/ano do cliente bate com o anúncio.
2. Se o carro do cliente NÃO estiver na compatibilidade do anúncio, responda exatamente: NAO_RESPONDER
3. Se o carro do cliente ESTIVER na lista, confirme de forma clara e objetiva.
4. Se a pergunta for sobre prazo, frete ou garantia e a info estiver no anúncio, responda.
5. Se houver QUALQUER dúvida, responda exatamente: NAO_RESPONDER
6. Respostas devem ser curtas, diretas e educadas. Máximo 2 frases.
7. NUNCA invente informações que não estejam no anúncio.

Formato:
- Se puder responder: apenas o texto da resposta
- Se não puder: NAO_RESPONDER"""


# ── Funções da MAC API ──────────────────────────────────────────
def mac_call(action, params=None):
    payload = {"action": action, "params": params or {}}
    headers = {
        "Content-Type": "application/json",
        "x-api-key": MAC_API_KEY
    }
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=30)
        return r.json()
    except Exception as e:
        log.error(f"Erro na MAC API ({action}): {e}")
        return {"status": 500, "error": str(e)}


def buscar_perguntas():
    res = mac_call("list_questions", {"status": "UNANSWERED", "limit": 50})
    if res.get("status") == 200:
        return res["data"].get("questions", [])
    log.error(f"Erro ao buscar perguntas: {res.get('error')}")
    return []


def buscar_anuncio(item_id):
    res = mac_call("get_items", {"ids": [item_id], "include_description": True})
    if res.get("status") == 200:
        items = res.get("data") or []
        if items and items[0].get("code") == 200:
            return items[0]["body"]
    return None


def responder_pergunta(question_id, texto):
    res = mac_call("answer_question", {"question_id": question_id, "text": texto})
    return res.get("status") == 200


# ── Claude AI ───────────────────────────────────────────────────
def analisar_com_claude(pergunta, titulo, descricao):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": f"PERGUNTA DO CLIENTE: {pergunta}\n\nTÍTULO DO ANÚNCIO: {titulo}\n\nDESCRIÇÃO: {descricao or titulo}"
        }]
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            json=body, headers=headers, timeout=30
        )
        data = r.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        log.error(f"Erro no Claude: {e}")
        return "NAO_RESPONDER"


# ── Loop principal ───────────────────────────────────────────────
def processar_rodada():
    log.info("🔍 Verificando perguntas novas...")
    perguntas = buscar_perguntas()

    if not perguntas:
        log.info("✅ Nenhuma pergunta nova")
        return

    log.info(f"📬 {len(perguntas)} pergunta(s) encontrada(s)")

    for q in perguntas:
        pergunta_id   = q["id"]
        pergunta_text = q["text"]
        item_id       = q["item_id"]

        log.info(f'💬 Analisando: "{pergunta_text[:70]}"')

        anuncio = buscar_anuncio(item_id)
        if not anuncio:
            log.warning(f"⚠️  Anúncio {item_id} não encontrado, pulando")
            continue

        titulo    = anuncio.get("title", "")
        descricao = anuncio.get("description") or titulo

        resposta = analisar_com_claude(pergunta_text, titulo, descricao)

        if "NAO_RESPONDER" in resposta:
            log.warning(f"⏭️  Não respondida (sem certeza) | {titulo[:50]}")
        else:
            if responder_pergunta(pergunta_id, resposta):
                log.info(f'✅ Respondida: "{resposta[:60]}"')
            else:
                log.error(f"❌ Falha ao enviar resposta para pergunta {pergunta_id}")


def main():
    log.info("🚀 INFINITY BOT iniciado!")
    log.info(f"   Intervalo: {INTERVALO_SEG}s")

    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY não configurada!")
        return
    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY não configurada!")
        return

    while True:
        try:
            processar_rodada()
        except Exception as e:
            log.error(f"Erro inesperado: {e}")
        log.info(f"⏳ Aguardando {INTERVALO_SEG}s...")
        time.sleep(INTERVALO_SEG)


if __name__ == "__main__":
    main()
