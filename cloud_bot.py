"""
Agente Trader Cloud — Híbrido Otimizado (Pullback + Wick + MTF M15)
Execução 24/7 na Sessão Nobre (18h-03h BRT).
"""

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("cloud_bot")

# --- CONFIGURAÇÃO ---
API_TOKEN = os.getenv("DERIV_API_TOKEN", "pat_f9c4151cc0d0206ee114bd367bdb56665b84b6a679c73c818c9403fb6813fece")
APP_ID = os.getenv("DERIV_APP_ID", "34aYJ2dEbNZrIqKNOZPrY")
ACCOUNT_TYPE = os.getenv("DERIV_ACCOUNT_TYPE", "demo")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

BASKET = {
    "R_50": {"display": "Volatility 50 Index", "lot": 0.5},
    "R_100": {"display": "Volatility 100 Index", "lot": 1.0},
}
NOBRE_HOURS = [21, 22, 23, 0, 1, 2, 3, 4, 5] # 18h-03h BRT
HISTORY_COUNT = 200
COOLDOWN_SECONDS = 1800

# --- ESTADO GLOBAL ---
session_stats = {"total": 0, "wins": 0, "losses": 0, "net_profit": 0.0}
account_balance = 0.0
account_currency = "USD"
active_account_id = ""
active_account_type = ""
last_trade_time = {sym: 0 for sym in BASKET}
last_heartbeat = 0 # Adicionado para garantir o log de vida
open_positions = []

# --- INDICADORES ---
def ema(values, span):
    if not values: return []
    alpha = 2.0 / (span + 1)
    res = [values[0]]
    for v in values[1:]: res.append(alpha * v + (1 - alpha) * res[-1])
    return res

def calculate_hybrid_indicators(candles_m5):
    if len(candles_m5) < 100: return None
    # M15 Trend (Simulado por agrupamento de M5)
    m15_closes = [candles_m5[i]["close"] for i in range(len(candles_m5)-1, 0, -3)][::-1]
    if len(m15_closes) < 30: return None
    me9_all, me21_all, me50_all = ema(m15_closes, 9), ema(m15_closes, 21), ema(m15_closes, 50)
    me9, me21, me50 = me9_all[-1], me21_all[-1], me50_all[-1]
    m15_trend = 1 if (me9 > me21 > me50) else (-1 if me9 < me21 < me50 else 0)

    # M5
    closes = [c["close"] for c in candles_m5]
    opens = [c["open"] for c in candles_m5]
    highs = [c["high"] for c in candles_m5]
    lows = [c["low"] for c in candles_m5]
    
    e9_all, e21_all, e50_all = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    e9, e21, e50 = e9_all[-2], e21_all[-2], e50_all[-2]
    
    tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atr = sum(tr[-14:]) / 14
    
    # RSI
    delta = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    g = sum([max(d, 0) for d in delta[-14:]]) / 14
    l = sum([max(-d, 0) for d in delta[-14:]]) / 14
    rsi = 100 - (100 / (1 + (g/(l+1e-9))))

    return {
        "m15_trend": m15_trend, "ema9": e9, "ema21": e21, "ema50": e50,
        "rsi": rsi, "atr": atr, "c_prev": closes[-2], "o_prev": opens[-2],
        "l_wick": min(closes[-2], opens[-2]) - lows[-2],
        "u_wick": highs[-2] - max(closes[-2], opens[-2]),
        "body": abs(closes[-2] - opens[-2])
    }

