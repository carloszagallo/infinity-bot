import os
import time
import requests
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Configurações ──────────────────────────────────────────────
MAC_API_KEY    = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
EMAIL_DESTINO  = os.environ.get("EMAIL_DESTINO", "carloszagallo@gmail.com")
MAC_BASE_URL   = "https://mcp.tiops.com.br/marketplace"
HEALTH_MINIMO  = float(os.environ.get("HEALTH_MINIMO", "0.80"))
LOTE_SIZE      = int(os.environ.get("LOTE_SIZE", "50"))

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
log = logging.getLogger("FichaTecnica")

SYSTEM_PROMPT = """Você é um especialista em autopeças e vai preencher atributos de anúncios do Mercado Livre.

Dado o TÍTULO do anúncio e a lista de ATRIBUTOS VAZIOS, preencha apenas os que conseguir deduzir com CERTEZA ABSOLUTA.

Regras ESTRITAS:
1. Só preencha se tiver 100% de certeza — melhor deixar vazio do que colocar errado
2. Tipo de veículo: se mencionar carro/caminhonete → "Carro/Caminhonete", moto → "Moto"
3. Condição: SEMPRE "Novo" (todos nossos produtos são novos)
4. É kit: se o título tiver "kit", "par", "jogo", "conjunto" → "Sim", caso contrário → "Não"
5. Origem: se mencionar "importado", "china" → "China"; se mencionar "nacional" → "Brasil"
6. Marca: extraia do título se estiver explícita
7. NUNCA invente informações que não estejam no título

Responda APENAS em JSON com o formato:
{"atributos": [{"id": "ATRIBUTO_ID", "value_name": "VALOR"}]}

Se não conseguir preencher nenhum, responda: {"atributos": []}"""


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
        log.error(f"Erro MAC API: {e}")
        return {"status": 500, "error": str(e)}


# ── Claude AI ──────────────────────────────────────────────────
def analisar_com_claude(titulo, atributos_vazios):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    atributos_str = "\n".join([f"- {a['id']}: {a['name']}" for a in atributos_vazios])
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": f"TÍTULO: {titulo}\n\nATRIBUTOS VAZIOS:\n{atributos_str}"
        }]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                         json=body, headers=headers, timeout=30)
        data = r.json()
        if "content" in data and data["content"]:
            import json
            texto = data["content"][0]["text"].strip()
            return json.loads(texto)
        return {"atributos": []}
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        return {"atributos": []}


