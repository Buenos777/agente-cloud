"""
Agente Trader Cloud — Institutional Trend Pullback (Deriv API)
Execução 24/7 sem dependência de MT5, Windows ou máquina física.

Estratégia institucional original:
  - Cesta: Volatility 100 (R_100), Volatility 25 (R_25), Volatility 10 (R_10)
  - Timeframe: M5 (300s)
  - Indicadores: EMA 9, EMA 21, EMA 50, RSI 14, ATR 14
  - Gatilho: Pullback na EMA 21 com alinhamento de médias e RSI 40-60
  - Trava de Sábado: suspende operações no sábado (weekday 5)
  - Cooldown: 30 minutos por ativo
  - Max posições simultâneas: 1 (em toda a cesta)
"""

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Logging estruturado
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cloud_bot")

# ---------------------------------------------------------------------------
# Configuração (env vars com fallback seguro)
# ---------------------------------------------------------------------------
API_TOKEN = os.getenv("DERIV_API_TOKEN", "pat_f9c4151cc0d0206ee114bd367bdb56665b84b6a679c73c818c9403fb6813fece")
APP_ID = os.getenv("DERIV_APP_ID", "34aYJ2dEbNZrIqKNOZPrY")
ACCOUNT_TYPE = os.getenv("DERIV_ACCOUNT_TYPE", "demo")  # "demo" ou "real"
ACCOUNT_ID_OVERRIDE = os.getenv("DERIV_ACCOUNT_ID", "")  # Se vazio, descobre automático

# Webhook de alertas (Discord ou Telegram)
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
account_balance = 0.0
account_currency = "USD"
active_account_id = ""
active_account_type = ""

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

    # ATR = SMA(14) do TR, shifted 1 -> usamos valores até n-2
    atr_vals = []
    for i in range(len(tr_list)):
        if i < 13:
            atr_vals.append(None)
        else:
            atr_vals.append(sum(tr_list[i - 13 : i + 1]) / 14)

    if atr_vals[-2] is None:
        return None
    atr = atr_vals[-2]

    # ---------- EMAs (9, 21, 50) com ewm(span=N) ----------
    ema9_all = ema(closes, 9)
    ema21_all = ema(closes, 21)
    ema50_all = ema(closes, 50)

    # Valores da barra fechada (índice n - 2)
    ema_9 = ema9_all[-2]
    ema_21 = ema21_all[-2]
    ema_50 = ema50_all[-2]

    # ---------- RSI 14 ----------
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # ewm(alpha=1/14) para RSI
    alpha_rsi = 1.0 / 14.0

    def ewm_rsi(vals: list[float]) -> list[float]:
        res = [vals[0]]
        for i in range(1, len(vals)):
            res.append(alpha_rsi * vals[i] + (1 - alpha_rsi) * res[-1])
        return res

    avg_gain = ewm_rsi(gains)
    avg_loss = ewm_rsi(losses)

    # shifted 1: índice correspondente à barra fechada em deltas é n - 3
    ag = avg_gain[-2]
    al = avg_loss[-2]
    if al == 0:
        rsi = 100.0
    else:
        rs = ag / al
        rsi = 100.0 - (100.0 / (1.0 + rs))

    return {
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_50": ema_50,
        "rsi": rsi,
        "atr": atr,
        "close_prev": closes[-2],
        "open_prev": opens[-2],
        "high_prev": highs[-2],
        "low_prev": lows[-2],
    }


# ---------------------------------------------------------------------------
# Notificações (Discord / Telegram)
# ---------------------------------------------------------------------------

