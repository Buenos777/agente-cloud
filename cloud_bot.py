"""
Agente Trader Cloud — Híbrido Otimizado (Pullback + Breakout + MTF M15)
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

# ---------------------------------------------------------------------------
# Logging e Configuração
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("cloud_bot")

API_TOKEN = os.getenv("DERIV_API_TOKEN", "pat_f9c4151cc0d0206ee114bd367bdb56665b84b6a679c73c818c9403fb6813fece")
APP_ID = os.getenv("DERIV_APP_ID", "34aYJ2dEbNZrIqKNOZPrY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1573074553406619678/YA6sVZuw1aCbGPQgA29Klx_Z-_HwqGyJ6OLpt63ZuvnqPQS8JeI0TA360ekzwx4trxQL")

# ---------------------------------------------------------------------------
# Configuração da Estratégia Híbrida
# ---------------------------------------------------------------------------
BASKET = {
    "R_50": {"display": "Volatility 50 Index", "lot": 0.5},
    "R_100": {"display": "Volatility 100 Index", "lot": 1.0},
}
# Sessão Nobre: 21h-06h UTC (18h-03h BRT)
NOBRE_HOURS = [21, 22, 23, 0, 1, 2, 3, 4, 5] 
HISTORY_COUNT = 300 # Preciso de mais candles para MTF M15
COOLDOWN_SECONDS = 1800 # 30 min

# ---------------------------------------------------------------------------
# Funções de Indicadores (M5 + M15 Trend)
# ---------------------------------------------------------------------------
def ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for i in range(1, len(values)):
        result.append(alpha * values[i] + (1 - alpha) * result[-1])
    return result

def calculate_advanced_indicators(candles_m5: list[dict]) -> dict | None:
    # M15 Trend: Agrupa M5 em M15
    m15_candles = []
    for i in range(0, len(candles_m5), 3):
        chunk = candles_m5[i:i+3]
        if len(chunk) < 3: break
        m15_candles.append({
            "close": chunk[-1]["close"],
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk)
        })
    
    if len(m15_candles) < 50: return None
    
    # M15 Trend: EMA 9/21/50
    m15_closes = [c["close"] for c in m15_candles]
    e9, e21, e50 = ema(m15_closes, 9)[-1], ema(m15_closes, 21)[-1], ema(m15_closes, 50)[-1]
    m15_trend = 1 if (e9 > e21 > e50) else (-1 if e9 < e21 < e50 else 0)

    # M5 Indicadores
    closes = [c["close"] for c in candles_m5]
    opens = [c["open"] for c in candles_m5]
    highs = [c["high"] for c in candles_m5]
    lows = [c["low"] for c in candles_m5]
    
    # ATR 14
    tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atr = sum(tr[-14:]) / 14
    
    # EMAs
    e9, e21, e50, e200 = ema(closes, 9)[-2], ema(closes, 21)[-2], ema(closes, 50)[-2], ema(closes, 200)[-2]
    
    # RSI
    delta = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gain = sum([max(d, 0) for d in delta[-14:]]) / 14
    loss = sum([max(-d, 0) for d in delta[-14:]]) / 14
    rsi = 100 - (100 / (1 + (gain/(loss+1e-9))))
    
    # Rejeição
    body = abs(closes[-2] - opens[-2])
    lower_wick = min(closes[-2], opens[-2]) - lows[-2]
    upper_wick = highs[-2] - max(closes[-2], opens[-2])
    
    return {
        "m15_trend": m15_trend,
        "ema9": e9, "ema21": e21, "ema50": e50, "ema200": e200,
        "rsi": rsi, "atr": atr,
        "close_prev": closes[-2], "open_prev": opens[-2],
        "lower_wick": lower_wick, "upper_wick": upper_wick,
        "body": body
    }

# ---------------------------------------------------------------------------
# Estrutura do Bot (Resumida para foco na implementação)
# ---------------------------------------------------------------------------
# (O resto do código mantém a mesma estrutura de DerivWS e loop da versão anterior)
# ... [Omitido por brevidade: assume-se a estrutura mantida do cloud_bot.py original] ...

# ---------------------------------------------------------------------------
# Trading Loop Otimizado
# ---------------------------------------------------------------------------
async def trading_loop(ws):
    global last_trade_time
    while True:
        now_utc = datetime.now(timezone.utc)
        
        # 1. Filtro de Sessão Nobre
        if now_utc.hour not in NOBRE_HOURS:
            await asyncio.sleep(60)
            continue
            
        for sym, cfg in BASKET.items():
            # 2. Lógica Híbrida: Pullback + Wick + MTF
            candles = await ws.get_candles(sym, HISTORY_COUNT)
            ind = calculate_advanced_indicators(candles)
            if not ind or ind["atr"] == 0: continue
            
            # Condição Pullback + Wick + Trend M15
            sig = None
            body_prev = ind["body"]
            
            # CALL
            if ind["ema9"] > ind["ema21"] > ind["ema50"] and ind["close_prev"] > ind["ema9"] and ind["m15_trend"] == 1:
                if (ind["lower_wick"] / (body_prev + 1e-9)) >= 0.25:
                    if 44 <= ind["rsi"] <= 58: sig = "CALL"
            # PUT
            elif ind["ema9"] < ind["ema21"] < ind["ema50"] and ind["close_prev"] < ind["ema9"] and ind["m15_trend"] == -1:
                if (ind["upper_wick"] / (body_prev + 1e-9)) >= 0.25:
                    if 44 <= ind["rsi"] <= 58: sig = "PUT"
            
            if sig:
                # Executar ordem (ID gerado, etc...)
                # ...
                log.info(f"Sinal Institucional Híbrido: {sym} {sig}")
                await asyncio.sleep(60)
                
        await asyncio.sleep(5)
