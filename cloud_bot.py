"""
Agente IA Cloud — Institutional Trend Pullback (Deriv API)
==========================================================
Versão cloud do deriv_live_runner.py. Mesma estratégia, mesmas regras,
zero dependência de MT5. Roda 24/7 em Render/Koyeb/Railway.

Estratégia:
  - EMA 9/21/50 (shifted 1 bar) sobre candles M5
  - RSI 14 (shifted 1 bar) zona 40-60
  - Pullback Institucional: preço toca EMA 21 e fecha além da EMA 9
  - ATR 14 para TP (1.8×) e SL (1.0×)
  - Trava de Sábado: zero operações no weekday 5
  - Cooldown de 30 min por ativo
  - Máximo 1 posição aberta na cesta

Ativos: R_100 (Vol100), R_25 (Vol25), R_10 (Vol10)
"""

import asyncio
import json
import os
import sys
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from collections import deque

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cloud_bot")

# ---------------------------------------------------------------------------
# Configuração (env vars para deploy seguro)
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv("DERIV_API_TOKEN", "")
APP_ID = os.getenv("DERIV_APP_ID", "1089")
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# Fallback endpoints caso o primário falhe
WS_ENDPOINTS = [
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
    f"wss://ws.binaryws.com/websockets/v3?app_id={APP_ID}",
    f"wss://green.binaryws.com/websockets/v3?app_id={APP_ID}",
    f"wss://blue.binaryws.com/websockets/v3?app_id={APP_ID}",
]

# Webhook de alertas (Discord ou Telegram — configurar depois)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Cesta de ativos — idêntica ao bot original
# Nomes na Deriv API: R_100, R_25, R_10
# ---------------------------------------------------------------------------
BASKET = {
    "R_100": {
        "display": "Volatility 100 Index",
        "lot": 1.0,
        "tp_atr": 1.8,
        "sl_atr": 1.0,
        "mult": 1.0,
    },
    "R_25": {
        "display": "Volatility 25 Index",
        "lot": 0.5,
        "tp_atr": 1.8,
        "sl_atr": 1.0,
        "mult": 0.5,
    },
    "R_10": {
        "display": "Volatility 10 Index",
        "lot": 0.5,
        "tp_atr": 1.8,
        "sl_atr": 1.0,
        "mult": 0.5,
    },
}

COOLDOWN_SECONDS = 6 * 300  # 30 minutos
CANDLE_GRANULARITY = 300  # M5 = 300 segundos
HISTORY_COUNT = 80  # candles necessários para indicadores
MAX_OPEN_POSITIONS = 1

# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------
last_trade_time: dict[str, float] = {sym: 0 for sym in BASKET}
last_evaluated_epoch: dict[str, int] = {sym: 0 for sym in BASKET}
open_positions: list[dict] = []
authorized = False
account_balance = 0.0
account_currency = "USD"

# ---------------------------------------------------------------------------
# Indicadores — idênticos ao deriv_live_runner.py
# ---------------------------------------------------------------------------

def ema(values: list[float], span: int) -> list[float]:
    """Exponential Moving Average idêntica ao pandas ewm(span=N).mean()."""
    if not values:
        return []
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for i in range(1, len(values)):
        result.append(alpha * values[i] + (1 - alpha) * result[-1])
    return result


