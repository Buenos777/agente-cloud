# Agente IA Cloud — Deriv API Bot

Bot de trading 24/7 rodando na nuvem, sem MT5, sem PC ligado.

## Estratégia
Institutional Trend Pullback (EMA 9/21/50 + RSI 40-60 + ATR) — idêntica ao bot local.

## Ativos
- Volatility 100 Index (R_100)
- Volatility 25 Index (R_25)
- Volatility 10 Index (R_10)

## Deploy no Render.com (Grátis)

### 1. Subir no GitHub
```bash
cd Agente-Cloud
git init
git add .
git commit -m "cloud bot v1"
git remote add origin https://github.com/SEU_USUARIO/agente-cloud.git
git push -u origin main
```

### 2. Criar serviço no Render
1. Acesse [render.com](https://render.com) e faça login
2. Clique em **New → Background Worker**
3. Conecte seu repositório GitHub
4. Configure:
   - **Name:** `agente-cloud`
   - **Runtime:** `Docker`
   - **Instance Type:** `Free`
5. Em **Environment Variables**, adicione:
   - `DERIV_API_TOKEN` = seu token da Deriv
   - `DERIV_APP_ID` = `1089`
   - (Opcional) `DISCORD_WEBHOOK` = URL do webhook Discord
6. Clique em **Create Background Worker**

### 3. Monitorar
- Logs em tempo real no painel do Render
- Alertas via Discord/Telegram (configurar env vars)

## Variáveis de Ambiente
| Variável | Obrigatória | Descrição |
|---|---|---|
| `DERIV_API_TOKEN` | ✅ | Token API da Deriv |
| `DERIV_APP_ID` | ❌ | App ID (default: 1089) |
| `DISCORD_WEBHOOK` | ❌ | URL webhook do Discord |
| `TELEGRAM_BOT_TOKEN` | ❌ | Token do bot Telegram |
| `TELEGRAM_CHAT_ID` | ❌ | Chat ID do Telegram |
