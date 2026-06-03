import os
import time
import json
import logging
import requests
from datetime import datetime

# ── Configurações ──────────────────────────────────────────────
MAC_API_KEY    = os.environ.get("MAC_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

# Relatório via Telegram (SMTP não funciona no Railway — porta bloqueada).
# Reaproveita o MESMO token/chat do Atendente.
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAC_BASE_URL  = "https://mcp.tiops.com.br/marketplace"
HEALTH_MINIMO = float(os.environ.get("HEALTH_MINIMO", "0.80"))
LOTE_SIZE     = int(os.environ.get("LOTE_SIZE", "50"))

# Segurança e robustez
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() == "true"      # true = só simula, não grava
RUN_ON_START     = os.environ.get("RUN_ON_START", "false").lower() == "true"  # roda uma vez ao subir
PAUSA_PAGINA     = float(os.environ.get("PAUSA_PAGINA", "0.4"))
PAUSA_UPDATE     = float(os.environ.get("PAUSA_UPDATE", "0.5"))
MAX_ERROS_PAGINA = int(os.environ.get("MAX_ERROS_PAGINA", "5"))   # erros seguidos antes de desistir da conta
MAX_ANUNCIOS     = int(os.environ.get("MAX_ANUNCIOS", "25000"))   # teto de segurança por conta
INTERVALO_HORAS  = float(os.environ.get("INTERVALO_HORAS", "24"))  # repete a cada X horas (24 = todo dia; bota 6 no catch-up)

# ── Memória persistente (Railway Volume em /data) ──────────────
CHECKPOINT_DIR  = os.environ.get("CHECKPOINT_DIR", "/data")
CHECKPOINT_FILE = os.path.join(CHECKPOINT_DIR, "fichario_checkpoint.txt")

CONTAS_ML = [
    {"id": 60771984,  "nome": "INFINITY AUTOPARTS"},
    {"id": 233798434, "nome": "FREEDOM"},
    {"id": 554248644, "nome": "AUTOPARTSLIBERTY"},
    {"id": 1994875400,"nome": "DESTINYAUTOPARTS"},
]

# Atributos que vale a pena tentar completar (foco na ficha técnica de autopeça).
# O código só mexe nos que NÃO são read_only / hidden na categoria.
CAMPOS_ALVO = {"BRAND", "MODEL", "PART_NUMBER", "VEHICLE_TYPE", "ORIGIN",
               "OEM", "IS_KIT", "ITEM_CONDITION", "NUMBER_OF_FANS"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("FichaTecnica")

SYSTEM_PROMPT = """Você é um especialista em autopeças preenchendo atributos de anúncios do Mercado Livre.
Recebe o TÍTULO do anúncio e uma lista de ATRIBUTOS FALTANTES (cada um com seus valores permitidos, quando houver).
Preencha SOMENTE os atributos que conseguir deduzir com CERTEZA ABSOLUTA a partir do título.

Regras ESTRITAS:
1. Na dúvida, NÃO preencha — é melhor deixar de fora do que errar.
2. Quando o atributo tiver lista de valores permitidos, use EXATAMENTE um desses nomes.
3. Condição do item: sempre "Novo" (todos os nossos produtos são novos).
4. NUNCA invente informação que não esteja no título.

Responda APENAS em JSON, sem texto fora dele:
{"atributos": [{"id": "ATRIBUTO_ID", "value_name": "VALOR"}]}
Se não tiver certeza de nenhum, responda: {"atributos": []}"""


# ── MAC API ────────────────────────────────────────────────────
def mac_call(action, params=None, meli_user_id=None):
    payload = {"action": action, "params": params or {}}
    if meli_user_id:
        payload["meli_user_id"] = meli_user_id
    headers = {"Content-Type": "application/json", "x-api-key": MAC_API_KEY}
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=40)
        return r.json()
    except Exception as e:
        return {"status": 0, "error": f"exception: {e}"}


# ── Telegram ───────────────────────────────────────────────────
def tg_send(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN/CHAT_ID não configurados — relatório não enviado. Resumo no log:")
        log.info(texto.replace("<b>", "").replace("</b>", ""))
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram limita ~4096 chars por mensagem
    for i in range(0, len(texto), 3500):
        pedaco = texto[i:i + 3500]
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": pedaco,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=20)
            if r.status_code != 200:
                log.error(f"Erro Telegram ({r.status_code}): {r.text[:200]}")
        except Exception as e:
            log.error(f"Erro Telegram: {e}")


# ── Memória / checkpoint ───────────────────────────────────────
def carregar_checkpoint():
    """Retorna o set de chaves 'cid:iid' já tratadas em passadas anteriores."""
    try:
        if os.path.exists(CHECKPOINT_FILE):
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                feitos = {ln.strip() for ln in f if ln.strip()}
            log.info(f"🧠 Memória: retomando — {len(feitos)} itens já tratados serão pulados.")
            return feitos
    except Exception as e:
        log.warning(f"Não consegui ler o checkpoint ({e}). Começando do zero nesta passada.")
    return set()


def marcar_feito(chave):
    """Acrescenta uma chave 'cid:iid' ao arquivo de memória (append, à prova de queda)."""
    try:
        with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
            f.write(chave + "\n")
    except Exception as e:
        log.warning(f"Não consegui gravar no checkpoint ({e}).")


def limpar_checkpoint():
    """Zera a memória ao terminar uma passada completa nas 4 contas."""
    try:
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
            log.info("🧠 Passada completa concluída — memória limpa para a próxima rodada.")
    except Exception as e:
        log.warning(f"Não consegui limpar o checkpoint ({e}).")


# ── Atributos da categoria (com cache) ─────────────────────────
_cache_categoria = {}

def campos_da_categoria(category_id):
    """Atributos PREENCHÍVEIS da categoria (sem read_only/hidden), com valores permitidos."""
    if category_id in _cache_categoria:
        return _cache_categoria[category_id]

    res = mac_call("category_attributes", {"categoryId": category_id})
    if res.get("status") != 200 or not isinstance(res.get("data"), list):
        _cache_categoria[category_id] = []
        return []

    campos = []
    for a in res["data"]:
        tags = a.get("tags") or {}
        if tags.get("read_only") or tags.get("hidden"):
            continue
        if a.get("id") not in CAMPOS_ALVO:
            continue
        campos.append({
            "id": a.get("id"),
            "name": a.get("name", a.get("id")),
            "value_type": a.get("value_type"),
            "values": a.get("values") or [],
            "required": bool(tags.get("required") or tags.get("catalog_required") or tags.get("fixed")),
        })
    _cache_categoria[category_id] = campos
    return campos


# ── Claude AI ──────────────────────────────────────────────────
def analisar_com_claude(titulo, faltantes):
    linhas = []
    for f in faltantes:
        if f["values"]:
            permitidos = ", ".join(v.get("name", "") for v in f["values"])
            linhas.append(f"- {f['id']} ({f['name']}) — valores permitidos: {permitidos}")
        else:
            linhas.append(f"- {f['id']} ({f['name']}) — texto livre")
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": f"TÍTULO: {titulo}\n\nATRIBUTOS FALTANTES:\n" + "\n".join(linhas),
        }],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          json=body, headers=headers, timeout=40)
        data = r.json()
        if data.get("content"):
            texto = data["content"][0]["text"].strip()
            texto = texto.replace("```json", "").replace("```", "").strip()
            ini = texto.find("{")
            if ini > 0:
                texto = texto[ini:]
            try:
                return json.loads(texto).get("atributos", [])
            except json.JSONDecodeError:
                obj, _ = json.JSONDecoder().raw_decode(texto)  # ignora texto extra após o JSON
                return obj.get("atributos", [])
    except Exception as e:
        log.error(f"Erro Claude: {e}")
    return []


