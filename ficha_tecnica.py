#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desconto_full_afeta.py - Aplica PRICE_DISCOUNT VARIAVEL nos anuncios "afeta metrica"
do Full (conta INFINITY 60771984), pra forcar venda antes da avaliacao de metrica do dia 15.

- Regua variavel por item (mapa PLANO: MLB -> %), definida com o Claude (giro x tempo x preco).
- Respeita o limite da ML por item (min/max do PRICE_DISCOUNT) -> faz CLAMP, nunca grava fora.
- DRY_RUN por padrao (NAO grava). Poe DRY_RUN=false pra valer.
- Roda no FICHARIO via START_SCRIPT=desconto_full_afeta.py
- Os 5 caros sem giro NAO entram aqui (sao pra REMOVER do Full a mao) - listados no fim.

Envs:
  MAC_API_KEY   (ja existe no Railway)
  CONTA         (default 60771984 = INFINITY)
  DRY_RUN       (default true)            -> false pra gravar
  TESTE_N       (default 0 = todos)       -> em REAL limita aos N primeiros (teste)
  START_DATE    (default hoje 00:00, LOCAL SEM fuso)
  FINISH_DATE   (default hoje+30d 23:59:59)
  PAUSA         (default 1.5s entre itens)
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (opcional)
"""
import os, time, json, datetime, requests

MAC_BASE_URL = "https://mcp.tiops.com.br/marketplace"
MAC_API_KEY  = os.environ.get("MAC_API_KEY", "")
CONTA        = os.environ.get("CONTA", "60771984").strip()
DRY_RUN      = os.environ.get("DRY_RUN", "true").lower() == "true"
TESTE_N      = int(os.environ.get("TESTE_N", "0"))
PAUSA        = float(os.environ.get("PAUSA", "1.5"))
DORMIR_FIM   = os.environ.get("DORMIR_FIM", "true").lower() == "true"  # fica Online sem re-rodar
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "")

_hoje = datetime.date.today()
START_DATE  = os.environ.get("START_DATE",  _hoje.strftime("%Y-%m-%dT00:00:00"))
FINISH_DATE = os.environ.get("FINISH_DATE", (_hoje + datetime.timedelta(days=30)).strftime("%Y-%m-%dT23:59:59"))

# ---- PLANO: MLB -> % de desconto (regua variavel) ----
PLANO = {
    "MLB1413592162": 22,
    "MLB6167284256": 15,
    "MLB3989908885": 12,
    "MLB3647046008": 22,
    "MLB5481853078": 15,
    "MLB3744632587": 15,
    "MLB4504176277": 15,
    "MLB3993704215": 15,
    "MLB4361014705": 15,
    "MLB3063824440": 30,
    "MLB3722151281": 30,
    "MLB4678641755": 30,
    "MLB3723261811": 30,
    "MLB4504150467": 15,
    "MLB6487806288": 30,
    "MLB5486609934": 18,
    "MLB4152561271": 22,
    "MLB1604316997": 18,
    "MLB2719367752": 12,
    "MLB4570794793": 12,
    "MLB4119680803": 15,
    "MLB4240721885": 15,
    "MLB1870702121": 18,
    "MLB2145031195": 18,
    "MLB3722135963": 22,
    "MLB4278165781": 12,
    "MLB4419647431": 22,
    "MLB4334439719": 18,
    "MLB1870688765": 30,
    "MLB3721977613": 30,
    "MLB3235749183": 15,
    "MLB3373608263": 30,
    "MLB1881218276": 15,
    "MLB1413578947": 22,
    "MLB4353799797": 30,
    "MLB3824072875": 18,
    "MLB1413611297": 15,
    "MLB1870707978": 22,
    "MLB5870602510": 12,
    "MLB3990062571": 15,
    "MLB2103149125": 22,
    "MLB5958109976": 22,
    "MLB1217397118": 30,
    "MLB4115532875": 30,
    "MLB4546363741": 30,
    "MLB1917614525": 15,
    "MLB2690140423": 22,
    "MLB3358389299": 22,
    "MLB1413617890": 15,
    "MLB5957463514": 15,
    "MLB4518795977": 12,
    "MLB5870499186": 22,
    "MLB1428907038": 15,
    "MLB4278231367": 15,
    "MLB4132869897": 12,
    "MLB4110153551": 22,
    "MLB6550334030": 30,
    "MLB5292858480": 15,
    "MLB3135255216": 15,
    "MLB1882759741": 12,
    "MLB3989945329": 30,
    "MLB4783810848": 12,
    "MLB1413588923": 15,
    "MLB5303220218": 30,
    "MLB4546366315": 30,
    "MLB1872805638": 30,
    "MLB1413617570": 22,
    "MLB4119628529": 15,
    "MLB1740670300": 12,
    "MLB4278084923": 30,
    "MLB6487823446": 30,
    "MLB1444722240": 12,
    "MLB5773627216": 22,
    "MLB3373239055": 30,
    "MLB3902587073": 25,
    "MLB4115596159": 15,
    "MLB6482758670": 22,
    "MLB6433828222": 12,
    "MLB4278087867": 12,
    "MLB3790166904": 22,
    "MLB3989975039": 12,
    "MLB3235768164": 15,
    "MLB1910557515": 22,
    "MLB1717304431": 30,
    "MLB1341164291": 15,
    "MLB4319949435": 30
}

# ---- 5 caros sem giro: REMOVER do Full a mao (nao descontar) ----
REMOVER = [
    {
        "mlb": "MLB5307324762",
        "prod": "Bomba Combustível Gasolina Volvo T5 Xc",
        "price": 604.89
    },
    {
        "mlb": "MLB4504297407",
        "prod": "Tampa De Valvula Lado Direito Motor Am",
        "price": 601.53
    },
    {
        "mlb": "MLB1413618932",
        "prod": "Radiador Resfriador De Oleo Journey Ch",
        "price": 638.48
    },
    {
        "mlb": "MLB3782578127",
        "prod": "Motor Limpador Gol Fox G5 G6 2010/ 1vw",
        "price": 324.74
    },
    {
        "mlb": "MLB4353312627",
        "prod": "Trocador Calor Resfriador Óleo Motor J",
        "price": 602.16
    }
]

def mac_call(action, params=None, meli_user_id=None):
    params = dict(params or {})
    if meli_user_id:
        params["meli_user_id"] = meli_user_id
    payload = {"action": action, "params": params}
    headers = {"Content-Type": "application/json", "x-api-key": MAC_API_KEY}
    try:
        r = requests.post(MAC_BASE_URL, json=payload, headers=headers, timeout=30)
        return r.json()
    except Exception as e:
        return {"status": 500, "error": str(e)}

def tg(msg):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=15)
    except Exception:
        pass

def detalhes_pd(mlb):
    """(original_price, min_dp, max_dp) do PRICE_DISCOUNT do item, ou None."""
    res = mac_call("raw", {"method": "GET",
                           "path": f"/seller-promotions/items/{mlb}?app_version=v2"},
                   meli_user_id=CONTA)
    if res.get("status") != 200:
        return None
    for of in (res.get("data") or []):
        if of.get("type") == "PRICE_DISCOUNT":
            return (of.get("original_price"), of.get("min_discounted_price"), of.get("max_discounted_price"))
    return None

def aplicar(mlb, pct):
    d = detalhes_pd(mlb)
    if not d:
        return ("SEM_PD", None, None)
    orig, mn, mx = d
    if not orig:
        return ("SEM_PRECO", None, None)
    dp = round(orig * (1 - pct / 100.0), 2)
    clamp = ""
    if mx is not None and dp > mx:   # meu desconto raso demais p/ a ML -> usa o minimo dela
        dp = mx; clamp = " (clamp->max)"
    if mn is not None and dp < mn:   # meu desconto fundo demais -> trava no minimo permitido
        dp = mn; clamp = " (clamp->min)"
    if DRY_RUN:
        return ("DRY" + clamp, orig, dp)
    res = mac_call("raw", {"method": "POST",
                           "path": f"/seller-promotions/items/{mlb}?app_version=v2",
                           "body": {"promotion_type": "PRICE_DISCOUNT", "deal_price": dp,
                                    "start_date": START_DATE, "finish_date": FINISH_DATE}},
                   meli_user_id=CONTA)
    if res.get("status") == 201:
        return ("OK" + clamp, orig, dp)
    return (f"ERRO {res.get('status')}: {str((res.get('data') or {}).get('message',''))[:60]}", orig, dp)

def main():
    modo = "DRY (simulacao)" if DRY_RUN else "REAL (gravando)"
    itens = list(PLANO.items())
    if (not DRY_RUN) and TESTE_N > 0:
        itens = itens[:TESTE_N]
    print(f"=== Desconto Full afeta-metrica | conta {CONTA} | {modo} | {len(itens)} itens | janela {START_DATE} -> {FINISH_DATE} ===")
    ok = dry = erro = 0
    for i, (mlb, pct) in enumerate(itens, 1):
        st, orig, dp = aplicar(mlb, pct)
        tag = st.split()[0]
        if tag == "OK": ok += 1
        elif tag == "DRY": dry += 1
        else: erro += 1
        print(f"[{i}/{len(itens)}] {mlb} -{pct}% {orig}->{dp} :: {st}")
        time.sleep(PAUSA)
    resumo = (f"Desconto Full ({modo}): OK={ok} DRY={dry} ERRO={erro} de {len(itens)} itens.\n"
              f"Janela {START_DATE} -> {FINISH_DATE}.\n"
              f"LEMBRETE - remover do Full a mao (5 caros s/ giro): " + ", ".join(r["mlb"] for r in REMOVER))
    print("\n" + resumo)
    tg(resumo)
    if DORMIR_FIM:
        print("\nConcluido. Dormindo pra NAO reiniciar/re-rodar no Railway.")
        print("Quando terminar: remova START_SCRIPT do FICHARIO (volta pro ficha_tecnica.py) e Redeploy.")
        while True:
            time.sleep(3600)

if __name__ == "__main__":
    main()
