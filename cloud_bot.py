"""
Agente Trader Cloud — Híbrido Otimizado (Pullback + Wick + MTF M15) (Deriv API)
Execução 24/7 sem dependência de MT5, Windows ou máquina física.

Estratégia híbrida otimizada:
  - Cesta: Volatility 50 (R_50), Volatility 100 (R_100)
  - Timeframe: M5 (300s) com confirmação M15 (agrupado)
  - Indicadores: EMA 9, EMA 21, EMA 50, RSI 14, ATR 14 (M5) + EMA 9/21/50 (M15)
  - Gatilho: Pullback com Wick Rejection + alinhamento M15
  - Sessão Nobre: opera apenas das 21h-05h UTC (18h-02h BRT)
  - Cooldown: 30 minutos por ativo
  - Max posições simultâneas: 1 (em toda a cesta)
  - Notificações: Discord Rich Embeds com Entrada, Desfecho (WIN/LOSS), Placar e Saldo
  - Alta Disponibilidade: Ping keepalive Deriv, reconexão automática e resolução resiliente
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

# Webhook do Discord (Notificações de Entrada, WIN/LOSS e Placar)
DEFAULT_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1573482390771335291/w42RelASpP2-g-PU8Laln7ZZ0yFf9m4rzsB14D3nYiOm6xvP9mGeFJ3FET2CCA-JDBfi"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", DEFAULT_DISCORD_WEBHOOK)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Cesta de ativos — Estratégia Híbrida Otimizada
# Nomes na Deriv API: R_50, R_100
# ---------------------------------------------------------------------------
BASKET: dict[str, dict] = {
    "R_50": {
        "display": "Volatility 50 Index",
        "lot": 0.5,
        "tp_atr": 1.8,
        "sl_atr": 1.0,
        "mult": 0.5,
    },
    "R_100": {
        "display": "Volatility 100 Index",
        "lot": 1.0,
        "tp_atr": 1.8,
        "sl_atr": 1.0,
        "mult": 1.0,
    },
}

COOLDOWN_SECONDS: int = 6 * 300  # 30 minutos
CANDLE_GRANULARITY: int = 300  # M5 = 300 segundos
HISTORY_COUNT: int = 200  # candles necessários para indicadores (mais para M15 agrupado)
MAX_OPEN_POSITIONS: int = 1

# Sessão Nobre: 21h-05h UTC (18h-02h BRT)
NOBRE_HOURS: list[int] = [21, 22, 23, 0, 1, 2, 3, 4, 5]

# ---------------------------------------------------------------------------
# Estado global & Estatísticas da Sessão
# ---------------------------------------------------------------------------
last_trade_time: dict[str, float] = {sym: 0 for sym in BASKET}
last_evaluated_epoch: dict[str, int] = {sym: 0 for sym in BASKET}
open_positions: list[dict] = []
account_balance: float = 0.0
account_currency: str = "USD"
active_account_id: str = ""
active_account_type: str = ""

# Estatísticas para relatório no Discord
session_stats: dict = {
    "total": 0,
    "wins": 0,
    "losses": 0,
    "net_profit": 0.0,
}

# ---------------------------------------------------------------------------
# Indicadores — versão original (referência, mantida para compatibilidade)
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
    tr_list: list[float] = []
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_list.append(max(hl, hc, lc))

    # ATR = SMA(14) do TR, shifted 1 -> usamos valores até n-2
    atr_vals: list[float | None] = []
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
# Indicadores — Estratégia Híbrida (M5 + MTF M15 + Wick Rejection)
# ---------------------------------------------------------------------------

def calculate_hybrid_indicators(candles_m5: list[dict]) -> dict | None:
    """
    Calcula indicadores da estratégia híbrida otimizada:
    1. M15 trend: agrupa candles M5 em M15, calcula EMA 9/21/50 no M15
    2. M5: EMA 9/21/50, RSI 14, ATR 14 (shifted 1)
    3. Wick rejection: lower_wick, upper_wick, body da barra fechada (penúltima)

    Precisa de pelo menos 100 candles M5.
    """
    if len(candles_m5) < 100:
        return None

    # =====================================================================
    # PARTE 1: M15 Trend (agrupamento de 3 candles M5 em 1 candle M15)
    # =====================================================================
    # Agrupa candles de 3 em 3, descartando os candles que sobram no início
    num_m15 = len(candles_m5) // 3
    if num_m15 < 30:
        return None

    m15_candles: list[dict] = []
    start_idx = len(candles_m5) - (num_m15 * 3)
    for i in range(start_idx, len(candles_m5), 3):
        group = candles_m5[i : i + 3]
        if len(group) < 3:
            break
        m15_candles.append({
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "epoch": group[-1]["epoch"],
        })

    m15_closes = [c["close"] for c in m15_candles]
    if len(m15_closes) < 30:
        return None

    m15_ema9_all = ema(m15_closes, 9)
    m15_ema21_all = ema(m15_closes, 21)
    m15_ema50_all = ema(m15_closes, 50)

    m15_ema9 = m15_ema9_all[-1]
    m15_ema21 = m15_ema21_all[-1]
    m15_ema50 = m15_ema50_all[-1]

    if m15_ema9 > m15_ema21 > m15_ema50:
        m15_trend = 1
    elif m15_ema9 < m15_ema21 < m15_ema50:
        m15_trend = -1
    else:
        m15_trend = 0

    # =====================================================================
    # PARTE 2: M5 — EMA 9/21/50, RSI 14, ATR 14 (shifted 1)
    # =====================================================================
    closes = [c["close"] for c in candles_m5]
    highs = [c["high"] for c in candles_m5]
    lows = [c["low"] for c in candles_m5]
    opens = [c["open"] for c in candles_m5]
    n = len(closes)

    # ---------- True Range & ATR 14 (shifted 1) ----------
    tr_list: list[float] = []
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_list.append(max(hl, hc, lc))

    atr_vals: list[float | None] = []
    for i in range(len(tr_list)):
        if i < 13:
            atr_vals.append(None)
        else:
            atr_vals.append(sum(tr_list[i - 13 : i + 1]) / 14)

    if atr_vals[-2] is None:
        return None
    atr = atr_vals[-2]

    # ---------- EMAs M5 (9, 21, 50) ----------
    ema9_all = ema(closes, 9)
    ema21_all = ema(closes, 21)
    ema50_all = ema(closes, 50)

    ema_9 = ema9_all[-2]
    ema_21 = ema21_all[-2]
    ema_50 = ema50_all[-2]

    # ---------- RSI 14 (ewm alpha=1/14, shifted 1) ----------
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses_rsi = [max(-d, 0.0) for d in deltas]

    alpha_rsi = 1.0 / 14.0

    def ewm_rsi(vals: list[float]) -> list[float]:
        res = [vals[0]]
        for i in range(1, len(vals)):
            res.append(alpha_rsi * vals[i] + (1 - alpha_rsi) * res[-1])
        return res

    avg_gain = ewm_rsi(gains)
    avg_loss = ewm_rsi(losses_rsi)

    ag = avg_gain[-2]
    al = avg_loss[-2]
    if al == 0:
        rsi = 100.0
    else:
        rs = ag / al
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # =====================================================================
    # PARTE 3: Wick Rejection da barra fechada (penúltima)
    # =====================================================================
    close_prev = closes[-2]
    open_prev = opens[-2]
    high_prev = highs[-2]
    low_prev = lows[-2]
    body = abs(close_prev - open_prev)
    lower_wick = min(close_prev, open_prev) - low_prev
    upper_wick = high_prev - max(close_prev, open_prev)

    return {
        # M15
        "m15_trend": m15_trend,
        "m15_ema9": m15_ema9,
        "m15_ema21": m15_ema21,
        "m15_ema50": m15_ema50,
        # M5 EMAs
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_50": ema_50,
        # M5 Osciladores
        "rsi": rsi,
        "atr": atr,
        # Preço da barra fechada
        "close_prev": close_prev,
        "open_prev": open_prev,
        "high_prev": high_prev,
        "low_prev": low_prev,
        # Wick Rejection
        "body": body,
        "lower_wick": lower_wick,
        "upper_wick": upper_wick,
    }


# ---------------------------------------------------------------------------
# Notificações no Discord (Rich Embeds Profissionais)
# ---------------------------------------------------------------------------

async def send_discord_embed(embed: dict):
    """Envia um Rich Embed para o Discord de forma assíncrona, com retry e proteção contra 429."""
    if not DISCORD_WEBHOOK:
        return

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")

    def _post():
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/Buenos777/agente-cloud, 1.0)",
        }
        for attempt in range(4):
            req = urllib.request.Request(
                DISCORD_WEBHOOK,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait_time = 2.0
                    try:
                        raw = e.read().decode("utf-8", errors="ignore")
                        data = json.loads(raw)
                        wait_time = float(data.get("retry_after", 2.0))
                    except Exception:
                        retry_hdr = e.headers.get("Retry-After")
                        if retry_hdr:
                            try:
                                wait_time = float(retry_hdr)
                            except Exception:
                                pass
                    wait_time = max(0.5, min(wait_time, 10.0))
                    log.warning(
                        f"Discord 429 Rate Limit (tentativa {attempt + 1}/4). Aguardando {wait_time + 0.5:.2f}s..."
                    )
                    time.sleep(wait_time + 0.5)
                else:
                    log.warning(f"Falha webhook Discord HTTP {e.code}: {e.reason}")
                    break
            except Exception as e:
                log.warning(f"Falha conexão webhook Discord (tentativa {attempt + 1}/4): {e}")
                time.sleep(1.0)

    await asyncio.to_thread(_post)


async def send_discord_entry_alert(
    contract_id: int,
    display: str,
    symbol: str,
    stype: str,
    stake: float,
    payout: float,
    entry_price: float,
    ind: dict,
    balance: float,
    currency: str,
):
    """Notificação completa enviada no momento exato em que a ordem é executada."""
    is_call = (stype == "CALL")
    direction_str = "🟢 CALL (COMPRA / ALTA)" if is_call else "🔴 PUT (VENDA / BAIXA)"
    color = 0x2E86DE if is_call else 0xE74C3C

    # Montar descrição do gatilho conforme indicadores disponíveis
    m15_label = "ALTA" if ind.get("m15_trend", 0) == 1 else ("BAIXA" if ind.get("m15_trend", 0) == -1 else "NEUTRO")
    wick_ratio = ""
    if ind.get("body", 0) > 0:
        if stype == "CALL":
            wick_ratio = f"Wick Ratio (lower/body): `{ind.get('lower_wick', 0) / ind['body']:.2f}`"
        else:
            wick_ratio = f"Wick Ratio (upper/body): `{ind.get('upper_wick', 0) / ind['body']:.2f}`"

    embed = {
        "title": f"⚡ [NOVA ENTRADA] {display} — {stype}",
        "description": "O Agente IA identificou um alinhamento híbrido (Pullback + Wick + MTF M15) e executou uma nova ordem na Deriv.",
        "color": color,
        "fields": [
            {"name": "Ativo", "value": f"**{display}** (`{symbol}`)", "inline": True},
            {"name": "Direção", "value": direction_str, "inline": True},
            {"name": "Duração", "value": "⏱️ **5 Minutos (M5)**", "inline": True},
            {"name": "Valor da Entrada (Stake)", "value": f"💵 **${stake:.2f} {currency}**", "inline": True},
            {"name": "Retorno Potencial (Payout)", "value": f"🎯 **${payout:.2f} {currency}**", "inline": True},
            {"name": "Preço de Entrada (Spot)", "value": f"📊 `{entry_price:.4f}`", "inline": True},
            {
                "name": "🧠 Gatilho Técnico Híbrido (Pullback + Wick + M15)",
                "value": (
                    f"• M5: EMA 9 (`{ind.get('ema_9', 0):.2f}`) | EMA 21 (`{ind.get('ema_21', 0):.2f}`) | EMA 50 (`{ind.get('ema_50', 0):.2f}`)\n"
                    f"• M15 Trend: **{m15_label}** (EMA9 `{ind.get('m15_ema9', 0):.2f}` | EMA21 `{ind.get('m15_ema21', 0):.2f}` | EMA50 `{ind.get('m15_ema50', 0):.2f}`)\n"
                    f"• RSI (14): `{ind.get('rsi', 0):.1f}` (faixa 44–58 confirmada)\n"
                    f"• Volatilidade ATR: `{ind.get('atr', 0):.4f}`\n"
                    f"• {wick_ratio}\n"
                    f"• Padrão: Pullback com Wick Rejection + confirmação MTF M15"
                ),
                "inline": False,
            },
            {"name": "ID do Contrato", "value": f"`#{contract_id}`", "inline": True},
            {"name": "Saldo Atual da Banca", "value": f"🏦 **${balance:.2f} {currency}**", "inline": True},
            {"name": "Conta Deriv", "value": f"`{active_account_id}` ({active_account_type.upper()})", "inline": True},
        ],
        "footer": {"text": "Agente Trader Cloud • Híbrido Otimizado • Deriv API • Sessão Nobre"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await send_discord_embed(embed)


async def send_discord_outcome_alert(
    contract_id: int,
    display: str,
    symbol: str,
    stype: str,
    stake: float,
    is_win: bool,
    profit: float,
    profit_pct: float,
    entry_price: float,
    exit_price: float,
    session_stats: dict,
    balance: float,
    currency: str,
):
    """Notificação sequencial enviada assim que a vela M5 fecha, com WIN/LOSS, estatísticas e saldo."""
    total = session_stats["total"]
    wins = session_stats["wins"]
    losses = session_stats["losses"]
    net_profit = session_stats["net_profit"]
    winrate = (wins / total * 100.0) if total > 0 else 0.0

    if is_win:
        title = f"🎯 [RESULTADO: WIN] {display} — Vitória Confirmada! 🎉"
        color = 0x00FF88  # Verde Neon
        result_badge = "✅ **VITÓRIA (WIN)**"
        profit_badge = f"💰 **+${profit:.2f} {currency} (+{profit_pct:.1f}%)**"
        desc = "A projeção híbrida (Pullback + Wick + M15) foi confirmada com sucesso no fechamento da vela M5!"
    else:
        title = f"🛑 [RESULTADO: LOSS] {display} — Operação Encerrada"
        color = 0xFF3838  # Vermelho
        result_badge = "❌ **DERROTA (LOSS)**"
        profit_badge = f"🔻 **-${abs(profit):.2f} {currency} (-100%)**"
        desc = "O mercado retraiu contra a posição no fechamento da vela M5."

    embed = {
        "title": title,
        "description": desc,
        "color": color,
        "fields": [
            {"name": "Resultado", "value": result_badge, "inline": True},
            {"name": "Lucro / Prejuízo", "value": profit_badge, "inline": True},
            {"name": "Entrada ➔ Saída", "value": f"`{entry_price:.4f}` ➔ `{exit_price:.4f}`", "inline": True},
            {"name": "🏆 Placar de Hoje", "value": f"**{wins} Wins — {losses} Losses**", "inline": True},
            {"name": "📊 Assertividade (Winrate)", "value": f"**{winrate:.1f}%**", "inline": True},
            {"name": "📈 Lucro Líquido Acumulado", "value": f"**${net_profit:+.2f} {currency}**", "inline": True},
            {"name": "💳 Saldo Atual da Banca", "value": f"🏦 **${balance:.2f} {currency}**", "inline": True},
            {"name": "Ativo / Direção", "value": f"{display} ({stype})", "inline": True},
            {"name": "Contrato", "value": f"`#{contract_id}`", "inline": True},
            {
                "name": "⏳ Gestão de Risco e Próximo Passo",
                "value": f"• Cooldown protetor de 30 minutos ativado em **{display}**.\n• IA continua varrendo a cesta em busca de novas oportunidades alinhadas.",
                "inline": False,
            },
        ],
        "footer": {"text": "Agente Trader Cloud • Híbrido Otimizado • Deriv API • Sessão Nobre"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await send_discord_embed(embed)


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
    """Wrapper resiliente para a conexão WebSocket autenticada via OTP da Deriv."""

    def __init__(self):
        self.ws = None
        self._req_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self.account_id: str = ""
        self.account_type: str = ""

    @property
    def is_connected(self) -> bool:
        if self.ws is None or self._recv_task is None:
            return False
        if self._recv_task.done():
            return False
        state = getattr(self.ws, "state", None)
        if state is not None:
            return state.name == "OPEN"
        return not getattr(self.ws, "closed", True)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self):
        """Obtém URL OTP da nova Deriv API e conecta com heartbeat ativo."""
        import websockets

        log.info("Obtendo URL autenticada da Deriv via OTP REST API...")
        ws_url, acc_id, acc_type, bal, curr = await asyncio.to_thread(fetch_deriv_otp_url)
        self.account_id = acc_id
        self.account_type = acc_type

        log.info(f"Conectando ao WebSocket da Deriv ({acc_type})...")
        self.ws = await asyncio.wait_for(
            websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=5,
                max_size=2**22,
            ),
            timeout=15,
        )
        log.info(f"WebSocket conectado com sucesso! [Conta {acc_id}]")
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Envia ping da Deriv a cada 25 segundos para manter a sessão sempre viva."""
        try:
            while self.is_connected:
                await asyncio.sleep(25)
                if self.is_connected:
                    try:
                        await self.send({"ping": 1}, timeout=10)
                    except Exception as e:
                        log.warning(f"Keepalive Deriv ping falhou: {e}")
                        break
        except asyncio.CancelledError:
            pass

    async def close(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("WebSocket fechado"))
        self._pending.clear()

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
            log.warning(f"recv_loop encerrado: {e}")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(str(e)))
            self._pending.clear()

    async def send(self, payload: dict, timeout: float = 15) -> dict:
        """Envia mensagem e aguarda resposta pelo req_id com validação de conexão."""
        if not self.is_connected:
            raise ConnectionError("WebSocket Deriv desconectado")

        req_id = self._next_id()
        payload["req_id"] = req_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self.ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
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
        return await self.send({"portfolio": 1}, timeout=15)

    async def get_contract_status(self, contract_id: int) -> dict:
        """Consulta detalhes e status de um contrato específico (aberto ou finalizado)."""
        return await self.send({"proposal_open_contract": 1, "contract_id": contract_id}, timeout=15)