async def send_alert(message: str):
    """Envia alerta para Discord ou Telegram (se configurados)."""
    log.info(f"[ALERTA] {message.replace(chr(10), ' | ')}")

    if DISCORD_WEBHOOK:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                await session.post(
                    DISCORD_WEBHOOK,
                    json={"content": f"🤖 **Agente Cloud:**\n{message}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as e:
            log.warning(f"Falha webhook Discord: {e}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": f"🤖 Agente Cloud:\n{message}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as e:
            log.warning(f"Falha webhook Telegram: {e}")


# ---------------------------------------------------------------------------
# Descoberta de Contas e Gerador de OTP (Nova Deriv API)
# ---------------------------------------------------------------------------

def fetch_deriv_otp_url() -> tuple[str, str, str, float, str]:
    """
    Chama os endpoints REST oficiais da nova Deriv API:
    1. GET /trading/v1/options/accounts (identifica conta demo/real)
    2. POST /trading/v1/options/accounts/{id}/otp (gera WebSocket URL autenticada)
    """
    global active_account_id, active_account_type, account_balance, account_currency

    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Deriv-App-ID": APP_ID,
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }

    # 1. Buscar lista de contas
    req_acc = urllib.request.Request(
        "https://api.derivws.com/trading/v1/options/accounts",
        headers=headers,
    )
    with urllib.request.urlopen(req_acc, timeout=15) as resp:
        acc_data = json.loads(resp.read().decode())
        accounts = acc_data.get("data", [])

    if not accounts:
        raise RuntimeError("Nenhuma conta retornada pela Deriv API.")

    # Filtrar conta por ID explícito ou por tipo (demo / real)
    target = None
    if ACCOUNT_ID_OVERRIDE:
        for acc in accounts:
            if acc.get("account_id") == ACCOUNT_ID_OVERRIDE:
                target = acc
                break
    if not target:
        for acc in accounts:
            if acc.get("account_type") == ACCOUNT_TYPE:
                target = acc
                break
    if not target:
        target = accounts[0]

    acc_id = target.get("account_id")
    acc_type = target.get("account_type", "demo")
    bal = float(target.get("balance", 0.0))
    curr = target.get("currency", "USD")

    active_account_id = acc_id
    active_account_type = acc_type
    account_balance = bal
    account_currency = curr

    log.info(
        f"Conta Deriv selecionada: {acc_id} ({acc_type.upper()}) | "
        f"Saldo: ${bal:.2f} {curr}"
    )

    # 2. Gerar OTP para o WebSocket
    otp_req = urllib.request.Request(
        f"https://api.derivws.com/trading/v1/options/accounts/{acc_id}/otp",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(otp_req, timeout=15) as resp:
        otp_res = json.loads(resp.read().decode())
        ws_url = otp_res.get("data", {}).get("url")
        if not ws_url:
            raise RuntimeError(f"OTP não retornou URL: {otp_res}")

    return ws_url, acc_id, acc_type, bal, curr


# ---------------------------------------------------------------------------
# WebSocket — comunicação em tempo real com a Deriv
# ---------------------------------------------------------------------------

class DerivWS:
    """Wrapper para a conexão WebSocket autenticada via OTP da Deriv."""

    def __init__(self):
        self.ws = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None
        self.account_id = ""
        self.account_type = ""

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self):
        """Obtém URL OTP da nova Deriv API e conecta."""
        import websockets

        log.info("Obtendo URL autenticada da Deriv via OTP REST API...")
        ws_url, acc_id, acc_type, bal, curr = await asyncio.to_thread(fetch_deriv_otp_url)
        self.account_id = acc_id
        self.account_type = acc_type

        log.info(f"Conectando ao WebSocket da Deriv ({acc_type})...")
        self.ws = await asyncio.wait_for(
            websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**22,
            ),
            timeout=15,
        )
        log.info(f"WebSocket conectado com sucesso! [Conta {acc_id}]")
        self._recv_task = asyncio.create_task(self._recv_loop())

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

    async def get_balance(self) -> dict:
        """Consulta saldo atualizado."""
        return await self.send({"balance": 1})

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
        """
        Executa compra na nova Deriv API:
        1. Solicita proposta (proposal) usando underlying_symbol
        2. Executa a compra do ID gerado
        """
        prop_req = {
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": duration,
            "duration_unit": duration_unit,
            "underlying_symbol": symbol,
        }
        prop_res = await self.send(prop_req, timeout=15)
        if "error" in prop_res:
            return prop_res

        prop_id = prop_res.get("proposal", {}).get("id")
        ask_price = prop_res.get("proposal", {}).get("ask_price", amount)

        buy_res = await self.send({"buy": prop_id, "price": ask_price}, timeout=15)
        return buy_res

    async def get_open_contracts(self) -> dict:
        """Consulta contratos atualmente abertos na carteira."""
        return await self.send({"portfolio": 1})


# ---------------------------------------------------------------------------
# Loop principal de trading
# ---------------------------------------------------------------------------

async def trading_loop(ws: DerivWS):
    """Loop idêntico ao deriv_live_runner.py, operando 100% em nuvem."""
    global open_positions, account_balance, account_currency

    log.info("")
    log.info("=" * 70)
    log.info("   AGENTE IA CLOUD — INSTITUTIONAL TREND PULLBACK (DERIV API)")
    log.info(f"   Conta: {active_account_id} ({active_account_type.upper()}) | Saldo: ${account_balance:.2f} {account_currency}")
    log.info("   Ativos: R_100 (Vol100), R_25 (Vol25), R_10 (Vol10)")
    log.info("   Regra: Trava de Sábado | Cooldown 30min | Max 1 posição")
    log.info("   Deploy: Render Cloud — 24/7 sem PC nem MT5")
    log.info("=" * 70)
    log.info("")

    # Atualizar saldo inicial
    try:
        bal_resp = await ws.get_balance()
        bal = bal_resp.get("balance", {})
        account_balance = float(bal.get("balance", account_balance))
        account_currency = bal.get("currency", account_currency)
    except Exception as e:
        log.warning(f"Erro ao ler saldo inicial: {e}")

    last_heartbeat = 0
    cycle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # ---- Trava de Sábado (weekday 5) ----
            if now_utc.weekday() == 5:
                if (time.time() - last_heartbeat) >= 300:
                    log.info("[SÁBADO] Trava de fim de semana ativa. IA em repouso protetor.")
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
                    f"[STATUS OK] Conta {active_account_id} | Saldo: ${account_balance:.2f} {account_currency} "
                    f"| Posições: {pos_str} | Ciclo #{cycle}"
                )
                last_heartbeat = time.time()

            # ---- Verificar posições abertas ----
            try:
                portfolio = await ws.get_open_contracts()
                contracts = portfolio.get("portfolio", {}).get("contracts", [])
                open_positions = [
                    c for c in contracts
                    if (c.get("underlying_symbol") or c.get("symbol")) in BASKET
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
                    stake = cfg["lot"]
                    tp_dist = cfg["tp_atr"] * atr
                    sl_dist = cfg["sl_atr"] * atr
                    entry_price = ind["close_prev"]

                    log.info("")
                    log.info(">>> SINAL INSTITUCIONAL DETECTADO <<<")
                    log.info(f"    Ativo: {cfg['display']} ({sym}) | Tipo: {stype}")
                    log.info(f"    Preço: {entry_price:.4f} | ATR: {atr:.4f}")
                    log.info(f"    TP dist: {tp_dist:.4f} | SL dist: {sl_dist:.4f}")
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

                            log.info("")
                            log.info(">>> ORDEM EXECUTADA NA DERIV <<<")
                            log.info(
                                f"    Contrato: {contract_id} | Stake: ${buy_price} | Payout: ${payout}"
                            )

                            last_trade_time[sym] = time.time()

                            await send_alert(
                                f"✅ ORDEM EXECUTADA ({active_account_type.upper()})\n"
                                f"Ativo: {cfg['display']}\n"
                                f"Tipo: {stype}\n"
                                f"Stake: ${buy_price}\n"
                                f"Payout: ${payout}\n"
                                f"Contrato: {contract_id}"
                            )

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
# Healthcheck HTTP server (para rodar de GRAÇA no Render Web Service)
# ---------------------------------------------------------------------------
async def start_healthcheck_server():
    """Servidor HTTP mínimo na porta 8080 para o Render manter o serviço ativo."""
    from aiohttp import web

    async def handle_ping(request):
        pos_str = f"{len(open_positions)} abertas" if open_positions else "0 (aguardando gatilho)"
        return web.json_response({
            "status": "online",
            "bot": "Agente IA Cloud",
            "account_id": active_account_id,
            "account_type": active_account_type,
            "balance": f"${account_balance:.2f} {account_currency}",
            "positions": pos_str,
            "utc_time": datetime.now(timezone.utc).isoformat(),
        })

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Healthcheck HTTP server rodando na porta {port}")


# ---------------------------------------------------------------------------
# Entrypoint — Reconnect automático infinito
# ---------------------------------------------------------------------------

async def main():
    try:
        await start_healthcheck_server()
    except Exception as e:
        log.warning(f"Não conseguiu subir healthcheck: {e}")

    reconnect_delay = 5

    while True:
        ws = DerivWS()
        try:
            await ws.connect()
            reconnect_delay = 5
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
