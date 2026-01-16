import json
import logging
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
from pathlib import Path
import queue
from typing import Dict, List, Optional, Tuple

import requests
import tkinter as tk
from tkinter import ttk


CONFIG_DIR = Path.home() / ".hft_bybit"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "trading.log"
ROOT_LOG_FILE = Path(__file__).resolve().parents[1] / "log.log"
PNL_HISTORY_FILE = Path(__file__).resolve().parents[1] / "pnl_history.json"


@dataclass
class SymbolMetrics:
    symbol: str
    volume_usd: float
    volatility: float
    imbalance: float
    change_24h: float
    score: float


@dataclass
class PositionSnapshot:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float
    position_idx: Optional[int] = None


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: float
    order_type: str
    price: Optional[float] = None
    reduce_only: bool = False


@dataclass
class PositionHistoryEntry:
    timestamp: datetime
    symbol: str
    side: str
    action: str
    qty: float
    price: float
    notional_usdt: float
    pnl_usdt: Optional[float] = None
    reason: str = ""


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def update(self, updates: dict) -> None:
        current = self.load()
        current.update(updates)
        self.save(current)


class BybitRestClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")

    def _sign(self, timestamp: str, recv_window: str, payload: str) -> str:
        message = f"{timestamp}{self.api_key}{recv_window}{payload}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, timestamp: str, recv_window: str, payload: str) -> dict:
        signature = self._sign(timestamp, recv_window, payload)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }

    def create_order(self, request: OrderRequest) -> dict:
        endpoint = "/v5/order/create"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = {
            "symbol": request.symbol,
            "side": request.side,
            "orderType": request.order_type,
            "qty": f"{request.qty:.6f}",
            "category": "linear",
            "timeInForce": "GTC",
            "orderLinkId": str(uuid.uuid4()),
        }
        if request.reduce_only:
            payload["reduceOnly"] = True
        if request.order_type == "Limit":
            if request.price is None:
                raise ValueError("Limit orders require a price.")
            payload["price"] = f"{request.price:.2f}"
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._headers(timestamp, recv_window, payload_str)
        response = requests.post(
            f"{self.base_url}{endpoint}",
            data=payload_str,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        payload_str = ""
        headers = self._headers(timestamp, recv_window, payload_str)
        response = requests.get(
            f"{self.base_url}{endpoint}",
            params=params,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def fetch_linear_tickers(self) -> list:
        data = self._get("/v5/market/tickers", {"category": "linear"})
        return data.get("result", {}).get("list", [])

    def fetch_positions(self) -> list:
        data = self._get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        return data.get("result", {}).get("list", [])

    def fetch_wallet_balance(self) -> dict:
        data = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        return data.get("result", {})

    def fetch_position_history(self) -> list:
        data = self._get("/v5/position/closed-pnl", {"category": "linear"})
        return data.get("result", {}).get("list", [])


class HFTStrategy:
    def __init__(self, maker_fee: float = 0.0001, taker_fee: float = 0.0006) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

    def compute_quotes(
        self,
        best_bid: float,
        best_ask: float,
        bid_size: float,
        ask_size: float,
        risk_skew: float,
        spread_multiplier: float,
    ) -> Tuple[float, float]:
        mid = (best_bid + best_ask) / 2
        imbalance = (bid_size - ask_size) / max(bid_size + ask_size, 1e-9)
        micro_price = mid + (imbalance * (best_ask - best_bid) * 0.5)
        skew = micro_price * risk_skew * imbalance
        half_spread = ((best_ask - best_bid) * spread_multiplier) / 2
        return micro_price - half_spread - skew, micro_price + half_spread - skew

    def estimate_roundtrip_fee(self, notional: float, use_maker: bool) -> float:
        fee = self.maker_fee if use_maker else self.taker_fee
        return notional * (fee + fee)


class BacktestEngine:
    def __init__(self, strategy: HFTStrategy) -> None:
        self.strategy = strategy

    def run_dummy(self, steps: int, fee_buffer: float) -> Tuple[List[int], List[float]]:
        timestamps = []
        pnl_series = []
        pnl = 0.0
        for idx in range(steps):
            spread = 0.5 + random.random()
            mid = 30000 + random.randint(-100, 100)
            bid = mid - spread / 2
            ask = mid + spread / 2
            bid_size = random.uniform(10, 80)
            ask_size = random.uniform(10, 80)
            quote_bid, quote_ask = self.strategy.compute_quotes(
                bid,
                ask,
                bid_size,
                ask_size,
                risk_skew=0.15,
                spread_multiplier=1.2,
            )
            pnl += (quote_ask - quote_bid) * 0.1
            pnl -= self.strategy.estimate_roundtrip_fee(1000, use_maker=False)
            pnl -= 1000 * fee_buffer
            timestamps.append(idx)
            pnl_series.append(pnl)
        return timestamps, pnl_series


class TkQueueHandler(logging.Handler):
    def __init__(self, ui_queue: queue.Queue) -> None:
        super().__init__()
        self.ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.ui_queue.put(("log", msg, None))


class TradingApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("HFT Bybit - Modern UI")
        self.geometry("980x680")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config_manager = ConfigManager(CONFIG_FILE)
        self.strategy = HFTStrategy()
        self.backtest_engine = BacktestEngine(self.strategy)
        self.client: Optional[BybitRestClient] = None

        self.ui_queue: queue.Queue = queue.Queue()
        self.worker_threads: List[threading.Thread] = []
        self.shutdown_event = threading.Event()

        self._setup_style()
        self._setup_ui()
        self._setup_logging()
        self._load_config()

        self.after(100, self._drain_queue)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

    def _setup_ui(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.trading_tab = ttk.Frame(self.notebook)
        self.backtest_tab = ttk.Frame(self.notebook)
        self.dashboard_tab = ttk.Frame(self.notebook)
        self.history_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.trading_tab, text="Trading")
        self.notebook.add(self.backtest_tab, text="Backtesting")
        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.history_tab, text="History")

        self._build_trading_tab()
        self._build_backtest_tab()
        self._build_dashboard_tab()
        self._build_history_tab()

    def _build_trading_tab(self) -> None:
        container = ttk.Frame(self.trading_tab)
        container.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(container)
        right = ttk.Frame(container)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(6, 0))

        connection = ttk.LabelFrame(left, text="Connection")
        connection.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(connection, text="API Key:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(connection, text="API Secret:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(connection, text="Base URL:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=4)

        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.base_url_var = tk.StringVar(value="https://api.bybit.com")

        ttk.Entry(connection, textvariable=self.api_key_var, width=40).grid(
            row=0, column=1, sticky=tk.EW, padx=6, pady=4
        )
        ttk.Entry(connection, textvariable=self.api_secret_var, width=40, show="*").grid(
            row=1, column=1, sticky=tk.EW, padx=6, pady=4
        )
        ttk.Entry(connection, textvariable=self.base_url_var, width=40).grid(
            row=2, column=1, sticky=tk.EW, padx=6, pady=4
        )

        self.status_var = tk.StringVar(value="Disconnected")
        self.status_label = ttk.Label(connection, textvariable=self.status_var, style="Header.TLabel")
        self.status_label.grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=6, pady=6)

        self.connect_button = ttk.Button(
            connection,
            text="Connect",
            style="Primary.TButton",
            command=self._connect,
        )
        self.connect_button.grid(row=4, column=0, columnspan=2, sticky=tk.EW, padx=6, pady=6)

        connection.columnconfigure(1, weight=1)

        order_frame = ttk.LabelFrame(left, text="Order Entry")
        order_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(order_frame, text="Symbol:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(order_frame, text="Side:").grid(row=1, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(order_frame, text="Quantity:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(order_frame, text="Type:").grid(row=3, column=0, sticky=tk.W, padx=6, pady=4)
        ttk.Label(order_frame, text="Limit Price:").grid(row=4, column=0, sticky=tk.W, padx=6, pady=4)

        self.symbol_var = tk.StringVar(value="BTCUSDT")
        self.side_var = tk.StringVar(value="Buy")
        self.qty_var = tk.DoubleVar(value=0.001)
        self.order_type_var = tk.StringVar(value="Market")
        self.limit_price_var = tk.DoubleVar(value=0.0)

        ttk.Entry(order_frame, textvariable=self.symbol_var).grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        ttk.Combobox(order_frame, textvariable=self.side_var, values=["Buy", "Sell"], state="readonly").grid(
            row=1, column=1, sticky=tk.EW, padx=6, pady=4
        )
        ttk.Entry(order_frame, textvariable=self.qty_var).grid(row=2, column=1, sticky=tk.EW, padx=6, pady=4)
        ttk.Combobox(order_frame, textvariable=self.order_type_var, values=["Market", "Limit"], state="readonly").grid(
            row=3, column=1, sticky=tk.EW, padx=6, pady=4
        )
        ttk.Entry(order_frame, textvariable=self.limit_price_var).grid(
            row=4, column=1, sticky=tk.EW, padx=6, pady=4
        )

        ttk.Button(order_frame, text="Send Order", command=self._send_order).grid(
            row=5, column=0, columnspan=2, sticky=tk.EW, padx=6, pady=6
        )
        order_frame.columnconfigure(1, weight=1)

        market_frame = ttk.LabelFrame(right, text="Market Data")
        market_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.ticker_list = tk.Listbox(market_frame, height=12)
        self.ticker_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        ttk.Button(market_frame, text="Refresh Tickers", command=self._refresh_tickers).pack(
            fill=tk.X, padx=6, pady=(0, 6)
        )

        positions_frame = ttk.LabelFrame(right, text="Open Positions")
        positions_frame.pack(fill=tk.BOTH, expand=True)

        self.positions_list = tk.Listbox(positions_frame, height=8)
        self.positions_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        ttk.Button(positions_frame, text="Refresh Positions", command=self._refresh_positions).pack(
            fill=tk.X, padx=6, pady=(0, 6)
        )

        log_frame = ttk.LabelFrame(self.trading_tab, text="Activity Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text.configure(state=tk.DISABLED)

    def _build_backtest_tab(self) -> None:
        frame = ttk.Frame(self.backtest_tab)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(frame, text="Dummy Backtest", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 6))

        control = ttk.Frame(frame)
        control.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(control, text="Steps:").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        ttk.Label(control, text="Fee buffer:").grid(row=1, column=0, padx=6, pady=4, sticky=tk.W)

        self.backtest_steps_var = tk.IntVar(value=120)
        self.backtest_fee_var = tk.DoubleVar(value=0.0002)

        ttk.Entry(control, textvariable=self.backtest_steps_var, width=12).grid(row=0, column=1, padx=6, pady=4)
        ttk.Entry(control, textvariable=self.backtest_fee_var, width=12).grid(row=1, column=1, padx=6, pady=4)

        ttk.Button(control, text="Run", command=self._run_backtest).grid(
            row=0, column=2, rowspan=2, padx=6, pady=4, sticky=tk.EW
        )
        control.columnconfigure(2, weight=1)

        self.backtest_output = tk.Text(frame, height=18, wrap=tk.WORD)
        self.backtest_output.pack(fill=tk.BOTH, expand=True)
        self.backtest_output.configure(state=tk.DISABLED)

    def _build_dashboard_tab(self) -> None:
        frame = ttk.Frame(self.dashboard_tab)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(frame, text="Portfolio Snapshot", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 6))
        self.balance_label = ttk.Label(frame, text="Balance: --")
        self.balance_label.pack(anchor=tk.W)

        ttk.Button(frame, text="Refresh Balance", command=self._refresh_balance).pack(
            anchor=tk.W, pady=(8, 0)
        )

    def _build_history_tab(self) -> None:
        frame = ttk.Frame(self.history_tab)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(frame, text="Closed PnL History", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 6))
        self.history_list = tk.Listbox(frame, height=16)
        self.history_list.pack(fill=tk.BOTH, expand=True)

        ttk.Button(frame, text="Refresh History", command=self._refresh_history).pack(
            fill=tk.X, pady=(6, 0)
        )

    def _setup_logging(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=[
                logging.FileHandler(LOG_FILE, encoding="utf-8"),
                logging.FileHandler(ROOT_LOG_FILE, encoding="utf-8"),
                TkQueueHandler(self.ui_queue),
            ],
        )

    def _load_config(self) -> None:
        data = self.config_manager.load()
        self.api_key_var.set(data.get("api_key", ""))
        self.api_secret_var.set(data.get("api_secret", ""))
        self.base_url_var.set(data.get("base_url", "https://api.bybit.com"))

    def _save_config(self) -> None:
        self.config_manager.update(
            {
                "api_key": self.api_key_var.get().strip(),
                "api_secret": self.api_secret_var.get().strip(),
                "base_url": self.base_url_var.get().strip(),
            }
        )

    def _connect(self) -> None:
        api_key = self.api_key_var.get().strip()
        api_secret = self.api_secret_var.get().strip()
        base_url = self.base_url_var.get().strip() or "https://api.bybit.com"
        if not api_key or not api_secret:
            self._log("API key and secret are required.")
            return
        self.client = BybitRestClient(api_key, api_secret, base_url)
        self.status_var.set("Connected")
        self._save_config()
        self._log("Connected to Bybit API.")

    def _send_order(self) -> None:
        if not self.client:
            self._log("Connect first before sending orders.")
            return
        try:
            request = OrderRequest(
                symbol=self.symbol_var.get().strip().upper(),
                side=self.side_var.get(),
                qty=float(self.qty_var.get()),
                order_type=self.order_type_var.get(),
                price=float(self.limit_price_var.get()) if self.order_type_var.get() == "Limit" else None,
            )
        except ValueError as exc:
            self._log(f"Invalid order input: {exc}")
            return
        self._run_worker("order", self.client.create_order, request)

    def _refresh_tickers(self) -> None:
        if not self.client:
            self._log("Connect first before fetching tickers.")
            return
        self._run_worker("tickers", self.client.fetch_linear_tickers)

    def _refresh_positions(self) -> None:
        if not self.client:
            self._log("Connect first before fetching positions.")
            return
        self._run_worker("positions", self.client.fetch_positions)

    def _refresh_balance(self) -> None:
        if not self.client:
            self._log("Connect first before fetching balance.")
            return
        self._run_worker("balance", self.client.fetch_wallet_balance)

    def _refresh_history(self) -> None:
        if not self.client:
            self._log("Connect first before fetching history.")
            return
        self._run_worker("history", self.client.fetch_position_history)

    def _run_backtest(self) -> None:
        steps = max(self.backtest_steps_var.get(), 1)
        fee_buffer = max(self.backtest_fee_var.get(), 0.0)
        timestamps, pnl_series = self.backtest_engine.run_dummy(steps, fee_buffer)
        self.backtest_output.configure(state=tk.NORMAL)
        self.backtest_output.delete("1.0", tk.END)
        self.backtest_output.insert(
            tk.END,
            f"Generated {len(pnl_series)} points. Last PnL: {pnl_series[-1]:,.2f} USDT\n",
        )
        for idx, pnl in zip(timestamps[-10:], pnl_series[-10:]):
            self.backtest_output.insert(tk.END, f"Step {idx}: {pnl:,.2f} USDT\n")
        self.backtest_output.configure(state=tk.DISABLED)

    def _run_worker(self, kind: str, func, *args) -> None:
        def worker() -> None:
            try:
                result = func(*args)
                self.ui_queue.put((kind, result, None))
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put((kind, None, exc))

        thread = threading.Thread(target=worker, daemon=True)
        self.worker_threads.append(thread)
        thread.start()

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload, error = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(payload)
            elif error is not None:
                self._log(f"{kind.capitalize()} failed: {error}")
            elif kind == "order":
                self._log(f"Order response: {payload}")
            elif kind == "tickers":
                self._update_tickers(payload)
            elif kind == "positions":
                self._update_positions(payload)
            elif kind == "balance":
                self._update_balance(payload)
            elif kind == "history":
                self._update_history(payload)
        if not self.shutdown_event.is_set():
            self.after(100, self._drain_queue)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _log(self, message: str) -> None:
        logging.info(message)

    def _update_tickers(self, tickers: list) -> None:
        self.ticker_list.delete(0, tk.END)
        for ticker in tickers[:60]:
            symbol = ticker.get("symbol", "")
            last_price = float(ticker.get("lastPrice", 0) or 0)
            change = float(ticker.get("price24hPcnt", 0) or 0) * 100
            self.ticker_list.insert(tk.END, f"{symbol:<10} {last_price:>10,.2f} ({change:+.2f}%)")
        self._log(f"Loaded {len(tickers)} tickers.")

    def _update_positions(self, positions: list) -> None:
        self.positions_list.delete(0, tk.END)
        for pos in positions:
            size = float(pos.get("size", 0) or 0)
            if size == 0:
                continue
            symbol = pos.get("symbol", "")
            side = pos.get("side", "")
            entry = float(pos.get("avgPrice", 0) or 0)
            pnl = float(pos.get("unrealisedPnl", 0) or 0)
            self.positions_list.insert(
                tk.END,
                f"{symbol} {side} {size} @ {entry:,.2f} | PnL {pnl:,.2f}",
            )
        self._log("Positions updated.")

    def _update_balance(self, balance: dict) -> None:
        total = balance.get("totalEquity") or balance.get("totalWalletBalance")
        self.balance_label.configure(text=f"Balance: {total or '--'}")
        self._log("Balance updated.")

    def _update_history(self, history: list) -> None:
        self.history_list.delete(0, tk.END)
        for item in history[:80]:
            symbol = item.get("symbol", "")
            closed_pnl = float(item.get("closedPnl", 0) or 0)
            qty = item.get("qty", "")
            self.history_list.insert(tk.END, f"{symbol:<10} qty {qty} pnl {closed_pnl:,.2f}")
        self._log("History updated.")

    def _on_close(self) -> None:
        self.shutdown_event.set()
        for thread in list(self.worker_threads):
            thread.join(timeout=1.0)
        self.destroy()


def main() -> None:
    sys.excepthook = _log_uncaught_exception
    app = TradingApp()
    app.mainloop()


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: Optional[object],
) -> None:
    logging.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


if __name__ == "__main__":
    main()