# --- INFRAESTRUTURA (WS & API) ---
async def send_discord_embed(embed):
    if not DISCORD_WEBHOOK: return
    try:
        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(DISCORD_WEBHOOK, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
    except Exception as e: log.warning(f"Discord falhou: {e}")

async def send_entry_alert(sym, stype, stake, ind):
    embed = {
        "title": f"🚀 [NOBRE] {BASKET[sym]['display']} — {stype}",
        "color": 0x2E86DE if stype == "CALL" else 0xE74C3C,
        "fields": [
            {"name": "Estratégia", "value": "Híbrida (Pullback + Wick + M15)", "inline": True},
            {"name": "Stake", "value": f"${stake:.2f}", "inline": True},
            {"name": "RSI / M15", "value": f"{ind['rsi']:.1f} / {'ALTA' if ind['m15_trend']==1 else 'BAIXA'}", "inline": True}
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await send_discord_embed(embed)

def fetch_deriv_otp():
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Deriv-App-ID": APP_ID, "Content-Type": "application/json"}
    req = urllib.request.Request("https://api.derivws.com/trading/v1/options/accounts", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        accs = json.loads(r.read().decode())["data"]
        target = next((a for a in accs if a["account_type"] == ACCOUNT_TYPE), accs[0])
    
    otp_req = urllib.request.Request(f"https://api.derivws.com/trading/v1/options/accounts/{target['account_id']}/otp", data=b"{}", headers=headers, method="POST")
    with urllib.request.urlopen(otp_req, timeout=15) as r:
        url = json.loads(r.read().decode())["data"]["url"]
    return url, target["account_id"], target["account_type"], float(target["balance"]), target["currency"]

class DerivWS:
    def __init__(self): self.ws = None; self._req_id = 0; self._pending = {}
    async def connect(self):
        import websockets
        url, aid, aty, bal, cur = await asyncio.to_thread(fetch_deriv_otp)
        self.account_id, self.account_type, self.balance, self.currency = aid, aty, bal, cur
        self.ws = await websockets.connect(url, ping_interval=30)
        self._recv_task = asyncio.create_task(self._recv_loop())
    async def _recv_loop(self):
        async for msg in self.ws:
            data = json.loads(msg)
            rid = data.get("req_id")
            if rid in self._pending: self._pending.pop(rid).set_result(data)
    async def send(self, req):
        self._req_id += 1; req["req_id"] = self._req_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[self._req_id] = fut
        await self.ws.send(json.dumps(req))
        return await asyncio.wait_for(fut, 15)
    async def get_candles(self, sym, count):
        r = await self.send({"ticks_history": sym, "count": count, "end": "latest", "style": "candles", "granularity": 300})
        return [{"open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "epoch": c["epoch"]} for c in r.get("candles", [])]
    async def buy(self, sym, stype, amount):
        p = await self.send({"proposal": 1, "amount": amount, "basis": "stake", "contract_type": stype, "currency": "USD", "duration": 5, "duration_unit": "m", "underlying_symbol": sym})
        if "error" in p: return p
        return await self.send({"buy": p["proposal"]["id"], "price": p["proposal"]["ask_price"]})

async def trading_loop(ws):
    global last_trade_time, last_heartbeat
    log.info("Iniciando Trading Loop Híbrido Otimizado...")
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour not in NOBRE_HOURS:
                if (time.time() - last_heartbeat) > 600:
                    log.info(f"Robô Ativo (Aguardando Sessão Nobre - {now.hour}h UTC).")
                    last_heartbeat = time.time()
                await asyncio.sleep(60); continue
            
            port = await ws.send({"portfolio": 1})
            open_count = len(port.get("portfolio", {}).get("contracts", []))
            if open_count >= 1: await asyncio.sleep(10); continue

            for sym, cfg in BASKET.items():
                if (time.time() - last_trade_time[sym]) < COOLDOWN_SECONDS: continue
                candles = await ws.get_candles(sym, HISTORY_COUNT)
                ind = calculate_hybrid_indicators(candles)
                if not ind: continue

                sig = None
                if ind["ema9"] > ind["ema21"] > ind["ema50"] and ind["m15_trend"] == 1:
                    if ind["c_prev"] > ind["ema9"] and (ind["l_wick"] / (ind["body"]+1e-9)) >= 0.25:
                        if 44 <= ind["rsi"] <= 58: sig = "CALL"
                elif ind["ema9"] < ind["ema21"] < ind["ema50"] and ind["m15_trend"] == -1:
                    if ind["c_prev"] < ind["ema9"] and (ind["u_wick"] / (ind["body"]+1e-9)) >= 0.25:
                        if 44 <= ind["rsi"] <= 58: sig = "PUT"
                
                if sig:
                    res = await ws.buy(sym, sig, cfg["lot"])
                    if "error" in res: log.error(f"Erro na ordem {sym}: {res['error']['message']}")
                    else:
                        log.info(f"✅ ORDEM EXECUTADA: {sym} {sig}")
                        last_trade_time[sym] = time.time()
                        await send_entry_alert(sym, sig, cfg["lot"], ind)
            await asyncio.sleep(10)
        except Exception as e: 
            log.error(f"Erro no Loop: {e}. Reiniciando conexão...")
            break # Sai do loop para a main() reconectar
        await asyncio.sleep(10) # Pausa entre operações fora do loop de erro

async def start_healthcheck():
    from aiohttp import web
    async def h(r): return web.json_response({"status": "online", "strategy": "Hybrid Nobre"})
    app = web.Application(); app.router.add_get("/", h)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

async def main():
    try: await start_healthcheck()
    except Exception as e: log.warning(f"Healthcheck falhou: {e}")
    while True:
        ws = DerivWS()
        try:
            await ws.connect()
            await trading_loop(ws)
        except Exception as e:
            log.error(f"Conexão falhou: {e}. Reconectando em 15s...")
            await asyncio.sleep(15)
        finally:
            if ws.ws:
                try:
                    await ws.ws.close()
                except Exception:
                    pass

if __name__ == "__main__": asyncio.run(main())
