"""
SAIR DA LOJA OFICIAL — tira anúncios de uma Loja Oficial (official_store_id -> 0)
=================================================================================
Mudança ESTRUTURAL em massa, separada do robô de ficha/regras.
- Conta-alvo configurável (default INFINITY 60771984).
- DRY_RUN=true (padrão) -> só simula e conta, NÃO escreve.
- TEST_N>0 -> mexe só nos N primeiros que precisam e PARA (teste).
- Loga cada mudança com o ID (auditável).
- Pula quem já está fora de loja oficial (não reescreve à toa).

Como rodar (no Railway, via START_SCRIPT=sair_loja_oficial.py):
  1) DRY_RUN=true                 -> vê QUANTOS sairiam (nada escrito)
  2) DRY_RUN=false + TEST_N=10    -> tira 10 de verdade, confere no site do ML
  3) DRY_RUN=false + TEST_N=0     -> tira TODOS

DEPLOY STAMP: 2026-06-07 — sair da loja oficial (one-shot).
"""
import os, time, logging, requests
from urllib.parse import quote

MAC_API_KEY  = os.environ.get("MAC_API_KEY", "")
MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"
CONTA        = int(os.environ.get("CONTA", "60771984"))            # INFINITY
DRY_RUN      = os.environ.get("DRY_RUN", "true").lower() == "true"
TEST_N       = int(os.environ.get("TEST_N", "10"))                  # >0 = só N e para
ALVO_STORE   = int(os.environ.get("ALVO_STORE", "0"))               # 0 = fora de loja oficial
PAUSA        = float(os.environ.get("PAUSA", "0.25"))

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("SairLoja")


def mac(action, params=None, meli_user_id=None):
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    try:
        r = requests.post(MAC_BASE_URL, json={"action": action, "params": params},
                          headers={"Content-Type": "application/json", "x-api-key": MAC_API_KEY},
                          timeout=60)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "error": r.text[:200]}
    except Exception as e:
        return {"status": 0, "error": str(e)}


def listar(cid):
    ids, scroll, erros = [], None, 0
    while True:
        q = ["search_type=scan", "limit=100", "status=active"]
        if scroll:
            q.append(f"scroll_id={quote(scroll, safe='')}")
        res = mac("raw", {"method": "GET", "path": f"/users/{cid}/items/search?" + "&".join(q)},
                  meli_user_id=cid)
        if res.get("status") != 200 or "data" not in res:
            erros += 1
            if erros >= 5:
                log.error(f"scan falhou 5x — parando. {res.get('error')}")
                break
            time.sleep(2); continue
        erros = 0
        d = res["data"]; scroll = d.get("scroll_id") or scroll
        lote = d.get("results", [])
        if not lote:
            break
        ids += lote
        time.sleep(PAUSA)
    return ids


def multiget(cid, bloco):
    res = mac("raw", {"method": "GET",
              "path": f"/items?ids={','.join(bloco)}&attributes=id,official_store_id,status"},
              meli_user_id=cid)
    if res.get("status") != 200 or "data" not in res:
        return None
    return [e["body"] for e in res["data"] if e.get("code") == 200 and e.get("body")]


def main():
    modo = "DRY-RUN (só simula)" if DRY_RUN else "APLICANDO (escrita real)"
    log.info(f"🏷️  Sair da Loja Oficial — conta {CONTA} | {modo} | "
             f"{'TESTE ' + str(TEST_N) if TEST_N else 'TODOS'} | alvo official_store_id={ALVO_STORE}")
    ids = listar(CONTA)
    log.info(f"{len(ids)} anúncios ativos na conta.")

    mudados = ja_fora = erros = vistos = 0
    for i in range(0, len(ids), 20):
        itens = multiget(CONTA, ids[i:i + 20])
        if itens is None:
            erros += 1; time.sleep(1); continue
        for it in itens:
            vistos += 1
            iid = it.get("id"); atual = it.get("official_store_id")
            if atual in (None, 0, ALVO_STORE):
                ja_fora += 1; continue
            if DRY_RUN:
                log.info(f"  🧪 {iid} | sairia da loja {atual} -> {ALVO_STORE}")
                mudados += 1
            else:
                r = mac("raw", {"method": "PUT", "path": f"/items/{iid}",
                                "body": {"official_store_id": ALVO_STORE}}, meli_user_id=CONTA)
                if r and r.get("status") in (200, 201):
                    log.info(f"  ✅ {iid} | loja {atual} -> {ALVO_STORE}")
                    mudados += 1
                else:
                    log.warning(f"  ❌ {iid}: {(r or {}).get('data') or (r or {}).get('error')}")
                    erros += 1
                time.sleep(PAUSA)
            if TEST_N and (mudados + erros) >= TEST_N:
                log.info(f"🏁 Fim do TESTE. mudados:{mudados} já_fora:{ja_fora} erros:{erros} vistos:{vistos}")
                return
    log.info(f"🏁 Concluído. mudados:{mudados} já_fora:{ja_fora} erros:{erros} vistos:{vistos}")


if __name__ == "__main__":
    main()
