#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desconto_full_recupera.py - RECUPERACAO POST-only.
Apos a rodada DELETE+POST, varios itens ficaram "pelados" (desconto apagado, POST falhou por lag do candidato).
Este script:
  - PULA quem ja tem desconto "started" (preserva os OK e o que foi feito a mao)
  - Faz POST mais fundo so nos "candidate" (os pelados), com varios retries (candidato demora a regenerar)
NAO deleta nada. Seguro rodar quantas vezes precisar.
Regua: 0v->40% | 1-2v->35% | >=3v->30% (mesma do agressivo), com clamp min/max.
DRY_RUN=true por padrao.
"""
import os, time, json, datetime, requests

MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"
MAC_API_KEY  = os.environ.get("MAC_API_KEY", "")
CONTA        = os.environ.get("CONTA", "60771984").strip()
DRY_RUN      = os.environ.get("DRY_RUN", "true").lower() == "true"
PAUSA        = float(os.environ.get("PAUSA", "2.0"))
RETRY_PAUSA  = float(os.environ.get("RETRY_PAUSA", "6.0"))
RETRIES      = int(os.environ.get("RETRIES", "4"))
DORMIR_FIM   = os.environ.get("DORMIR_FIM", "true").lower() == "true"
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "")

_hoje = datetime.date.today()
START_DATE  = os.environ.get("START_DATE",  _hoje.strftime("%Y-%m-%dT00:00:00"))
FINISH_DATE = os.environ.get("FINISH_DATE", (_hoje + datetime.timedelta(days=30)).strftime("%Y-%m-%dT23:59:59"))

PLANO = {
    "MLB1413592162": 35,
    "MLB6167284256": 35,
    "MLB3989908885": 30,
    "MLB3647046008": 35,
    "MLB5481853078": 30,
    "MLB3744632587": 30,
    "MLB4504176277": 35,
    "MLB3993704215": 35,
    "MLB4361014705": 35,
    "MLB3063824440": 40,
    "MLB3722151281": 40,
    "MLB4678641755": 40,
    "MLB3723261811": 40,
    "MLB4504150467": 35,
    "MLB6487806288": 40,
    "MLB5486609934": 35,
    "MLB4152561271": 35,
    "MLB1604316997": 35,
    "MLB2719367752": 30,
    "MLB4570794793": 30,
    "MLB4119680803": 30,
    "MLB4240721885": 30,
    "MLB1870702121": 35,
    "MLB2145031195": 35,
    "MLB3722135963": 35,
    "MLB4278165781": 30,
    "MLB4419647431": 35,
    "MLB4334439719": 35,
    "MLB1870688765": 40,
    "MLB3721977613": 40,
    "MLB3235749183": 30,
    "MLB3373608263": 40,
    "MLB1881218276": 30,
    "MLB1413578947": 35,
    "MLB4353799797": 40,
    "MLB3824072875": 35,
    "MLB1413611297": 30,
    "MLB1870707978": 35,
    "MLB5870602510": 30,
    "MLB3990062571": 30,
    "MLB2103149125": 35,
    "MLB5958109976": 35,
    "MLB1217397118": 40,
    "MLB4115532875": 40,
    "MLB4546363741": 40,
    "MLB1917614525": 30,
    "MLB2690140423": 35,
    "MLB3358389299": 35,
    "MLB1413617890": 30,
    "MLB5957463514": 30,
    "MLB4518795977": 30,
    "MLB5870499186": 35,
    "MLB1428907038": 35,
    "MLB4278231367": 30,
    "MLB4132869897": 30,
    "MLB4110153551": 35,
    "MLB6550334030": 40,
    "MLB5292858480": 30,
    "MLB3135255216": 35,
    "MLB1882759741": 30,
    "MLB3989945329": 40,
    "MLB4783810848": 30,
    "MLB1413588923": 30,
    "MLB5303220218": 40,
    "MLB4546366315": 40,
    "MLB1872805638": 40,
    "MLB1413617570": 35,
    "MLB4119628529": 30,
    "MLB1740670300": 30,
    "MLB4278084923": 40,
    "MLB6487823446": 40,
    "MLB1444722240": 30,
    "MLB5773627216": 35,
    "MLB3373239055": 40,
    "MLB3902587073": 40,
    "MLB4115596159": 30,
    "MLB6482758670": 35,
    "MLB6433828222": 30,
    "MLB4278087867": 30,
    "MLB3790166904": 35,
    "MLB3989975039": 30,
    "MLB3235768164": 30,
    "MLB1910557515": 35,
    "MLB1717304431": 40,
    "MLB1341164291": 30,
    "MLB4319949435": 40
}

def mac_call(action, params=None, meli_user_id=None, _retry429=True):
    params = dict(params or {})
    if meli_user_id: params["meli_user_id"] = meli_user_id
    try:
        r = requests.post(MAC_BASE_URL, json={"action": action, "params": params},
                          headers={"Content-Type": "application/json", "x-api-key": MAC_API_KEY}, timeout=30)
        j = r.json()
    except Exception as e:
        return {"status": 500, "error": str(e)}
    if j.get("status") == 429 and _retry429:
        time.sleep(5); return mac_call(action, params, None, _retry429=False)
    return j

def tg(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT, "text": msg}, timeout=15)
    except Exception: pass

def get_pd(mlb):
    res = mac_call("raw", {"method": "GET", "path": f"/seller-promotions/items/{mlb}?app_version=v2"}, CONTA)
    if res.get("status") != 200: return None
    for of in (res.get("data") or []):
        if of.get("type") == "PRICE_DISCOUNT": return of
    return None

def recupera(mlb, pct):
    pd = get_pd(mlb)
    if not pd: return ("SEM_PD", None, None)
    orig = pd.get("original_price")
    if not orig: return ("SEM_PRECO", None, None)
    if pd.get("status") == "started":
        return ("JA_TEM_pulado", orig, pd.get("price"))  # preserva
    mn = pd.get("min_discounted_price"); mx = pd.get("max_discounted_price")
    dp = round(orig * (1 - pct / 100.0), 2); clamp = ""
    if mx is not None and dp > mx: dp = mx; clamp = "(max)"
    if mn is not None and dp < mn: dp = mn; clamp = "(min)"
    if DRY_RUN: return ("DRY_post", orig, dp)
    for tent in range(RETRIES):
        res = mac_call("raw", {"method": "POST", "path": f"/seller-promotions/items/{mlb}?app_version=v2",
                               "body": {"promotion_type": "PRICE_DISCOUNT", "deal_price": dp,
                                        "start_date": START_DATE, "finish_date": FINISH_DATE}}, CONTA)
        if res.get("status") == 201:
            return ("OK" + clamp, orig, dp)
        msg = str((res.get("data") or {}).get("message", ""))
        if "candidate" in msg.lower() and tent < RETRIES - 1:
            time.sleep(RETRY_PAUSA); get_pd(mlb); continue
        return (f"ERRO {res.get('status')}: {msg[:50]}", orig, dp)

def main():
    modo = "DRY" if DRY_RUN else "REAL"
    itens = list(PLANO.items())
    print(f"=== RECUPERA POST-only | conta {CONTA} | {modo} | {len(itens)} itens ===")
    ok = pulado = erro = dry = 0
    for i, (mlb, pct) in enumerate(itens, 1):
        st, orig, dp = recupera(mlb, pct)
        tag = st.split()[0]
        if tag == "OK": ok += 1
        elif tag == "JA_TEM_pulado": pulado += 1
        elif tag == "DRY_post": dry += 1
        else: erro += 1
        print(f"[{i}/{len(itens)}] {mlb} -{pct}% {orig}->{dp} :: {st}")
        time.sleep(PAUSA)
    resumo = f"RECUPERA ({modo}): preenchidos={ok} ja_tinham={pulado} DRY={dry} ERRO={erro} de {len(itens)}."
    print("\n" + resumo); tg(resumo)
    if DORMIR_FIM:
        print("\nConcluido. Dormindo. Pode rodar de novo se sobrar pelado. Remova START_SCRIPT e Redeploy quando terminar.")
        while True: time.sleep(3600)

if __name__ == "__main__":
    main()