# ---------------------------------------------------------------------------
# Fallback Standalone para Consulta de Contratos & Sincronização
# ---------------------------------------------------------------------------

async def fetch_contract_status_standalone(contract_id: int) -> dict:
    """Consulta o status de um contrato conectando diretamente à Deriv (à prova de falha do WS principal)."""
    try:
        def _get_otp_url():
            headers = {
                "Authorization": f"Bearer {API_TOKEN}",
                "Deriv-App-ID": APP_ID,
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(
                f"https://api.derivws.com/trading/v1/options/accounts/{active_account_id or 'DOT90318004'}/otp",
                data=b"{}",
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())["data"]["url"]

        url = await asyncio.to_thread(_get_otp_url)
        import websockets
        async with websockets.connect(url, timeout=10) as tmp_ws:
            await tmp_ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "req_id": 999}))
            raw = await asyncio.wait_for(tmp_ws.recv(), timeout=10)
            return json.loads(raw)
    except Exception as e:
        log.warning(f"fetch_contract_status_standalone falhou para #{contract_id}: {e}")
        return {}


async def sync_session_stats(ws: DerivWS):
    """Sincroniza as estatísticas da sessão (últimas 24h) diretamente com a Deriv."""
    global session_stats
    try:
        from datetime import timedelta
        # Busca a partir das últimas 24h para contemplar a virada de dia UTC
        start_date_str = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
        resp = await ws.send({
            "profit_table": 1,
            "description": 1,
            "date_from": start_date_str,
            "limit": 50,
        }, timeout=15)
        txs = resp.get("profit_table", {}).get("transactions", [])
        total_pnl = 0.0
        w = 0
        l = 0
        for t in txs:
            buy = float(t.get("buy_price", 0))
            sell = float(t.get("sell_price", 0))
            pnl = sell - buy
            total_pnl += pnl
            if pnl > 0:
                w += 1
            else:
                l += 1
        session_stats["total"] = len(txs)
        session_stats["wins"] = w
        session_stats["losses"] = l
        session_stats["net_profit"] = total_pnl
        log.info(f"Estatísticas sincronizadas com Deriv: {w}W - {l}L | PnL Líquido: ${total_pnl:+.2f}")
    except Exception as e:
        log.warning(f"Não conseguiu sincronizar profit_table da Deriv: {e}")