# ── Monta atributo final, mapeando value_id quando for lista ───
def montar_attr(campo, value_name):
    attr = {"id": campo["id"], "value_name": value_name}
    if campo["values"]:
        for v in campo["values"]:
            if (v.get("name") or "").strip().lower() == (value_name or "").strip().lower():
                attr["value_id"] = v.get("id")
                attr["value_name"] = v.get("name")
                break
        else:
            return None  # valor fora dos permitidos → descarta por segurança
    return attr


# ── Decide o que preencher em um anúncio ───────────────────────
def calcular_preenchimentos(item):
    titulo = item.get("title", "")
    category_id = item.get("category_id")
    if not category_id:
        return []

    campos = campos_da_categoria(category_id)
    if not campos:
        return []

    ja_tem = set()
    for attr in item.get("attributes", []):
        val = attr.get("value_name")
        if val and val not in ("", "null", "N/A"):
            ja_tem.add(attr.get("id"))

    faltantes = [c for c in campos if c["id"] not in ja_tem]
    if not faltantes:
        return []

    novos = []
    pra_ia = []
    for c in faltantes:
        if c["id"] == "ITEM_CONDITION":
            a = montar_attr(c, "Novo")
            if a:
                novos.append(a)
            continue
        if c["value_type"] == "list" and len(c["values"]) == 1:
            a = montar_attr(c, c["values"][0].get("name"))
            if a:
                novos.append(a)
            continue
        if c["id"] == "IS_KIT":
            ehkit = any(k in titulo.lower() for k in ["kit", "par", "jogo", "conjunto", "c/ 4", "c/4"])
            a = montar_attr(c, "Sim" if ehkit else "Não")
            if a:
                novos.append(a)
            continue
        pra_ia.append(c)

    if pra_ia:
        for sugestao in analisar_com_claude(titulo, pra_ia):
            campo = next((c for c in pra_ia if c["id"] == sugestao.get("id")), None)
            if campo:
                a = montar_attr(campo, sugestao.get("value_name"))
                if a:
                    novos.append(a)

    return novos


