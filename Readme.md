# HFT Bybit Futures Suite (Prototype)

A PyQt6 prototype for an adaptive HFT market-making system on Bybit futures. The UI includes
API key storage, trading toggles, position sizing, TP/SL controls, multi-symbol selection,
logging, and a backtesting dashboard.

## Features
- Auto-save API keys (stored locally in `~/.hft_bybit/config.json`).
- Connect/Disconnect and Auto Trading toggles.
- Market-making mode with imbalance skew controls and fee-aware thresholds.
- TP/SL controls (no timeout-based exits).
- Multi-symbol scoring table plus auto-select top N symbols (always-on).
- Backtesting simulator and P&L dashboard placeholder with fee buffer.
- File + UI logging.

## Run
```bash
python src/main.py
```

## Notes
- This prototype can send live orders via Bybit REST API (configure key/secret and base URL).
- Requires `requests` for REST calls.
- Order sizes are normalized to per-symbol step sizes to avoid invalid quantity errors.
- Fee assumptions: default UI values are 0.06% taker / 0.01% maker with an extra fee buffer.