# ---------------------------------------------------------------------------
# Monitoramento do Desfecho (WIN/LOSS) em Segundo Plano
# ---------------------------------------------------------------------------

async def monitor_contract_outcome(
    contract_id: int,
    symbol: str,
    display: str,
    stype: str,
    stake: float,
    payout: float,
    entry_price: float,
    duration_min: int = 5,
    ws: DerivWS | None = None,
):
    """
    Acompanha o contrato até a expiração na Deriv.
    Assim que finaliza (status != 'open' ou is_expired == 1),
    calcula Win/Loss, estatísticas acumuladas e envia notificação rica no Discord.
    Possui fallback automático para nunca abandonar a consulta em silêncio.
    """
    global session_stats, account_balance, account_currency

    log.info(f"Monitorando contrato #{contract_id} ({display} {stype}) - {duration_min}m...")
    wait_initial = max(5, (duration_min * 60) - 30)
    await asyncio.sleep(wait_initial)

    for attempt in range(120):  # tenta por até 10 minutos após o término previsto
        try:
            resp: dict = {}
            if ws and ws.is_connected:
                try:
                    resp = await ws.get_contract_status(contract_id)
                except Exception:
                    resp = {}

            if not resp or "error" in resp or "proposal_open_contract" not in resp:
                resp = await fetch_contract_status_standalone(contract_id)

            poc = resp.get("proposal_open_contract", {})
            status = poc.get("status", "open")
            is_expired = poc.get("is_expired", 0)

            if is_expired == 1 or status in ("won", "lost"):
                # Se ainda estiver "open" com is_expired=1, aguarda 2s para Deriv consolidar status final
                if status == "open" and is_expired == 1:
                    await asyncio.sleep(2)
                    try:
                        if ws and ws.is_connected:
                            fresh = await ws.get_contract_status(contract_id)
                        else:
                            fresh = await fetch_contract_status_standalone(contract_id)
                        if fresh and "proposal_open_contract" in fresh:
                            poc = fresh["proposal_open_contract"]
                            status = poc.get("status", status)
                    except Exception:
                        pass

                raw_profit = poc.get("profit")
                profit = float(raw_profit) if raw_profit is not None else 0.0

                # REGRA INFALÍVEL: lucro positivo OU status won = WIN
                is_win = (status == "won") or (profit > 0.0)

                # Se status ainda for indefinido e profit for 0, aguarda mais uma iteração
                if not is_win and status not in ("won", "lost") and profit == 0.0:
                    await asyncio.sleep(2)
                    continue

                if is_win and profit <= 0.0:
                    profit = float(payout - stake)
                elif not is_win and profit >= 0.0:
                    profit = -float(stake)

                exit_price = float(poc.get("exit_spot", poc.get("current_spot", entry_price)))
                raw_pct = poc.get("profit_percentage")
                profit_pct = float(raw_pct) if raw_pct is not None else ((profit / stake) * 100.0)

                # Sincronizar histórico do dia diretamente com o extrato da Deriv
                try:
                    if ws and ws.is_connected:
                        await sync_session_stats(ws)
                    else:
                        session_stats["total"] += 1
                        if is_win:
                            session_stats["wins"] += 1
                        else:
                            session_stats["losses"] += 1
                        session_stats["net_profit"] += profit
                except Exception:
                    session_stats["total"] += 1
                    if is_win:
                        session_stats["wins"] += 1
                    else:
                        session_stats["losses"] += 1
                    session_stats["net_profit"] += profit

                # Atualizar saldo da conta
                try:
                    if ws and ws.is_connected:
                        bal_resp = await ws.get_balance()
                        bal = bal_resp.get("balance", {})
                        account_balance = float(bal.get("balance", account_balance))
                        account_currency = bal.get("currency", account_currency)
                except Exception:
                    pass

                log.info(
                    f">>> [DESFECHO DO CONTRATO #{contract_id}] "
                    f"{'WIN' if is_win else 'LOSS'} | Lucro: ${profit:+.2f} | "
                    f"Placar: {session_stats['wins']}W - {session_stats['losses']}L | "
                    f"Saldo: ${account_balance:.2f} {account_currency}"
                )

                await send_discord_outcome_alert(
                    contract_id=contract_id,
                    display=display,
                    symbol=symbol,
                    stype=stype,
                    stake=stake,
                    is_win=is_win,
                    profit=profit,
                    profit_pct=profit_pct,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    session_stats=session_stats,
                    balance=account_balance,
                    currency=account_currency,
                )
                return

        except Exception as e:
            log.warning(f"Erro ao verificar contrato #{contract_id} (tentativa {attempt}): {e}")

        await asyncio.sleep(5)

    log.error(f"Tempo limite excedido ao monitorar contrato #{contract_id} após 120 tentativas!")