# ── Processa uma conta ─────────────────────────────────────────
def processar_conta(conta, feitos):
    cid, nome = conta["id"], conta["nome"]
    stats = {"verificados": 0, "preenchidos": 0, "sem_alteracao": 0,
             "erros": 0, "anuncios_atualizados": 0, "pulados": 0}
    log.info(f"[{nome}] 🔍 Buscando anúncios com health < {HEALTH_MINIMO}...")

    offset = 0
    erros_seguidos = 0
    total = None

    while offset < MAX_ANUNCIOS:
        res = mac_call("list_items", {"limit": LOTE_SIZE, "offset": offset, "status": "active"},
                       meli_user_id=cid)

        if res.get("status") != 200 or "data" not in res:
            erros_seguidos += 1
            log.warning(f"[{nome}] ⚠️ Falha na página offset={offset}: {res.get('error')} "
                        f"({erros_seguidos}/{MAX_ERROS_PAGINA})")
            if erros_seguidos >= MAX_ERROS_PAGINA:
                log.error(f"[{nome}] Muitos erros seguidos — interrompendo esta conta.")
                break
            time.sleep(2)   # respira e tenta a MESMA página de novo
            continue
        erros_seguidos = 0

        data = res["data"]
        if total is None:
            total = (data.get("paging") or {}).get("total")
            log.info(f"[{nome}] Total de anúncios ativos: {total}")

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            try:
                health = float(item.get("health") or 0)
                if health >= HEALTH_MINIMO:
                    continue
                stats["verificados"] += 1

                item_id = item.get("id")
                chave = f"{cid}:{item_id}"
                if chave in feitos:
                    stats["pulados"] += 1
                    continue

                novos = calcular_preenchimentos(item)
                if not novos:
                    stats["sem_alteracao"] += 1
                    marcar_feito(chave)
                    continue

                titulo = item.get("title", "")[:45]
                if DRY_RUN:
                    log.info(f"[{nome}] 🧪 [SIMULAÇÃO] {item_id} | {len(novos)} attr | "
                             f"{[a['id'] for a in novos]} | {titulo}")
                    stats["preenchidos"] += len(novos)
                    stats["anuncios_atualizados"] += 1
                    marcar_feito(chave)
                    continue

                res2 = mac_call("raw", {"method": "PUT", "path": f"/items/{item_id}", "body": {"attributes": novos}}, meli_user_id=cid)
                if res2.get("status") in (200, 201):
                    log.info(f"[{nome}] ✅ {item_id} | {len(novos)} attr | {titulo}")
                    stats["preenchidos"] += len(novos)
                    stats["anuncios_atualizados"] += 1
                    marcar_feito(chave)
                else:
                    log.warning(f"[{nome}] ⚠️ Update falhou {item_id}: {res2.get('error')}")
                    stats["erros"] += 1   # não marca → tenta de novo na próxima passada
                time.sleep(PAUSA_UPDATE)
            except Exception as e:
                log.error(f"[{nome}] Erro no item {item.get('id')}: {e}")
                stats["erros"] += 1

        offset += LOTE_SIZE
        if total is not None and offset >= total:
            break
        time.sleep(PAUSA_PAGINA)

    log.info(f"[{nome}] ✅ Concluído: {stats}")
    return stats


