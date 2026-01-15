# HFT Bybit Futures Suite (Prototype)

A PyQt6 prototype for an adaptive HFT market-making system on Bybit futures. The UI includes
API key storage, trading toggles, position sizing, TP/SL controls, multi-symbol selection,
logging, and a backtesting dashboard.

## Features
- Auto-save API keys (stored locally in `~/.hft_bybit/config.json`).
- Connect/Disconnect and Auto Trading toggles.
- Market-making mode with imbalance skew controls.
- TP/SL controls (no timeout-based exits).
- Multi-symbol scoring table (volume/volatility/imbalance).
- Backtesting simulator and P&L dashboard placeholder.
- File + UI logging.

## Run
```bash
python src/main.py
```

## Notes
- This is a scaffold/prototype. Live Bybit API integration should be added where noted.
- Fee assumptions: taker fees 0.10% per side in the strategy estimator.
