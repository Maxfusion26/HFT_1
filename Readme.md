# HFT Bybit Futures Suite (Rust CLI)

A Rust-based CLI for interacting with Bybit's futures REST API. It supports storing API keys,
fetching tickers, reading server time, inspecting positions, checking wallet balances, and
placing orders.

## Features
- Local config storage in `~/.hft_bybit/config.json`.
- Fetch Bybit linear tickers.
- Read Bybit server time.
- Inspect open positions and wallet balances.
- Place Market/Limit orders via REST.

## Run
```bash
cargo run -- config --api-key YOUR_KEY --api-secret YOUR_SECRET
cargo run -- tickers
cargo run -- server-time
cargo run -- positions
cargo run -- wallet
cargo run -- order BTCUSDT Buy 0.001 --order-type Market
```

## Notes
- The CLI uses Bybit REST API v5 endpoints.
- Order sizes and price formatting follow the Bybit API expectations.
- Set `--base-url` in the config command if you need testnet.