def calculate_indicators(candles: list[dict]) -> dict | None:
    """
    Recebe lista de candles (dict com open, high, low, close, epoch).
    Retorna indicadores da barra FECHADA (penúltima), idêntico ao bot MT5.
    Precisa de pelo menos 60 candles.
    """
    if len(candles) < 60:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]

    n = len(closes)

    # ---------- True Range & ATR 14 (shifted 1) ----------
    tr_list = []
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_list.append(max(hl, hc, lc))

    # ATR = SMA(14) do TR, shifted 1 → usamos valores até n-2
    atr_vals = []
    for i in range(len(tr_list)):
        if i < 13:
            atr_vals.append(None)
        else:
            atr_vals.append(sum(tr_list[i - 13 : i + 1]) / 14)

    # shifted 1: ATR da barra fechada = atr_vals[-2] (penúltimo TR index)
    if atr_vals[-2] is None:
        return None
    atr = atr_vals[-2]

    # ---------- EMA 9/21/50 (shifted 1 — calculado sobre closes shifted 1) ----------
    # shifted 1 = ewm sobre closes[:-1] e pegar último valor
    closes_shifted = closes[:-1]  # exclui barra formando
    ema_9_all = ema(closes_shifted, 9)
    ema_21_all = ema(closes_shifted, 21)
    ema_50_all = ema(closes_shifted, 50)

    ema_9 = ema_9_all[-1]
    ema_21 = ema_21_all[-1]
    ema_50 = ema_50_all[-1]

    # ---------- RSI 14 (shifted 1) ----------
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    # shifted 1 → usar deltas até -2 (excluir a barra formando)
    deltas_shifted = deltas[:-1]

    gains = [max(d, 0) for d in deltas_shifted]
    losses = [max(-d, 0) for d in deltas_shifted]

    # SMA de 14 períodos (rolling) → pegar último
    if len(gains) < 14:
        return None
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14

    if avg_loss < 1e-8:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # Barra fechada = candles[-2], barra formando = candles[-1]
    closed = candles[-2]
    return {
        "closed_epoch": closed["epoch"],
        "atr": atr,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_50": ema_50,
        "rsi": rsi,
        "close_prev": closed["close"],
        "open_prev": closed["open"],
        "high_prev": closed["high"],
        "low_prev": closed["low"],
    }


# ---------------------------------------------------------------------------
# Alertas (Discord / Telegram)
# ---------------------------------------------------------------------------

async def send_alert(message: str):
    """Envia alerta para Discord webhook ou Telegram (não-bloqueante)."""
    log.info(f"[ALERTA] {message}")
    try:
        import aiohttp

        if DISCORD_WEBHOOK:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    DISCORD_WEBHOOK,
                    json={"content": message},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
    except Exception as e:
        log.warning(f"[ALERTA FALHOU] {e}")


# ---------------------------------------------------------------------------
# WebSocket — comunicação com a Deriv
# ---------------------------------------------------------------------------