# ── Relatório via Telegram ─────────────────────────────────────
def enviar_relatorio(stats_contas):
    agora = datetime.now().strftime("%d/%m/%Y às %H:%M")
    modo = "🧪 SIMULAÇÃO (DRY_RUN)" if DRY_RUN else "🚀 PRODUÇÃO"
    linhas = [f"<b>📋 Relatório Fichas Técnicas</b>", f"{agora} • {modo}", ""]
    tot = {"verificados": 0, "preenchidos": 0, "anuncios_atualizados": 0,
           "sem_alteracao": 0, "erros": 0, "pulados": 0}
    for nome, s in stats_contas.items():
        for k in tot:
            tot[k] += s.get(k, 0)
        linhas.append(
            f"<b>{nome}</b>\n"
            f"  📋 Verificados (health baixo): {s['verificados']}\n"
            f"  ✅ Atributos preenchidos: {s['preenchidos']}\n"
            f"  📦 Anúncios atualizados: {s['anuncios_atualizados']}\n"
            f"  ⏭️ Sem alteração: {s['sem_alteracao']}\n"
            f"  🧠 Pulados (memória): {s.get('pulados', 0)}\n"
            f"  ❌ Erros: {s['erros']}"
        )
    linhas.append("")
    linhas.append(f"<b>TOTAL</b> — ✅ {tot['preenchidos']} attr em "
                  f"{tot['anuncios_atualizados']} anúncios • ❌ {tot['erros']} erros")
    tg_send("\n".join(linhas))


# ── Rodada completa ────────────────────────────────────────────
def rodar():
    log.info(f"📋 Iniciando rodada — modo {'SIMULAÇÃO' if DRY_RUN else 'PRODUÇÃO'}")
    feitos = carregar_checkpoint()
    stats_contas = {}
    completou_tudo = True
    for conta in CONTAS_ML:
        try:
            stats_contas[conta["nome"]] = processar_conta(conta, feitos)
        except Exception as e:
            log.error(f"Erro na conta {conta['nome']}: {e}")
            completou_tudo = False
            stats_contas[conta["nome"]] = {"verificados": 0, "preenchidos": 0, "sem_alteracao": 0,
                                           "erros": 1, "anuncios_atualizados": 0, "pulados": 0}
    enviar_relatorio(stats_contas)
    if completou_tudo:
        limpar_checkpoint()
    log.info("📋 Rodada concluída!")


# ── MAIN ───────────────────────────────────────────────────────
def main():
    log.info("📋 BOT FICHAS TÉCNICAS iniciado!")
    if not MAC_API_KEY:
        log.error("❌ MAC_API_KEY não configurada!"); return
    if not CLAUDE_API_KEY:
        log.error("❌ CLAUDE_API_KEY não configurada!"); return

    try:
        os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    except Exception as e:
        log.warning(f"⚠️ Sem persistência em {CHECKPOINT_DIR} ({e}). "
                    f"Sem o Volume montado, a memória não sobrevive a restart.")

    # Roda agora ao subir e repete a cada INTERVALO_HORAS (padrão 24h = todo dia).
    # Quando uma passada parar de preencher coisa nova, é sinal de que zerou o que dava pra zerar.
    while True:
        rodar()
        log.info(f"⏳ Próxima rodada em {INTERVALO_HORAS:.0f}h...")
        time.sleep(max(60, INTERVALO_HORAS * 3600))


if __name__ == "__main__":
    main()