# ---------------------------------------------------------------------------
# Loop principal de trading — Estratégia Híbrida Otimizada + Sessão Nobre
# ---------------------------------------------------------------------------

async def trading_loop(ws: DerivWS):
    """Loop de trading híbrido otimizado (Pullback + Wick + MTF M15), operando na Sessão Nobre."""
    global open_positions, account_balance, account_currency

    log.info("")
    log.info("=" * 70)
    log.info("   AGENTE IA CLOUD — HÍBRIDO OTIMIZADO (PULLBACK + WICK + MTF M15)")
    log.info(f"   Conta: {active_account_id} ({active_account_type.upper()}) | Saldo: ${account_balance:.2f} {account_currency}")
    log.info("   Ativos: R_50 (Vol50), R_100 (Vol100)")
    log.info("   Regra: Sessão Nobre (21h-05h UTC) | Cooldown 30min | Max 1 posição")
    log.info("   Gatilho: Pullback + Wick Rejection + Confirmação MTF M15")
    log.info("   Notificações Discord: Ativas com Entrada e Relatório WIN/LOSS")
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

    # Sincronizar histórico do dia
    await sync_session_stats(ws)

    last_heartbeat: float = 0
    cycle: int = 0

    while True:
        try:
            # Validação instantânea de conectividade
            if not ws.is_connected:
                raise ConnectionError("Conexão com a Deriv foi interrompida")

            now_utc = datetime.now(timezone.utc)

            # ---- Filtro de Sessão Nobre (21h-05h UTC = 18h-02h BRT) ----
            if now_utc.hour not in NOBRE_HOURS:
                if (time.time() - last_heartbeat) >= 300:
                    log.info(
                        f"[FORA DA SESSÃO NOBRE] Hora UTC: {now_utc.hour}h. "
                        f"IA em repouso protetor. Próxima sessão: 21h UTC."
                    )
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
                    f"| Placar: {session_stats['wins']}W/{session_stats['losses']}L | Posições: {pos_str} | Ciclo #{cycle}"
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
            except Exception as e:
                log.warning(f"Erro ao consultar portfolio: {e}")
                if not ws.is_connected:
                    raise ConnectionError("WS desconectado durante get_open_contracts")
                open_positions = []

            if len(open_positions) >= MAX_OPEN_POSITIONS:
                await asyncio.sleep(4)
                cycle += 1
                continue

            # ---- Escanear ativos da cesta (Estratégia Híbrida) ----
            for sym, cfg in BASKET.items():
                if len(open_positions) >= MAX_OPEN_POSITIONS:
                    break

                now_epoch = time.time()
                if (now_epoch - last_trade_time[sym]) < COOLDOWN_SECONDS:
                    continue

                # Buscar candles M5 (200 para agrupamento M15)
                candles = await ws.get_candles(sym, HISTORY_COUNT)
                if not candles or len(candles) < 100:
                    continue

                # Não reavaliar a mesma barra
                closed_epoch = candles[-2]["epoch"]
                if last_evaluated_epoch[sym] == closed_epoch:
                    continue
                last_evaluated_epoch[sym] = closed_epoch

                # Calcular indicadores híbridos (M5 + M15 + Wick)
                ind = calculate_hybrid_indicators(candles)
                if ind is None:
                    continue

                atr = ind["atr"]
                if atr is None or atr <= 0:
                    continue

                body = ind["body"]
                stype: str | None = None

                # ---- Setup Híbrido: CALL ----
                # EMA9 > EMA21 > EMA50 (M5) + M15 trend bullish + close > EMA9
                # + wick rejection (lower_wick / body >= 0.25) + RSI 44-58
                if ind["ema_9"] > ind["ema_21"] > ind["ema_50"]:
                    if ind["m15_trend"] == 1:
                        if ind["close_prev"] > ind["ema_9"]:
                            if body > 0 and (ind["lower_wick"] / body) >= 0.25:
                                if 44 <= ind["rsi"] <= 58:
                                    stype = "CALL"

                # ---- Setup Híbrido: PUT ----
                # EMA9 < EMA21 < EMA50 (M5) + M15 trend bearish + close < EMA9
                # + wick rejection (upper_wick / body >= 0.25) + RSI 44-58
                elif ind["ema_9"] < ind["ema_21"] < ind["ema_50"]:
                    if ind["m15_trend"] == -1:
                        if ind["close_prev"] < ind["ema_9"]:
                            if body > 0 and (ind["upper_wick"] / body) >= 0.25:
                                if 44 <= ind["rsi"] <= 58:
                                    stype = "PUT"

                if stype is not None:
                    stake = cfg["lot"]
                    tp_dist = cfg["tp_atr"] * atr
                    sl_dist = cfg["sl_atr"] * atr
                    entry_price = ind["close_prev"]

                    log.info("")
                    log.info(">>> SINAL HÍBRIDO DETECTADO (Pullback + Wick + M15) <<<")
                    log.info(f"    Ativo: {cfg['display']} ({sym}) | Tipo: {stype}")
                    log.info(f"    Preço: {entry_price:.4f} | ATR: {atr:.4f}")
                    log.info(f"    TP dist: {tp_dist:.4f} | SL dist: {sl_dist:.4f}")
                    log.info(
                        f"    M5  EMA9={ind['ema_9']:.4f} EMA21={ind['ema_21']:.4f} "
                        f"EMA50={ind['ema_50']:.4f} RSI={ind['rsi']:.1f}"
                    )
                    log.info(
                        f"    M15 EMA9={ind['m15_ema9']:.4f} EMA21={ind['m15_ema21']:.4f} "
                        f"EMA50={ind['m15_ema50']:.4f} Trend={'ALTA' if ind['m15_trend'] == 1 else 'BAIXA'}"
                    )
                    log.info(
                        f"    Wick: lower={ind['lower_wick']:.4f} upper={ind['upper_wick']:.4f} "
                        f"body={body:.4f}"
                    )
                    if body > 0:
                        log.info(
                            f"    Wick Ratio: lower/body={ind['lower_wick']/body:.2f} "
                            f"upper/body={ind['upper_wick']/body:.2f}"
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
                        else:
                            buy_info = result.get("buy", {})
                            contract_id = int(buy_info.get("contract_id", 0))
                            buy_price = float(buy_info.get("buy_price", stake))
                            payout = float(buy_info.get("payout", 0.0))

                            log.info("")
                            log.info(">>> ORDEM EXECUTADA NA DERIV <<<")
                            log.info(
                                f"    Contrato: {contract_id} | Stake: ${buy_price:.2f} | Payout: ${payout:.2f}"
                            )

                            last_trade_time[sym] = time.time()

                            # 1. Enviar Alerta Completo de Entrada no Discord
                            await send_discord_entry_alert(
                                contract_id=contract_id,
                                display=cfg["display"],
                                symbol=sym,
                                stype=stype,
                                stake=buy_price,
                                payout=payout,
                                entry_price=entry_price,
                                ind=ind,
                                balance=account_balance,
                                currency=account_currency,
                            )

                            # 2. Iniciar monitoramento do resultado (WIN/LOSS) em paralelo
                            asyncio.create_task(
                                monitor_contract_outcome(
                                    contract_id=contract_id,
                                    symbol=sym,
                                    display=cfg["display"],
                                    stype=stype,
                                    stake=buy_price,
                                    payout=payout,
                                    entry_price=entry_price,
                                    duration_min=5,
                                    ws=ws,
                                )
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

        except ConnectionError as e:
            log.error(f"[CONEXÃO DERIV PERDIDA] {e}")
            raise
        except Exception as e:
            log.error(f"[ERRO LOOP] {e}")
            if not ws.is_connected:
                raise ConnectionError("WS desconectado detectado após erro")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Healthcheck HTTP server (porta para o Render e monitoramento público)
# ---------------------------------------------------------------------------
async def start_healthcheck_server():
    """Servidor HTTP na porta configurada para o Render e pings externos."""
    from aiohttp import web

    async def handle_ping(request):
        pos_str = f"{len(open_positions)} abertas" if open_positions else "0 (aguardando gatilho)"
        total = session_stats["total"]
        wins = session_stats["wins"]
        losses = session_stats["losses"]
        winrate = (wins / total * 100.0) if total > 0 else 0.0
        return web.json_response({
            "status": "online",
            "bot": "Agente IA Cloud — Híbrido Otimizado (Pullback + Wick + MTF M15)",
            "strategy": "Hybrid Nobre (M5 + M15 + Wick Rejection)",
            "account_id": active_account_id,
            "account_type": active_account_type,
            "balance": f"${account_balance:.2f} {account_currency}",
            "positions": pos_str,
            "nobre_hours_utc": NOBRE_HOURS,
            "stats": {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "winrate": f"{winrate:.1f}%",
                "net_profit": f"${session_stats['net_profit']:+.2f} {account_currency}",
            },
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

    reconnect_delay: int = 5

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
            reconnect_delay = min(reconnect_delay * 2, 60)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