class DerivWS:
    """Wrapper para a conexão WebSocket com a Deriv API."""

    def __init__(self):
        self.ws = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None
        self._endpoint_idx = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self):
        """Tenta conectar em cada endpoint até conseguir."""
        import websockets

        for attempt in range(len(WS_ENDPOINTS) * 2):
            idx = attempt % len(WS_ENDPOINTS)
            url = WS_ENDPOINTS[idx]
            try:
                log.info(f"Conectando a {url} ...")
                self.ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        ping_interval=25,
                        ping_timeout=10,
                        close_timeout=5,
                        max_size=2**22,
                    ),
                    timeout=15,
                )
                log.info("WebSocket conectado!")
                self._recv_task = asyncio.create_task(self._recv_loop())
                return
            except Exception as e:
                log.warning(f"Falha em {url}: {e}")
                await asyncio.sleep(2)

        raise ConnectionError("Não foi possível conectar a nenhum endpoint da Deriv.")

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            await self.ws.close()

    async def _recv_loop(self):
        """Loop que recebe mensagens e resolve futures pendentes."""
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                req_id = data.get("req_id")
                if req_id and req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(data)
        except Exception as e:
            log.error(f"recv_loop encerrado: {e}")
            # Resolver todos os futures pendentes com erro
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(str(e)))
            self._pending.clear()

    async def send(self, payload: dict, timeout: float = 15) -> dict:
        """Envia mensagem e aguarda resposta pelo req_id."""
        req_id = self._next_id()
        payload["req_id"] = req_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def authorize(self) -> dict:
        """Envia token de autorização."""
        resp = await self.send({"authorize": API_TOKEN})
        if "error" in resp:
            raise RuntimeError(f"Auth falhou: {resp['error']}")
        return resp

    async def get_candles(self, symbol: str, count: int = HISTORY_COUNT) -> list[dict]:
        """Busca candles M5 históricos."""
        resp = await self.send(
            {
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": count,
                "end": "latest",
                "style": "candles",
                "granularity": CANDLE_GRANULARITY,
            },
            timeout=20,
        )
        if "error" in resp:
            log.error(f"Erro ao buscar candles {symbol}: {resp['error']}")
            return []

        raw = resp.get("candles", [])
        return [
            {
                "epoch": c["epoch"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
            for c in raw
        ]

    async def buy_contract(
        self, symbol: str, contract_type: str, amount: float, duration: int = 5, duration_unit: str = "m"
    ) -> dict:
        """Compra contrato (CALL ou PUT) na Deriv."""
        resp = await self.send(
            {
                "buy": 1,
                "price": amount,
                "parameters": {
                    "contract_type": contract_type,  # "CALL" ou "PUT"
                    "symbol": symbol,
                    "basis": "stake",
                    "amount": amount,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "currency": "USD",
                },
            },
            timeout=15,
        )
        return resp

    async def get_balance(self) -> dict:
        return await self.send({"balance": 1})

    async def get_open_contracts(self) -> dict:
        return await self.send({"portfolio": 1})


# ---------------------------------------------------------------------------
# Loop principal — mesma lógica do deriv_live_runner.py
# ---------------------------------------------------------------------------

async def trading_loop(ws: DerivWS):
    """Loop idêntico ao main() do deriv_live_runner.py, adaptado para API."""
    global open_positions, account_balance, account_currency

    log.info("")
    log.info("=" * 70)
    log.info("   AGENTE IA CLOUD — INSTITUTIONAL TREND PULLBACK (DERIV API)")
    log.info("   Ativos: R_100 (Vol100), R_25 (Vol25), R_10 (Vol10)")
    log.info("   Regra: Trava de Sábado | Cooldown 30min | Max 1 posição")
    log.info("   Deploy: Render/Koyeb/Railway — 24/7 sem PC")
    log.info("=" * 70)
    log.info("")

    # Buscar saldo inicial
    try:
        bal_resp = await ws.get_balance()
        bal = bal_resp.get("balance", {})
        account_balance = float(bal.get("balance", 0))
        account_currency = bal.get("currency", "USD")
        log.info(f"[CONECTADO] Saldo: ${account_balance:.2f} {account_currency}")
    except Exception as e:
        log.warning(f"Não conseguiu ler saldo: {e}")

    last_heartbeat = 0
    cycle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # ---- Trava de Sábado (weekday 5) ----
            if now_utc.weekday() == 5:
                if (time.time() - last_heartbeat) >= 300:
                    log.info(f"[SÁBADO] Trava de fim de semana ativa. IA em repouso protetor.")
                    last_heartbeat = time.time()
                await asyncio.sleep(30)
                continue

            # ---- Heartbeat a cada 60s ----
            if (time.time() - last_heartbeat) >= 60:
                pos_str = (
                    f"{len(open_positions)} em aberto"
                    if open_positions
                    else "0 (Aguardando gatilho)"
                )
                log.info(
                    f"[STATUS OK] Saldo: ${account_balance:.2f} {account_currency} "
                    f"| Posições: {pos_str} | Ciclo #{cycle}"
                )
                last_heartbeat = time.time()

            # ---- Se já tem posição aberta, não abre outra ----
            # Verificar contratos abertos via API
            try:
                portfolio = await ws.get_open_contracts()
                contracts = portfolio.get("portfolio", {}).get("contracts", [])
                open_positions = [
                    c for c in contracts
                    if c.get("symbol") in BASKET
                ]
            except Exception:
                pass

            if len(open_positions) >= MAX_OPEN_POSITIONS:
                await asyncio.sleep(4)
                cycle += 1
                continue

            # ---- Escanear ativos da cesta ----
            for sym, cfg in BASKET.items():
                if len(open_positions) >= MAX_OPEN_POSITIONS:
                    break

                now_epoch = time.time()
                if (now_epoch - last_trade_time[sym]) < COOLDOWN_SECONDS:
                    continue

                # Buscar candles M5
                candles = await ws.get_candles(sym, HISTORY_COUNT)
                if not candles or len(candles) < 60:
                    continue

                # Não reavaliar a mesma barra
                closed_epoch = candles[-2]["epoch"]
                if last_evaluated_epoch[sym] == closed_epoch:
                    continue
                last_evaluated_epoch[sym] = closed_epoch

                # Calcular indicadores
                ind = calculate_indicators(candles)
                if ind is None:
                    continue

                atr = ind["atr"]
                if atr is None or atr <= 0:
                    continue

                stype = None

                # ---- Setup Comprovado de Pullback Institucional ----
                if ind["ema_9"] > ind["ema_21"] and ind["ema_21"] > ind["ema_50"]:
                    if (
                        ind["low_prev"] <= ind["ema_21"]
                        and ind["close_prev"] > ind["ema_9"]
                        and 40 <= ind["rsi"] <= 60
                    ):
                        stype = "CALL"

                elif ind["ema_9"] < ind["ema_21"] and ind["ema_21"] < ind["ema_50"]:
                    if (
                        ind["high_prev"] >= ind["ema_21"]
                        and ind["close_prev"] < ind["ema_9"]
                        and 40 <= ind["rsi"] <= 60
                    ):
                        stype = "PUT"

                if stype is not None:
                    # Calcular stake e enviar ordem
                    stake = cfg["lot"]
                    tp_dist = cfg["tp_atr"] * atr
                    sl_dist = cfg["sl_atr"] * atr
                    entry_price = ind["close_prev"]

                    log.info(f"")
                    log.info(f">>> SINAL DETECTADO <<<")
                    log.info(
                        f"    Ativo: {cfg['display']} ({sym}) | Tipo: {stype}"
                    )
                    log.info(
                        f"    Preço: {entry_price:.4f} | ATR: {atr:.4f}"
                    )
                    log.info(
                        f"    TP dist: {tp_dist:.4f} | SL dist: {sl_dist:.4f}"
                    )
                    log.info(
                        f"    EMA9={ind['ema_9']:.4f} EMA21={ind['ema_21']:.4f} "
                        f"EMA50={ind['ema_50']:.4f} RSI={ind['rsi']:.1f}"
                    )

                    try:
                        result = await ws.buy_contract(
                            symbol=sym,
                            contract_type=stype,
                            amount=stake,
                            duration=5,
                            duration_unit="m",
                        )

                        if "error" in result:
                            log.error(
                                f"[ORDEM FALHOU] {result['error'].get('message', result['error'])}"
                            )
                            await send_alert(
                                f"❌ ORDEM FALHOU {stype} {cfg['display']}: "
                                f"{result['error'].get('message', '')}"
                            )
                        else:
                            buy_info = result.get("buy", {})
                            contract_id = buy_info.get("contract_id", "?")
                            buy_price = buy_info.get("buy_price", stake)
                            payout = buy_info.get("payout", 0)

                            log.info(f"")
                            log.info(f">>> ORDEM EXECUTADA <<<")
                            log.info(
                                f"    Contrato: {contract_id} | Stake: ${buy_price} | Payout: ${payout}"
                            )

                            last_trade_time[sym] = time.time()

                            await send_alert(
                                f"✅ ORDEM EXECUTADA\n"
                                f"Ativo: {cfg['display']}\n"
                                f"Tipo: {stype}\n"
                                f"Stake: ${buy_price}\n"
                                f"Payout: ${payout}\n"
                                f"Contrato: {contract_id}"
                            )

                            # Atualizar saldo
                            try:
                                bal_resp = await ws.get_balance()
                                account_balance = float(
                                    bal_resp.get("balance", {}).get("balance", account_balance)
                                )
                            except Exception:
                                pass

                            break

                    except Exception as e:
                        log.error(f"[ERRO ORDEM] {e}")

            await asyncio.sleep(2)
            cycle += 1

        except Exception as e:
            log.error(f"[ERRO LOOP] {e}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entrypoint — Reconnect automático infinito
# ---------------------------------------------------------------------------

async def main():
    reconnect_delay = 5

    while True:
        ws = DerivWS()
        try:
            await ws.connect()

            # Autorizar
            log.info("Autorizando com token API...")
            auth = await ws.authorize()
            auth_data = auth.get("authorize", {})
            log.info(
                f"Autorizado! Conta: {auth_data.get('loginid', '?')} | "
                f"Email: {auth_data.get('email', '?')}"
            )

            reconnect_delay = 5  # reset delay após sucesso

            # Rodar loop de trading
            await trading_loop(ws)

        except KeyboardInterrupt:
            log.info("[ENCERRADO] Bot pausado pelo usuário.")
            break
        except Exception as e:
            log.error(f"[DESCONECTADO] {e}")
            log.info(f"Reconectando em {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 120)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
