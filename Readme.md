# HFT Bybit Futures Suite (Prototype)

A PyQt5 prototype for an adaptive HFT market-making system on Bybit futures. The UI includes
API key storage, trading toggles, position sizing, TP/SL controls, multi-symbol selection,
logging, and a backtesting dashboard.

## Features
- Auto-save API keys (stored locally in `~/.hft_bybit/config.json`).
- Connect/Disconnect and Auto Trading toggles.
- Market-making mode with imbalance skew controls and fee-aware thresholds.
- TP/SL controls (no timeout-based exits).
- Multi-symbol scoring table plus auto-select top 5 symbols by 24h growth (always-on, sourced from Bybit tickers).
- Backtesting simulator and P&L dashboard placeholder with fee buffer.
- File + UI logging.
- Max concurrent positions control to cap simultaneous open trades.
- Live portfolio tab showing balance, equity, open positions, and real-time PnL.

## Run
```bash
python src/main.py
```

## Notes
- This prototype can send live orders via Bybit REST API (configure key/secret and base URL).
- Requires `requests` for REST calls.
- Order sizes are normalized to per-symbol step sizes to avoid invalid quantity errors.
- Supports Market and Limit order types (set in the Trading Controls).
- Limit orders can auto-shift price in small steps until filled (auto-shift is enabled by default).
- Position mode auto-detects and falls back to one-way if it cannot be resolved; override manually if needed.
- Fee assumptions: default UI values are 0.06% taker / 0.01% maker with an extra fee buffer.