# ── EMAIL ──────────────────────────────────────────────────────
def enviar_relatorio(stats_contas):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        return
    agora = datetime.now()
    linhas = ""
    for nome, s in stats_contas.items():
        linhas += f"""
        <tr><td colspan="2" style="background:#1a1a2e;color:#fff;padding:8px"><b>{nome}</b></td></tr>
        <tr style="background:#f9f9f9"><td>📋 Anúncios verificados</td><td><b>{s['verificados']}</b></td></tr>
        <tr><td>✅ Atributos preenchidos</td><td><b style="color:#2ecc71">{s['preenchidos']}</b></td></tr>
        <tr style="background:#f9f9f9"><td>⏭️ Sem alteração necessária</td><td><b>{s['sem_alteracao']}</b></td></tr>
        <tr><td>❌ Erros</td><td><b style="color:#e74c3c">{s['erros']}</b></td></tr>
        """

    corpo = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
    <h2 style="color:#1a1a2e">📋 Relatório Fichas Técnicas</h2>
    <p style="color:#666">{agora.strftime('%d/%m/%Y às %H:%M')}</p>
    <hr>
    <table width="100%" cellpadding="10" style="border-collapse:collapse">
    {linhas}
    </table>
    <hr>
    <p style="color:#999;font-size:12px">Infinity Bot • Fichas Técnicas Automáticas</p>
    </body></html>
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📋 Fichas Técnicas — {agora.strftime('%d/%m/%Y')}"
        msg["From"]    = GMAIL_USER
        msg["To"]      = EMAIL_DESTINO
        msg.attach(MIMEText(corpo, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.sendmail(GMAIL_USER, EMAIL_DESTINO, msg.as_string())
        log.info("📧 Relatório enviado!")
    except Exception as e:
        log.error(f"Erro email: {e}")


# ── PROCESSAR CONTA ────────────────────────────────────────────
def processar_conta(conta):
    cid  = conta["id"]
    nome = conta["nome"]
    stats = {"verificados": 0, "preenchidos": 0, "sem_alteracao": 0, "erros": 0}

    log.info(f"[{nome}] 🔍 Buscando anúncios com health < {HEALTH_MINIMO}...")

    offset = 0
    total_processados = 0
    MAX_ANUNCIOS = 500  # processa até 500 por rodada

    while total_processados < MAX_ANUNCIOS:
        res = mac_call("list_items", {
            "limit": LOTE_SIZE,
            "offset": offset,
            "status": "active"
        }, meli_user_id=cid)

        if res.get("status") != 200:
            log.error(f"[{nome}] Erro ao listar anúncios: {res.get('error')}")
            break

        items = res["data"].get("items", [])
        if not items:
            break

        for item in items:
            item_id = item.get("id")
            health  = float(item.get("health") or 0)
            titulo  = item.get("title", "")

            if health >= HEALTH_MINIMO:
                continue

            stats["verificados"] += 1

            # Identifica atributos vazios
            atributos_atuais = item.get("attributes", [])
            atributos_vazios = []

            campos_importantes = [
                "BRAND", "VEHICLE_TYPE", "ITEM_CONDITION",
                "IS_KIT", "ORIGIN", "MODEL"
            ]

            for attr in atributos_atuais:
                if attr.get("id") in campos_importantes:
                    val = attr.get("value_name")
                    if not val or val in ["", "null", "N/A"]:
                        atributos_vazios.append({
                            "id": attr.get("id"),
                            "name": attr.get("name", attr.get("id"))
                        })

            if not atributos_vazios:
                stats["sem_alteracao"] += 1
                continue

            # Claude preenche os atributos
            resultado = analisar_com_claude(titulo, atributos_vazios)
            novos_attrs = resultado.get("atributos", [])

            if not novos_attrs:
                stats["sem_alteracao"] += 1
                continue

            # Atualiza o anúncio
            try:
                res2 = mac_call("update_item", {
                    "item_id": item_id,
                    "attributes": novos_attrs
                }, meli_user_id=cid)

                if res2.get("status") in [200, 201]:
                    log.info(f"[{nome}] ✅ {item_id} | {len(novos_attrs)} atributo(s) | {titulo[:40]}")
                    stats["preenchidos"] += len(novos_attrs)
                else:
                    log.warning(f"[{nome}] ⚠️  {item_id} — {res2.get('error','erro')}")
                    stats["erros"] += 1
            except Exception as e:
                log.error(f"[{nome}] Erro ao atualizar {item_id}: {e}")
                stats["erros"] += 1

            time.sleep(0.5)  # evita rate limit

        total_processados += len(items)
        offset += LOTE_SIZE

        if len(items) < LOTE_SIZE:
            break

    log.info(f"[{nome}] ✅ Concluído: {stats}")
    return stats


# ── MAIN ───────────────────────────────────────────────────────
def main():
    log.info("📋 BOT FICHAS TÉCNICAS iniciado!")

    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY não configurada!"); return
    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY não configurada!"); return

    stats_contas = {}
    for conta in CONTAS_ML:
        try:
            stats_contas[conta["nome"]] = processar_conta(conta)
        except Exception as e:
            log.error(f"Erro na conta {conta['nome']}: {e}")

    enviar_relatorio(stats_contas)
    log.info("📋 Fichas técnicas concluídas! Aguardando próxima semana...")

    # Roda uma vez por semana (domingo 03:00)
    while True:
        agora = datetime.now()
        if agora.weekday() == 6 and agora.hour == 3 and agora.minute < 1:
            log.info("📋 Iniciando rodada semanal...")
            stats_contas = {}
            for conta in CONTAS_ML:
                try:
                    stats_contas[conta["nome"]] = processar_conta(conta)
                except Exception as e:
                    log.error(f"Erro na conta {conta['nome']}: {e}")
            enviar_relatorio(stats_contas)
        time.sleep(60)


if __name__ == "__main__":
    main()
