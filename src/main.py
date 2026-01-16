import json
import logging
import math
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets
import requests


CONFIG_DIR = Path.home() / ".hft_bybit"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "trading.log"


@dataclass
class SymbolMetrics:
    symbol: str
    volume_usd: float
    volatility: float
    imbalance: float
    change_24h: float

    @property
    def score(self) -> float:
        return (self.volume_usd * 0.6) + (self.volatility * 0.3) + (self.imbalance * 0.1)


@dataclass
class MarketSnapshot:
    symbol: str
    mid: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    volatility: float
    imbalance: float


@dataclass
class PositionState:
    symbol: str
    qty: float = 0.0
    entry_price: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    last_update: datetime = datetime.utcnow()
    position_idx: Optional[int] = None


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: float
    position_idx: int
    order_type: str
    limit_price: Optional[float]
    reduce_only: bool = False
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    set_trading_stop: bool = False
    retry: int = 0
    remaining_qty: Optional[float] = None


@dataclass
class PositionSnapshot:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float
    position_idx: Optional[int] = None


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


@dataclass
class TradingStopRequest:
    symbol: str
    position_idx: int
    take_profit: Optional[float]
    stop_loss: Optional[float]
    source: str = "entry"


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
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    def create_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        position_idx: int = 0,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> dict:
        endpoint = "/v5/order/create"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": f"{qty:.6f}",
            "category": "linear",
            "timeInForce": "GTC",
            "orderLinkId": str(uuid.uuid4()),
            "positionIdx": position_idx,
        }
        if reduce_only:
            payload["reduceOnly"] = True
        if order_type == "Limit":
            if price is None:
                raise ValueError("Limit orders require a price.")
            payload["price"] = f"{price:.2f}"
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        signature = self._sign(timestamp, recv_window, payload_str)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }
        response = requests.post(
            f"{self.base_url}{endpoint}", data=payload_str, headers=headers, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def fetch_linear_tickers(self) -> List[dict]:
        endpoint = "/v5/market/tickers"
        params = {"category": "linear"}
        response = requests.get(f"{self.base_url}{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return []
        return payload.get("result", {}).get("list", [])

    def fetch_server_time(self) -> Optional[float]:
        endpoint = "/v5/market/time"
        response = requests.get(f"{self.base_url}{endpoint}", timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return None
        result = payload.get("result", {})
        if "timeSecond" in result:
            try:
                return float(result["timeSecond"]) * 1000
            except (TypeError, ValueError):
                return None
        if "timeNano" in result:
            try:
                return float(result["timeNano"]) / 1_000_000
            except (TypeError, ValueError):
                return None
        return None

    def fetch_position_mode(self) -> Optional[str]:
        endpoint = "/v5/position/list"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        params = {"category": "linear"}
        query = "&".join(f"{key}={params[key]}" for key in sorted(params))
        signature = self._sign(timestamp, recv_window, query)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        response = requests.get(
            f"{self.base_url}{endpoint}", params=params, headers=headers, timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return None
        positions = payload.get("result", {}).get("list", [])
        for position in positions:
            if position.get("positionIdx") in (1, 2):
                return "hedge"
        return "one-way"

    def fetch_instruments_specs(self) -> dict:
        endpoint = "/v5/market/instruments-info"
        params = {"category": "linear"}
        response = requests.get(f"{self.base_url}{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return {}
        specs = {}
        for item in payload.get("result", {}).get("list", []):
            symbol = item.get("symbol")
            lot_filter = item.get("lotSizeFilter", {})
            min_qty = float(lot_filter.get("minOrderQty", 0) or 0)
            step = float(lot_filter.get("qtyStep", 0) or 0)
            min_notional = float(lot_filter.get("minNotional", 0) or 0)
            if symbol and min_qty > 0 and step > 0:
                specs[symbol] = {"min_qty": min_qty, "step": step, "min_notional": min_notional}
        return specs

    def fetch_positions(self) -> list:
        endpoint = "/v5/position/list"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        base_params = {"category": "linear"}

        def _request(params: dict) -> list:
            query = "&".join(f"{key}={params[key]}" for key in sorted(params))
            signature = self._sign(timestamp, recv_window, query)
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
            }
            response = requests.get(
                f"{self.base_url}{endpoint}", params=params, headers=headers, timeout=10
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                return []
            return payload.get("result", {}).get("list", [])

        positions = _request(base_params)
        if positions:
            return positions
        for settle_coin in ("USDT", "USDC"):
            positions = _request({**base_params, "settleCoin": settle_coin})
            if positions:
                return positions
        return []

    def fetch_position_history(self, limit: int = 50) -> list:
        endpoint = "/v5/position/closed-pnl"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        params = {"category": "linear", "limit": limit}
        query = "&".join(f"{key}={params[key]}" for key in sorted(params))
        signature = self._sign(timestamp, recv_window, query)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        response = requests.get(
            f"{self.base_url}{endpoint}", params=params, headers=headers, timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return []
        return payload.get("result", {}).get("list", [])

    def fetch_wallet_balance(self) -> dict:
        endpoint = "/v5/account/wallet-balance"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        params = {"accountType": "UNIFIED"}
        query = "&".join(f"{key}={params[key]}" for key in sorted(params))
        signature = self._sign(timestamp, recv_window, query)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        response = requests.get(
            f"{self.base_url}{endpoint}", params=params, headers=headers, timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            return {}
        return payload.get("result", {})

    def set_trading_stop(
        self,
        symbol: str,
        position_idx: int,
        take_profit: Optional[float],
        stop_loss: Optional[float],
    ) -> dict:
        if take_profit is None and stop_loss is None:
            raise ValueError("At least one of take_profit or stop_loss must be set.")
        endpoint = "/v5/position/trading-stop"
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": position_idx,
            "tpslMode": "Full",
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "tpOrderType": "Market",
            "slOrderType": "Market",
        }
        if take_profit is not None:
            payload["takeProfit"] = f"{take_profit:.6f}"
        if stop_loss is not None:
            payload["stopLoss"] = f"{stop_loss:.6f}"
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        signature = self._sign(timestamp, recv_window, payload_str)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }
        response = requests.post(
            f"{self.base_url}{endpoint}", data=payload_str, headers=headers, timeout=10
        )
        response.raise_for_status()
        return response.json()

class QtLogHandler(logging.Handler):
    def __init__(self, widget: QtWidgets.QTextEdit) -> None:
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        QtCore.QMetaObject.invokeMethod(
            self.widget,
            "append",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, msg),
        )


class OrderThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(object, object, object)

    def __init__(self, client: BybitRestClient, request: OrderRequest) -> None:
        super().__init__()
        self.client = client
        self.request = request

    def run(self) -> None:
        try:
            response = self.client.create_order(
                symbol=self.request.symbol,
                side=self.request.side,
                qty=self.request.qty,
                position_idx=self.request.position_idx,
                order_type=self.request.order_type,
                price=self.request.limit_price,
                reduce_only=self.request.reduce_only,
            )
            self.finished.emit(self.request, response, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(self.request, None, exc)


class TickerThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(list, dict, dict, object)

    def __init__(self, client: BybitRestClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            tickers = self.client.fetch_linear_tickers()
            change_map = {}
            symbols = []
            last_price_map = {}
            for ticker in tickers:
                symbol = ticker.get("symbol")
                last_price = float(ticker.get("lastPrice", 0) or 0)
                prev_price = float(ticker.get("prevPrice24h", 0) or 0)
                if symbol:
                    symbols.append(symbol)
                    if last_price > 0:
                        last_price_map[symbol] = last_price
                    if prev_price > 0:
                        change_map[symbol] = ((last_price - prev_price) / prev_price) * 100
            self.finished.emit(symbols, change_map, last_price_map, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit([], {}, {}, exc)


class InstrumentThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(dict, object)

    def __init__(self, client: BybitRestClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            specs = self.client.fetch_instruments_specs()
            self.finished.emit(specs, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit({}, exc)


class PortfolioThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(list, dict, object)

    def __init__(self, client: BybitRestClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            positions = self.client.fetch_positions()
            balance = self.client.fetch_wallet_balance()
            self.finished.emit(positions, balance, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit([], {}, exc)


class HistoryThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(list, object)

    def __init__(self, client: BybitRestClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            history = self.client.fetch_position_history()
            self.finished.emit(history, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit([], exc)


class TradingStopThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(object, object, object)

    def __init__(self, client: BybitRestClient, request: TradingStopRequest) -> None:
        super().__init__()
        self.client = client
        self.request = request

    def run(self) -> None:
        try:
            response = self.client.set_trading_stop(
                symbol=self.request.symbol,
                position_idx=self.request.position_idx,
                take_profit=self.request.take_profit,
                stop_loss=self.request.stop_loss,
            )
            self.finished.emit(self.request, response, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(self.request, None, exc)
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
    ) -> tuple[float, float]:
        mid = (best_bid + best_ask) / 2
        imbalance = (bid_size - ask_size) / max(bid_size + ask_size, 1e-9)
        micro_price = mid + (imbalance * (best_ask - best_bid) * 0.5)
        skew = micro_price * risk_skew * imbalance
        half_spread = ((best_ask - best_bid) * spread_multiplier) / 2
        return micro_price - half_spread - skew, micro_price + half_spread - skew

    def estimate_roundtrip_fee(self, notional: float, use_maker: bool) -> float:
        fee = self.maker_fee if use_maker else self.taker_fee
        return notional * (fee + fee)

    def required_edge(self, use_maker: bool, fee_buffer: float) -> float:
        fee = self.maker_fee if use_maker else self.taker_fee
        return (fee + fee) + fee_buffer


class BacktestEngine(QtCore.QObject):
    finished = QtCore.pyqtSignal(list, list)

    def __init__(self, strategy: HFTStrategy, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.strategy = strategy

    def run_dummy(self, steps: int, fee_buffer: float) -> None:
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
        self.finished.emit(timestamps, pnl_series)


class TradingApp(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HFT Bybit Futures - Adaptive Market Maker")
        self.resize(920, 600)
        self.config = ConfigManager(CONFIG_FILE)
        self.strategy = HFTStrategy()
        self.backtest_engine = BacktestEngine(self.strategy)
        self.client: Optional[BybitRestClient] = None
        self.connected = False
        self.position_mode_detected: Optional[str] = None
        self.limit_shift_attempts: Dict[tuple, int] = {}
        self.order_threads: List[OrderThread] = []
        self.trading_stop_threads: List[TradingStopThread] = []
        self.ticker_thread: Optional[TickerThread] = None
        self.instrument_thread: Optional[InstrumentThread] = None
        self.instrument_specs_ready = False
        self.portfolio_thread: Optional[PortfolioThread] = None
        self.history_thread: Optional[HistoryThread] = None
        self.ticker_symbols: List[str] = []
        self.ticker_change_map: Dict[str, float] = {}
        self.ticker_last_price_map: Dict[str, float] = {}
        self.selected_symbols: List[str] = []
        self._updating_symbol_list = False
        self.open_positions: List[PositionSnapshot] = []
        self.portfolio_ready = False
        self.trading_stop_cache: Dict[tuple, tuple[float, float]] = {}
        self.position_history: List[PositionHistoryEntry] = []
        self.history_keys: set[str] = set()
        self.portfolio_timer = QtCore.QTimer(self)
        self.portfolio_timer.setInterval(2000)
        self.history_timer = QtCore.QTimer(self)
        self.history_timer.setInterval(10_000)
        self.time_status_timer = QtCore.QTimer(self)
        self.time_status_timer.setInterval(2000)
        self.symbol_specs = {
            "BTCUSDT": {"min_qty": 0.001, "step": 0.001, "min_notional": 5.0},
            "ETHUSDT": {"min_qty": 0.01, "step": 0.01, "min_notional": 5.0},
            "BNBUSDT": {"min_qty": 0.1, "step": 0.1, "min_notional": 5.0},
            "SOLUSDT": {"min_qty": 0.1, "step": 0.1, "min_notional": 5.0},
            "XRPUSDT": {"min_qty": 1.0, "step": 1.0, "min_notional": 5.0},
        }
        self.positions: Dict[str, PositionState] = {}
        self.symbol_metrics: List[SymbolMetrics] = []
        self.trading_timer = QtCore.QTimer(self)
        self.trading_timer.setInterval(1500)
        self._setup_ui()
        self._apply_style()
        self._load_config()
        self._setup_logging()
        self._wire_signals()
        self._refresh_symbol_table()

    def _setup_ui(self) -> None:
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)

        self.trading_tab = QtWidgets.QWidget()
        self.backtest_tab = QtWidgets.QWidget()
        self.dashboard_tab = QtWidgets.QWidget()
        self.history_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.trading_tab, "Trading")
        self.tabs.addTab(self.backtest_tab, "Backtesting")
        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.history_tab, "History")

        self._setup_trading_tab()
        self._setup_backtest_tab()
        self._setup_dashboard_tab()
        self._setup_history_tab()

    def _setup_trading_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.trading_tab)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        header_widget = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        self.time_status_label = QtWidgets.QLabel("Moscow: -- | Bybit: -- | Ping: -- ms")
        self.time_status_label.setProperty("role", "time_status")
        time_font = QtGui.QFont()
        time_font.setBold(True)
        self.time_status_label.setFont(time_font)
        self.time_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.time_status_label.setMinimumHeight(18)

        self.connection_status_label = QtWidgets.QLabel("Disconnected")
        self.connection_status_label.setProperty("status", "idle")
        self.trading_status_label = QtWidgets.QLabel("Auto-trading Off")
        self.trading_status_label.setProperty("status", "idle")
        self.connection_status_label.setProperty("role", "status")
        self.trading_status_label.setProperty("role", "status")
        self.connection_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.trading_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

        header.addWidget(self.time_status_label)
        header.addWidget(self.connection_status_label, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        header.addWidget(self.trading_status_label, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(4)
        self.main_splitter.setChildrenCollapsible(False)

        left_panel = QtWidgets.QWidget()
        left_panel.setProperty("panel", "true")
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        right_panel = QtWidgets.QWidget()
        right_panel.setProperty("panel", "true")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        creds_group = QtWidgets.QGroupBox("Credentials")
        creds_group.setProperty("card", "true")
        creds_layout = QtWidgets.QGridLayout(creds_group)
        creds_layout.setHorizontalSpacing(10)
        creds_layout.setVerticalSpacing(8)

        self.api_key_input = QtWidgets.QLineEdit()
        self.api_secret_input = QtWidgets.QLineEdit()
        self.api_secret_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.api_base_url_input = QtWidgets.QLineEdit("https://api.bybit.com")
        self.auto_save_checkbox = QtWidgets.QCheckBox("Auto-save")

        creds_layout.addWidget(QtWidgets.QLabel("API Key"), 0, 0)
        creds_layout.addWidget(self.api_key_input, 0, 1)
        creds_layout.addWidget(QtWidgets.QLabel("API Secret"), 1, 0)
        creds_layout.addWidget(self.api_secret_input, 1, 1)
        creds_layout.addWidget(QtWidgets.QLabel("Base URL"), 2, 0)
        creds_layout.addWidget(self.api_base_url_input, 2, 1)
        creds_layout.addWidget(self.auto_save_checkbox, 3, 0, 1, 2)

        controls_group = QtWidgets.QGroupBox("Trading Controls")
        controls_group.setProperty("card", "true")
        controls_layout = QtWidgets.QGridLayout(controls_group)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(4)
        controls_layout.setColumnStretch(1, 1)
        controls_layout.setColumnStretch(3, 1)
        controls_layout.setColumnStretch(5, 1)
        controls_layout.setColumnStretch(6, 1)

        self.connect_button = QtWidgets.QPushButton("Connect")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.auto_trading_toggle = QtWidgets.QPushButton("Start Auto Trading")
        self.auto_trading_toggle.setCheckable(True)

        self.symbol_input = QtWidgets.QComboBox()
        self.symbol_input.addItems(["BTCUSDT", "ETHUSDT", "SOLUSDT"]) 
        self.position_size_input = QtWidgets.QDoubleSpinBox()
        self.position_size_input.setMaximum(1_000_000)
        self.position_size_input.setValue(500)
        self.position_size_input.setSuffix(" $")

        self.max_positions_input = QtWidgets.QSpinBox()
        self.max_positions_input.setRange(1, 50)
        self.max_positions_input.setValue(5)

        self.order_type_input = QtWidgets.QComboBox()
        self.order_type_input.addItems(["Market", "Limit"])
        self.limit_price_input = QtWidgets.QDoubleSpinBox()
        self.limit_price_input.setRange(0.0, 1_000_000)
        self.limit_price_input.setDecimals(2)
        self.limit_price_input.setValue(0.0)
        self.limit_price_input.setSuffix(" $")
        self.limit_price_input.setEnabled(False)
        self.auto_shift_checkbox = QtWidgets.QCheckBox("Auto-shift limit")
        self.auto_shift_checkbox.setChecked(True)
        self.shift_bps_input = QtWidgets.QDoubleSpinBox()
        self.shift_bps_input.setRange(0.01, 1.0)
        self.shift_bps_input.setDecimals(2)
        self.shift_bps_input.setValue(0.05)
        self.shift_bps_input.setSuffix(" %")

        self.top_n_input = QtWidgets.QSpinBox()
        self.top_n_input.setRange(1, 20)
        self.top_n_input.setValue(5)

        self.auto_select_checkbox = QtWidgets.QCheckBox("Auto-select top symbols")
        self.auto_select_checkbox.setChecked(True)
        self.auto_select_interval = QtWidgets.QSpinBox()
        self.auto_select_interval.setRange(5, 600)
        self.auto_select_interval.setValue(60)
        self.auto_select_interval.setSuffix(" s")

        self.tp_input = QtWidgets.QDoubleSpinBox()
        self.tp_input.setRange(0.1, 10.0)
        self.tp_input.setValue(0.8)
        self.tp_input.setSuffix(" %")

        self.sl_input = QtWidgets.QDoubleSpinBox()
        self.sl_input.setRange(0.1, 10.0)
        self.sl_input.setValue(0.4)
        self.sl_input.setSuffix(" %")

        self.trailing_tp_sl_checkbox = QtWidgets.QCheckBox("Auto-trail TP/SL")
        self.trailing_tp_sl_checkbox.setChecked(False)
        self.trailing_trigger_input = QtWidgets.QDoubleSpinBox()
        self.trailing_trigger_input.setRange(0.0, 5.0)
        self.trailing_trigger_input.setValue(0.3)
        self.trailing_trigger_input.setSuffix(" %")

        self.maker_mode_checkbox = QtWidgets.QCheckBox("Market making mode")
        self.risk_skew_input = QtWidgets.QDoubleSpinBox()
        self.risk_skew_input.setRange(0.0, 1.0)
        self.risk_skew_input.setSingleStep(0.05)
        self.risk_skew_input.setValue(0.15)

        self.position_mode_input = QtWidgets.QComboBox()
        self.position_mode_input.addItems(
            ["Auto-detect", "One-way (posIdx 0)", "Hedge (posIdx 1/2)"]
        )

        self.spread_multiplier_input = QtWidgets.QDoubleSpinBox()
        self.spread_multiplier_input.setRange(1.0, 5.0)
        self.spread_multiplier_input.setValue(1.2)

        self.maker_fee_input = QtWidgets.QDoubleSpinBox()
        self.maker_fee_input.setRange(0.0, 0.5)
        self.maker_fee_input.setDecimals(3)
        self.maker_fee_input.setValue(0.01)
        self.maker_fee_input.setSuffix(" %")

        self.taker_fee_input = QtWidgets.QDoubleSpinBox()
        self.taker_fee_input.setRange(0.0, 0.5)
        self.taker_fee_input.setDecimals(3)
        self.taker_fee_input.setValue(0.06)
        self.taker_fee_input.setSuffix(" %")

        self.fee_buffer_input = QtWidgets.QDoubleSpinBox()
        self.fee_buffer_input.setRange(0.0, 0.5)
        self.fee_buffer_input.setDecimals(3)
        self.fee_buffer_input.setValue(0.1)
        self.fee_buffer_input.setSuffix(" % buffer")

        controls_layout.addWidget(self.connect_button, 0, 0)
        controls_layout.addWidget(self.disconnect_button, 0, 1)
        controls_layout.addWidget(self.auto_trading_toggle, 0, 2)
        controls_layout.addWidget(QtWidgets.QLabel("Symbol"), 1, 0)
        controls_layout.addWidget(self.symbol_input, 1, 1)
        controls_layout.addWidget(QtWidgets.QLabel("Position size"), 1, 2)
        controls_layout.addWidget(self.position_size_input, 1, 3)
        controls_layout.addWidget(QtWidgets.QLabel("Max positions"), 1, 4)
        controls_layout.addWidget(self.max_positions_input, 1, 5)
        controls_layout.addWidget(QtWidgets.QLabel("Order type"), 2, 0)
        controls_layout.addWidget(self.order_type_input, 2, 1)
        controls_layout.addWidget(QtWidgets.QLabel("Limit price"), 2, 2)
        controls_layout.addWidget(self.limit_price_input, 2, 3)
        controls_layout.addWidget(self.auto_shift_checkbox, 2, 4)
        controls_layout.addWidget(self.shift_bps_input, 2, 5)
        controls_layout.addWidget(QtWidgets.QLabel("TP"), 3, 0)
        controls_layout.addWidget(self.tp_input, 3, 1)
        controls_layout.addWidget(QtWidgets.QLabel("SL"), 3, 2)
        controls_layout.addWidget(self.sl_input, 3, 3)
        controls_layout.addWidget(self.trailing_tp_sl_checkbox, 3, 4)
        controls_layout.addWidget(QtWidgets.QLabel("Trail trigger"), 3, 5)
        controls_layout.addWidget(self.trailing_trigger_input, 3, 6)
        controls_layout.addWidget(self.maker_mode_checkbox, 4, 0)
        controls_layout.addWidget(QtWidgets.QLabel("Skew"), 4, 1)
        controls_layout.addWidget(self.risk_skew_input, 4, 2)
        controls_layout.addWidget(QtWidgets.QLabel("Spread"), 4, 3)
        controls_layout.addWidget(self.spread_multiplier_input, 4, 4)
        controls_layout.addWidget(QtWidgets.QLabel("Position mode"), 4, 5)
        controls_layout.addWidget(self.position_mode_input, 4, 6)
        controls_layout.addWidget(QtWidgets.QLabel("Maker fee"), 5, 0)
        controls_layout.addWidget(self.maker_fee_input, 5, 1)
        controls_layout.addWidget(QtWidgets.QLabel("Taker fee"), 5, 2)
        controls_layout.addWidget(self.taker_fee_input, 5, 3)
        controls_layout.addWidget(QtWidgets.QLabel("Fee buffer"), 5, 4)
        controls_layout.addWidget(self.fee_buffer_input, 5, 5)
        controls_layout.addWidget(QtWidgets.QLabel("Top (24h рост)"), 6, 0)
        controls_layout.addWidget(self.top_n_input, 6, 1)
        controls_layout.addWidget(self.auto_select_checkbox, 6, 2)
        controls_layout.addWidget(QtWidgets.QLabel("Refresh"), 6, 3)
        controls_layout.addWidget(self.auto_select_interval, 6, 4)

        universe_group = QtWidgets.QGroupBox("Universe & Selection")
        universe_group.setProperty("card", "true")
        universe_layout = QtWidgets.QVBoxLayout(universe_group)
        universe_layout.setSpacing(8)

        universe_toolbar = QtWidgets.QHBoxLayout()
        self.auto_select_button = QtWidgets.QPushButton("Refresh symbols")
        universe_toolbar.addWidget(self.auto_select_button)
        universe_toolbar.addStretch()

        self.symbol_table = QtWidgets.QTableWidget(0, 6)
        self.symbol_table.setHorizontalHeaderLabels(
            ["Symbol", "Volume $", "Volatility", "Imbalance", "24h %", "Score"]
        )
        self.symbol_table.verticalHeader().setVisible(False)
        self.symbol_table.setAlternatingRowColors(True)
        self.symbol_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.symbol_table.setMinimumHeight(220)

        self.symbol_list = QtWidgets.QListWidget()
        self.symbol_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.symbol_list.setMinimumHeight(120)
        self.symbol_list.itemChanged.connect(self._on_symbol_item_changed)

        universe_layout.addLayout(universe_toolbar)
        universe_layout.addWidget(self.symbol_table)
        universe_layout.addWidget(QtWidgets.QLabel("Active symbols"))
        universe_layout.addWidget(self.symbol_list)

        log_group = QtWidgets.QGroupBox("Live Log")
        log_group.setProperty("card", "true")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.log_output = QtWidgets.QTextEdit()
        self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)

        left_layout.addWidget(creds_group)
        left_layout.addWidget(controls_group)
        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        divider.setProperty("divider", "true")
        left_layout.addWidget(divider)
        left_layout.addWidget(self._build_portfolio_section())
        left_layout.addStretch()

        right_layout.addWidget(universe_group)
        right_layout.addWidget(log_group)

        self.main_splitter.addWidget(left_panel)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setSizes([420, 560])

        header_widget.setMaximumHeight(24)
        layout.addWidget(header_widget)
        layout.addWidget(self.main_splitter)

    def _setup_backtest_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.backtest_tab)
        form = QtWidgets.QFormLayout()

        self.backtest_steps_input = QtWidgets.QSpinBox()
        self.backtest_steps_input.setRange(10, 5000)
        self.backtest_steps_input.setValue(200)

        self.backtest_button = QtWidgets.QPushButton("Run Backtest")
        self.backtest_output = QtWidgets.QTextEdit()
        self.backtest_output.setReadOnly(True)

        form.addRow("Simulated steps", self.backtest_steps_input)
        layout.addLayout(form)
        layout.addWidget(self.backtest_button)
        layout.addWidget(QtWidgets.QLabel("Backtest Summary"))
        layout.addWidget(self.backtest_output)

    def _setup_history_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.history_tab)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QtWidgets.QLabel("Position History")
        header.setProperty("role", "title")
        layout.addWidget(header)

        self.history_table = QtWidgets.QTableWidget(0, 9)
        self.history_table.setHorizontalHeaderLabels(
            ["Time", "Symbol", "Side", "Action", "Qty", "Price", "Notional (USDT)", "PnL (USDT)", "Reason"]
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.history_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.history_table)

    def _setup_dashboard_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.dashboard_tab)
        self.pnl_label = QtWidgets.QLabel("P&L: 0.0 USDT")
        self.pnl_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pnl_chart = QtWidgets.QTextEdit()
        self.pnl_chart.setReadOnly(True)
        self.pnl_chart.setPlaceholderText("P&L chart placeholder")
        layout.addWidget(self.pnl_label)
        layout.addWidget(self.pnl_chart)

    def _build_portfolio_section(self) -> QtWidgets.QWidget:
        section = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        summary_group = QtWidgets.QGroupBox("Live Portfolio")
        summary_group.setProperty("card", "true")
        summary_layout = QtWidgets.QGridLayout(summary_group)
        summary_layout.setHorizontalSpacing(10)
        summary_layout.setVerticalSpacing(6)

        self.balance_label = QtWidgets.QLabel("Balance: --")
        self.equity_label = QtWidgets.QLabel("Equity: --")
        self.unrealized_label = QtWidgets.QLabel("Unrealized PnL: --")
        self.open_positions_label = QtWidgets.QLabel("Open positions: 0")

        summary_layout.addWidget(self.balance_label, 0, 0)
        summary_layout.addWidget(self.equity_label, 0, 1)
        summary_layout.addWidget(self.unrealized_label, 1, 0)
        summary_layout.addWidget(self.open_positions_label, 1, 1)

        self.positions_table = QtWidgets.QTableWidget(0, 10)
        self.positions_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Side",
                "Size",
                "Entry",
                "Unrealized PnL",
                "Current Price",
                "TP Price",
                "SL Price",
                "TP Profit (USDT)",
                "SL Loss (USDT)",
            ]
        )
        self.positions_table.verticalHeader().setVisible(False)
        self.positions_table.setAlternatingRowColors(True)
        self.positions_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.positions_table.setProperty("role", "positions")
        self.positions_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        layout.addWidget(summary_group)
        layout.addWidget(self.positions_table)
        return section

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #0b0f1a; }
            QLabel, QCheckBox { color: #e6edf3; font-size: 12px; }
            QLabel[role="time_status"] { font-size: 12px; }
            QLabel[role="status"] { font-size: 10.5px; }
            QLabel[role="title"] { font-size: 19px; font-weight: 600; color: #f8fafc; letter-spacing: 0.2px; }
            QLabel[role="subtitle"] { font-size: 11.5px; color: #94a3b8; }
            QLabel[status="idle"] { color: #94a3b8; }
            QLabel[status="ok"] { color: #22c55e; }
            QLabel[status="warn"] { color: #f59e0b; }
            QGroupBox { border: 1px solid #1f2937; border-radius: 10px; margin-top: 2px; background: #0f1422; padding: 5px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 8px; color: #94a3b8; font-weight: 600; }
            QGroupBox[card="true"] { background: #0f172a; }
            QWidget[panel="true"] { background: #0b1220; border: 1px solid #101826; border-radius: 12px; padding: 1px; }
            QFrame[divider="true"] { color: #1f2937; background: #1f2937; min-height: 1px; max-height: 1px; }
            QPushButton { background: #1f6feb; color: white; border-radius: 10px; padding: 5px 10px; }
            QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1d4ed8, stop:1 #3b82f6); }
            QPushButton:checked { background: #22c55e; }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox { background: #0b1220; color: #e6edf3; border: 1px solid #1f2937; padding: 4px; border-radius: 8px; min-height: 24px; }
            QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { width: 20px; }
            QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow { width: 10px; height: 10px; }
            QTextEdit { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; border-radius: 10px; }
            QTableWidget { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; alternate-background-color: #0f172a; }
            QTableWidget::item { padding: 4px; }
            QTableWidget[role="positions"]::item { font-weight: 600; font-size: 12px; }
            QHeaderView::section { background: #111827; color: #94a3b8; padding: 5px; border: none; font-weight: 600; }
            QListWidget { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; border-radius: 10px; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #111827; color: #94a3b8; padding: 5px 10px; border-radius: 10px; margin-right: 6px; min-width: 90px; }
            QTabBar::tab:selected { background: #1f2937; color: #e6edf3; }
            """
        )

    def _load_config(self) -> None:
        data = self.config.load()
        self.api_key_input.setText(data.get("api_key", ""))
        self.api_secret_input.setText(data.get("api_secret", ""))
        self.api_base_url_input.setText(data.get("base_url", "https://api.bybit.com"))
        self.auto_save_checkbox.setChecked(data.get("auto_save", False))
        if geometry := data.get("window_geometry"):
            self.restoreGeometry(QtCore.QByteArray.fromHex(geometry.encode("utf-8")))
        if splitter_sizes := data.get("splitter_sizes"):
            self.main_splitter.setSizes(splitter_sizes)

        self.position_size_input.setValue(data.get("position_size", 500))
        self.max_positions_input.setValue(data.get("max_positions", 5))
        self.tp_input.setValue(data.get("tp_pct", 0.8))
        self.sl_input.setValue(data.get("sl_pct", 0.4))
        self.trailing_tp_sl_checkbox.setChecked(data.get("trailing_tp_sl", False))
        self.trailing_trigger_input.setValue(data.get("trailing_trigger", 0.3))
        self.maker_mode_checkbox.setChecked(data.get("maker_mode", False))
        self.risk_skew_input.setValue(data.get("risk_skew", 0.15))
        self.spread_multiplier_input.setValue(data.get("spread_multiplier", 1.2))
        self.maker_fee_input.setValue(data.get("maker_fee", 0.01))
        self.taker_fee_input.setValue(data.get("taker_fee", 0.06))
        self.fee_buffer_input.setValue(data.get("fee_buffer", 0.1))
        self.top_n_input.setValue(data.get("top_n", 5))
        self.auto_select_interval.setValue(data.get("refresh_interval", 60))
        self.order_type_input.setCurrentText(data.get("order_type", "Market"))
        self.limit_price_input.setValue(data.get("limit_price", 0.0))
        self.auto_shift_checkbox.setChecked(data.get("auto_shift", True))
        self.shift_bps_input.setValue(data.get("shift_bps", 0.05))
        self.position_mode_input.setCurrentText(data.get("position_mode", "Auto-detect"))
        self.auto_select_checkbox.setChecked(data.get("auto_select", True))
        self.selected_symbols = data.get("selected_symbols", [])

    def _setup_logging(self) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=[
                logging.FileHandler(LOG_FILE, encoding="utf-8"),
                QtLogHandler(self.log_output),
            ],
        )
        logging.info("Application started")

    def _wire_signals(self) -> None:
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.auto_trading_toggle.toggled.connect(self._toggle_auto_trading)
        self.auto_save_checkbox.toggled.connect(self._persist_config)
        self.api_key_input.textChanged.connect(self._persist_config)
        self.api_secret_input.textChanged.connect(self._persist_config)
        self.api_base_url_input.textChanged.connect(self._persist_config)
        self.position_size_input.valueChanged.connect(self._persist_config)
        self.max_positions_input.valueChanged.connect(self._persist_config)
        self.tp_input.valueChanged.connect(self._persist_config)
        self.tp_input.valueChanged.connect(self._render_portfolio_table)
        self.sl_input.valueChanged.connect(self._persist_config)
        self.sl_input.valueChanged.connect(self._render_portfolio_table)
        self.trailing_tp_sl_checkbox.toggled.connect(self._persist_config)
        self.trailing_tp_sl_checkbox.toggled.connect(self._render_portfolio_table)
        self.trailing_trigger_input.valueChanged.connect(self._persist_config)
        self.trailing_trigger_input.valueChanged.connect(self._render_portfolio_table)
        self.maker_mode_checkbox.toggled.connect(self._persist_config)
        self.risk_skew_input.valueChanged.connect(self._persist_config)
        self.spread_multiplier_input.valueChanged.connect(self._persist_config)
        self.maker_fee_input.valueChanged.connect(self._persist_config)
        self.taker_fee_input.valueChanged.connect(self._persist_config)
        self.fee_buffer_input.valueChanged.connect(self._persist_config)
        self.top_n_input.valueChanged.connect(self._persist_config)
        self.auto_select_interval.valueChanged.connect(self._persist_config)
        self.order_type_input.currentTextChanged.connect(self._persist_config)
        self.limit_price_input.valueChanged.connect(self._persist_config)
        self.auto_shift_checkbox.toggled.connect(self._persist_config)
        self.shift_bps_input.valueChanged.connect(self._persist_config)
        self.position_mode_input.currentTextChanged.connect(self._persist_config)
        self.auto_select_button.clicked.connect(self._refresh_symbol_table)
        self.auto_select_checkbox.toggled.connect(self._toggle_auto_select)
        self.order_type_input.currentTextChanged.connect(self._toggle_order_type)
        self.trading_timer.timeout.connect(self._run_trading_cycle)
        self.portfolio_timer.timeout.connect(self._request_portfolio)
        self.time_status_timer.timeout.connect(self._update_time_status)
        self.history_timer.timeout.connect(self._request_history)
        self.backtest_button.clicked.connect(self._run_backtest)
        self.backtest_engine.finished.connect(self._update_backtest_results)

    def _persist_config(self) -> None:
        if not self.auto_save_checkbox.isChecked():
            return
        data = {
            "api_key": self.api_key_input.text().strip(),
            "api_secret": self.api_secret_input.text().strip(),
            "base_url": self.api_base_url_input.text().strip(),
            "auto_save": self.auto_save_checkbox.isChecked(),
            "position_size": self.position_size_input.value(),
            "max_positions": self.max_positions_input.value(),
            "tp_pct": self.tp_input.value(),
            "sl_pct": self.sl_input.value(),
            "trailing_tp_sl": self.trailing_tp_sl_checkbox.isChecked(),
            "trailing_trigger": self.trailing_trigger_input.value(),
            "maker_mode": self.maker_mode_checkbox.isChecked(),
            "risk_skew": self.risk_skew_input.value(),
            "spread_multiplier": self.spread_multiplier_input.value(),
            "maker_fee": self.maker_fee_input.value(),
            "taker_fee": self.taker_fee_input.value(),
            "fee_buffer": self.fee_buffer_input.value(),
            "top_n": self.top_n_input.value(),
            "refresh_interval": self.auto_select_interval.value(),
            "order_type": self.order_type_input.currentText(),
            "limit_price": self.limit_price_input.value(),
            "auto_shift": self.auto_shift_checkbox.isChecked(),
            "shift_bps": self.shift_bps_input.value(),
            "position_mode": self.position_mode_input.currentText(),
            "window_geometry": self.saveGeometry().toHex().data().decode("utf-8"),
            "splitter_sizes": self.main_splitter.sizes(),
            "auto_select": self.auto_select_checkbox.isChecked(),
            "selected_symbols": self.selected_symbols,
        }
        self.config.save(data)

    def _toggle_order_type(self, order_type: str) -> None:
        self.limit_price_input.setEnabled(order_type == "Limit")

    def _connect(self) -> None:
        api_key = self.api_key_input.text().strip()
        api_secret = self.api_secret_input.text().strip()
        base_url = self.api_base_url_input.text().strip() or "https://api.bybit.com"
        if not api_key or not api_secret:
            logging.error("API key/secret required to connect.")
            self.connection_status_label.setText("Status: Missing credentials")
            self.connection_status_label.setProperty("status", "warn")
            self.connection_status_label.style().polish(self.connection_status_label)
            return
        self.client = BybitRestClient(api_key, api_secret, base_url)
        self.connected = True
        self.portfolio_ready = False
        self.position_mode_detected = self._detect_position_mode()
        self._request_tickers()
        self._request_instruments()
        self.portfolio_timer.start()
        self.time_status_timer.start()
        self.history_timer.start()
        self._request_history()
        self._update_time_status()
        logging.info("Connected to Bybit futures API at %s", base_url)
        self.connection_status_label.setText("Status: Connected")
        self.connection_status_label.setProperty("status", "ok")
        self.connection_status_label.style().polish(self.connection_status_label)

    def _disconnect(self) -> None:
        self.client = None
        self.connected = False
        self.position_mode_detected = None
        self.ticker_symbols = []
        self.ticker_change_map = {}
        self.ticker_last_price_map = {}
        self.instrument_specs_ready = False
        self.open_positions = []
        self.portfolio_ready = False
        self.trading_stop_cache = {}
        self.portfolio_timer.stop()
        self.time_status_timer.stop()
        self.history_timer.stop()
        if self.history_thread and self.history_thread.isRunning():
            self.history_thread.quit()
        self.history_thread = None
        if self.instrument_thread and self.instrument_thread.isRunning():
            self.instrument_thread.quit()
        self.instrument_thread = None
        if self.portfolio_thread and self.portfolio_thread.isRunning():
            self.portfolio_thread.quit()
        self.portfolio_thread = None
        logging.info("Disconnected from Bybit futures API")
        self.connection_status_label.setText("Status: Disconnected")
        self.connection_status_label.setProperty("status", "idle")
        self.connection_status_label.style().polish(self.connection_status_label)

    def _toggle_auto_trading(self, enabled: bool) -> None:
        if enabled:
            self.auto_trading_toggle.setText("Stop Auto Trading")
            logging.info("Auto trading enabled")
            self._log_strategy_overview()
            self.trading_timer.start()
            self.trading_status_label.setText("Auto-trading: On")
            self.trading_status_label.setProperty("status", "ok")
            self.trading_status_label.style().polish(self.trading_status_label)
        else:
            self.auto_trading_toggle.setText("Start Auto Trading")
            logging.info("Auto trading disabled")
            self.trading_timer.stop()
            self.trading_status_label.setText("Auto-trading: Off")
            self.trading_status_label.setProperty("status", "idle")
            self.trading_status_label.style().polish(self.trading_status_label)

    def _log_strategy_overview(self) -> None:
        logging.info(
            "Strategy: hybrid market making + momentum with inventory skew and fee-aware thresholds."
        )

    def _refresh_symbol_table(self) -> None:
        if self.client and not self.ticker_symbols:
            self._request_tickers()
        self.symbol_metrics = self._generate_symbol_metrics()
        self.symbol_metrics.sort(key=lambda item: item.change_24h, reverse=True)

        self.symbol_table.setRowCount(len(self.symbol_metrics))
        for row, metric in enumerate(self.symbol_metrics):
            self.symbol_table.setItem(row, 0, QtWidgets.QTableWidgetItem(metric.symbol))
            self.symbol_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{metric.volume_usd:,.0f}"))
            self.symbol_table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{metric.volatility:.3f}"))
            self.symbol_table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"{metric.imbalance:.3f}"))
            self.symbol_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{metric.change_24h:.2f}%"))
            self.symbol_table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{metric.score:,.2f}"))
        self._update_symbol_list()
        self._schedule_symbol_refresh()

    def _toggle_auto_select(self, enabled: bool) -> None:
        if enabled:
            self._update_symbol_list()
            self._schedule_symbol_refresh()
            logging.info("Auto-select enabled: top %s symbols", self.top_n_input.value())
        else:
            logging.info("Auto-select disabled")

    def _schedule_symbol_refresh(self) -> None:
        if not self.auto_select_checkbox.isChecked():
            return
        QtCore.QTimer.singleShot(self.auto_select_interval.value() * 1000, self._refresh_symbol_table)

    def _update_symbol_list(self) -> None:
        self._updating_symbol_list = True
        self.symbol_list.clear()
        for metric in self.symbol_metrics:
            item = QtWidgets.QListWidgetItem(metric.symbol)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            if self.auto_select_checkbox.isChecked():
                is_selected = metric in self.symbol_metrics[: self.top_n_input.value()]
            else:
                is_selected = metric.symbol in self.selected_symbols
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if is_selected else QtCore.Qt.CheckState.Unchecked
            )
            self.symbol_list.addItem(item)
        self._updating_symbol_list = False
        if self.auto_select_checkbox.isChecked():
            self.selected_symbols = [
                metric.symbol for metric in self.symbol_metrics[: self.top_n_input.value()]
            ]

    def _is_symbol_selected(self, symbol: str) -> bool:
        for idx in range(self.symbol_list.count()):
            item = self.symbol_list.item(idx)
            if item.text() == symbol and item.checkState() == QtCore.Qt.CheckState.Checked:
                return True
        return False

    def _on_symbol_item_changed(self, item: QtWidgets.QListWidgetItem) -> None:
        if self._updating_symbol_list or self.auto_select_checkbox.isChecked():
            return
        symbol = item.text()
        if item.checkState() == QtCore.Qt.CheckState.Checked:
            if symbol not in self.selected_symbols:
                self.selected_symbols.append(symbol)
        else:
            if symbol in self.selected_symbols:
                self.selected_symbols.remove(symbol)
        self._persist_config()

    def _generate_symbol_metrics(self) -> List[SymbolMetrics]:
        symbols, change_map = self._fetch_symbol_universe()
        metrics = []
        for symbol in symbols:
            change_24h = change_map.get(symbol, random.uniform(-6.0, 12.0))
            metrics.append(
                SymbolMetrics(
                    symbol=symbol,
                    volume_usd=random.uniform(10_000_000, 200_000_000),
                    volatility=random.uniform(0.5, 3.0),
                    imbalance=random.uniform(-1.0, 1.0),
                    change_24h=change_24h,
                )
            )
        return metrics

    def _fetch_symbol_universe(self) -> tuple[List[str], dict]:
        if not self.client:
            fallback = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
            return fallback, {}
        if self.ticker_symbols:
            return self.ticker_symbols, self.ticker_change_map
        fallback = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        return fallback, {}

    def _request_tickers(self) -> None:
        if not self.client or (self.ticker_thread and self.ticker_thread.isRunning()):
            return
        self.ticker_thread = TickerThread(self.client)
        self.ticker_thread.finished.connect(self._on_tickers_ready)
        self.ticker_thread.start()

    def _request_instruments(self) -> None:
        if not self.client or (self.instrument_thread and self.instrument_thread.isRunning()):
            return
        self.instrument_thread = InstrumentThread(self.client)
        self.instrument_thread.finished.connect(self._on_instruments_ready)
        self.instrument_thread.start()

    def _request_portfolio(self) -> None:
        if not self.client or (self.portfolio_thread and self.portfolio_thread.isRunning()):
            return
        self.portfolio_thread = PortfolioThread(self.client)
        self.portfolio_thread.finished.connect(self._on_portfolio_ready)
        self.portfolio_thread.start()

    def _request_history(self) -> None:
        if not self.client or (self.history_thread and self.history_thread.isRunning()):
            return
        self.history_thread = HistoryThread(self.client)
        self.history_thread.finished.connect(self._on_history_ready)
        self.history_thread.start()

    def _update_time_status(self) -> None:
        moscow_time = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=3))
        ).strftime("%Y-%m-%d %H:%M:%S")
        bybit_time_text = "--"
        ping_text = "--"
        if self.client:
            try:
                start = time.time()
                server_time_ms = self.client.fetch_server_time()
                ping_ms = (time.time() - start) * 1000
                ping_text = f"{ping_ms:.0f}"
                if server_time_ms is not None:
                    server_dt = datetime.fromtimestamp(server_time_ms / 1000, tz=timezone.utc)
                    bybit_time_text = server_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except requests.RequestException as exc:
                logging.warning("Failed to fetch Bybit server time: %s", exc)
        self.time_status_label.setText(
            f"Moscow: {moscow_time} | Bybit: {bybit_time_text} | Ping: {ping_text} ms"
        )

    def _on_instruments_ready(self, specs: dict, error: object) -> None:
        if error:
            logging.error("Failed to fetch instrument specs: %s", error)
            return
        if specs:
            self.symbol_specs.update(specs)
            self.instrument_specs_ready = True

    def _on_tickers_ready(self, symbols: list, change_map: dict, last_price_map: dict, error: object) -> None:
        if error:
            logging.error("Failed to fetch symbol universe: %s", error)
            return
        self.ticker_symbols = symbols
        self.ticker_change_map = change_map
        self.ticker_last_price_map = last_price_map
        self._refresh_symbol_table()
        if self.open_positions:
            self._render_portfolio_table()

    def _on_portfolio_ready(self, positions: list, balance: dict, error: object) -> None:
        if error:
            logging.error("Failed to fetch portfolio: %s", error)
            return
        self._update_portfolio(balance, positions)

    def _on_history_ready(self, history: list, error: object) -> None:
        if error:
            logging.error("Failed to fetch position history: %s", error)
            return
        self._update_history_from_api(history)

    def _update_portfolio(self, balance: dict, positions: list) -> None:
        self.open_positions = []
        total_unrealized = 0.0
        for item in positions:
            snapshot = self._parse_portfolio_position(item)
            if snapshot is None:
                continue
            self.open_positions.append(snapshot)
            total_unrealized += snapshot.unrealized_pnl
        self.portfolio_ready = True

        self._render_portfolio_table()
        self._render_balance_summary(balance, total_unrealized)
        self._sync_local_positions()
        self._sync_trading_stops()
        self._monitor_positions_for_exit()

    def _parse_portfolio_position(self, item: dict) -> Optional[PositionSnapshot]:
        symbol = item.get("symbol") or item.get("symbolName")
        if not symbol:
            return None
        raw_size = item.get("size")
        if raw_size is None:
            raw_size = item.get("positionAmt") or item.get("qty") or item.get("positionSize")
        try:
            size = float(raw_size or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size == 0:
            return None
        side = item.get("side") or ""
        if not side:
            side = "Buy" if size > 0 else "Sell"
        size = abs(size)
        entry_raw = item.get("avgPrice")
        if entry_raw is None:
            entry_raw = item.get("entryPrice") or item.get("avgEntryPrice")
        try:
            entry_price = float(entry_raw or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        unrealized_raw = item.get("unrealisedPnl")
        if unrealized_raw is None:
            unrealized_raw = item.get("unrealizedPnl") or item.get("unrealisedPNL")
        try:
            unrealized = float(unrealized_raw or 0)
        except (TypeError, ValueError):
            unrealized = 0.0
        position_idx_raw = item.get("positionIdx")
        try:
            position_idx = int(position_idx_raw) if position_idx_raw is not None else None
        except (TypeError, ValueError):
            position_idx = None
        return PositionSnapshot(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry_price,
            unrealized_pnl=unrealized,
            position_idx=position_idx,
        )

    def _render_portfolio_table(self) -> None:
        self.positions_table.setColumnCount(10)
        self.positions_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Side",
                "Size",
                "Entry",
                "Unrealized PnL",
                "Current Price",
                "TP Price",
                "SL Price",
                "TP Profit (USDT)",
                "SL Loss (USDT)",
            ]
        )
        self.positions_table.setRowCount(len(self.open_positions))
        for row, position in enumerate(self.open_positions):
            self.positions_table.setItem(row, 0, QtWidgets.QTableWidgetItem(position.symbol))
            self.positions_table.setItem(row, 1, QtWidgets.QTableWidgetItem(position.side))
            self.positions_table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{position.size:.6f}"))
            self.positions_table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"{position.entry_price:.4f}"))
            self.positions_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{position.unrealized_pnl:.2f}"))
            last_price = self.ticker_last_price_map.get(position.symbol, position.entry_price)
            self.positions_table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{last_price:.4f}"))
            tp_price, sl_price = self._get_tp_sl_prices(
                position.entry_price,
                last_price,
                position.side,
            )
            self.positions_table.setItem(row, 6, QtWidgets.QTableWidgetItem(f"{tp_price:.4f}"))
            self.positions_table.setItem(row, 7, QtWidgets.QTableWidgetItem(f"{sl_price:.4f}"))
            tp_profit = abs(tp_price - position.entry_price) * position.size
            sl_loss = abs(position.entry_price - sl_price) * position.size
            self.positions_table.setItem(row, 8, QtWidgets.QTableWidgetItem(f"{tp_profit:.2f}"))
            self.positions_table.setItem(row, 9, QtWidgets.QTableWidgetItem(f"{sl_loss:.2f}"))
            side_color = (
                QtGui.QColor(34, 197, 94, 70)
                if position.side.lower() == "buy"
                else QtGui.QColor(239, 68, 68, 70)
            )
            for column in range(self.positions_table.columnCount()):
                item = self.positions_table.item(row, column)
                if item is not None:
                    item.setBackground(side_color)
            pnl_item = self.positions_table.item(row, 4)
            if pnl_item is not None and position.unrealized_pnl != 0:
                pnl_color = (
                    QtGui.QColor(34, 197, 94)
                    if position.unrealized_pnl > 0
                    else QtGui.QColor(239, 68, 68)
                )
                pnl_item.setForeground(pnl_color)
        self.open_positions_label.setText(f"Open positions: {len(self.open_positions)}")

    def _add_history_entry(
        self,
        symbol: str,
        side: str,
        action: str,
        qty: float,
        price: float,
        notional_usdt: float,
        pnl_usdt: Optional[float] = None,
        reason: str = "",
    ) -> None:
        entry = PositionHistoryEntry(
            timestamp=datetime.utcnow(),
            symbol=symbol,
            side=side,
            action=action,
            qty=qty,
            price=price,
            notional_usdt=notional_usdt,
            pnl_usdt=pnl_usdt,
            reason=reason,
        )
        key = (
            f"{entry.timestamp.isoformat()}|{entry.symbol}|{entry.side}|{entry.action}|"
            f"{entry.qty:.6f}|{entry.price:.4f}|{entry.notional_usdt:.2f}|"
            f"{entry.pnl_usdt if entry.pnl_usdt is not None else 'na'}|{entry.reason}"
        )
        self.history_keys.add(key)
        self.position_history.insert(0, entry)
        if len(self.position_history) > 500:
            self.position_history = self.position_history[:500]
        self._render_history_table()

    def _render_history_table(self) -> None:
        if not hasattr(self, "history_table"):
            return
        self.history_table.setRowCount(len(self.position_history))
        for row, entry in enumerate(self.position_history):
            self.history_table.setItem(
                row,
                0,
                QtWidgets.QTableWidgetItem(entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.history_table.setItem(row, 1, QtWidgets.QTableWidgetItem(entry.symbol))
            self.history_table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry.side))
            self.history_table.setItem(row, 3, QtWidgets.QTableWidgetItem(entry.action))
            self.history_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{entry.qty:.6f}"))
            self.history_table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{entry.price:.4f}"))
            self.history_table.setItem(
                row, 6, QtWidgets.QTableWidgetItem(f"{entry.notional_usdt:.2f}")
            )
            pnl_text = "--" if entry.pnl_usdt is None else f"{entry.pnl_usdt:.2f}"
            self.history_table.setItem(row, 7, QtWidgets.QTableWidgetItem(pnl_text))
            self.history_table.setItem(row, 8, QtWidgets.QTableWidgetItem(entry.reason))
            if entry.pnl_usdt is not None:
                pnl_color = (
                    QtGui.QColor(34, 197, 94, 70)
                    if entry.pnl_usdt > 0
                    else QtGui.QColor(239, 68, 68, 70)
                )
                for column in range(self.history_table.columnCount()):
                    item = self.history_table.item(row, column)
                    if item is not None:
                        item.setBackground(pnl_color)

    def _update_history_from_api(self, history: list) -> None:
        if not history:
            return
        new_entries = []
        for item in history:
            symbol = item.get("symbol")
            side = item.get("side") or ""
            qty_raw = item.get("qty") or item.get("closedSize") or item.get("size")
            price_raw = (
                item.get("avgExitPrice")
                or item.get("avgPrice")
                or item.get("exitPrice")
                or item.get("price")
            )
            pnl_raw = item.get("closedPnl") or item.get("pnl") or item.get("realisedPnl")
            ts_raw = item.get("updatedTime") or item.get("createdTime") or item.get("execTime")
            if not symbol or not qty_raw or not price_raw or not ts_raw:
                continue
            try:
                qty = float(qty_raw)
                price = float(price_raw)
                ts_ms = int(float(ts_raw))
                pnl_usdt = float(pnl_raw) if pnl_raw is not None else None
            except (TypeError, ValueError):
                continue
            timestamp = datetime.utcfromtimestamp(ts_ms / 1000)
            notional = qty * price
            entry = PositionHistoryEntry(
                timestamp=timestamp,
                symbol=symbol,
                side=side or "Unknown",
                action="Exit",
                qty=qty,
                price=price,
                notional_usdt=notional,
                pnl_usdt=pnl_usdt,
                reason="API",
            )
            key = (
                f"{timestamp.isoformat()}|{entry.symbol}|{entry.side}|{entry.action}|"
                f"{entry.qty:.6f}|{entry.price:.4f}|{entry.notional_usdt:.2f}|"
                f"{entry.pnl_usdt if entry.pnl_usdt is not None else 'na'}|{entry.reason}"
            )
            if key in self.history_keys:
                continue
            self.history_keys.add(key)
            new_entries.append(entry)
        if new_entries:
            self.position_history = new_entries + self.position_history
            self.position_history = self.position_history[:500]
            self._render_history_table()

    def _render_balance_summary(self, balance: dict, total_unrealized: float) -> None:
        total_equity = "--"
        total_wallet = "--"
        if balance:
            account_list = balance.get("list", [])
            if account_list:
                account = account_list[0]
                total_equity = account.get("totalEquity", "--")
                total_wallet = account.get("totalWalletBalance", "--")
        self.balance_label.setText(f"Balance: {total_wallet}")
        self.equity_label.setText(f"Equity: {total_equity}")
        self.unrealized_label.setText(f"Unrealized PnL: {total_unrealized:,.2f}")

    def _sync_trading_stops(self) -> None:
        if not self.client or not self.connected:
            return
        for position in self.open_positions:
            last_price = self.ticker_last_price_map.get(position.symbol, position.entry_price)
            if position.entry_price <= 0 or last_price <= 0:
                continue
            tp_price, sl_price = self._get_tp_sl_prices(
                position.entry_price,
                last_price,
                position.side,
            )
            position_idx = (
                position.position_idx
                if position.position_idx is not None
                else self._resolve_position_idx(position.side)
            )
            if position_idx is None:
                continue
            cache_key = (position.symbol, position_idx)
            cached = self.trading_stop_cache.get(cache_key)
            rounded = (round(tp_price, 6), round(sl_price, 6))
            if cached == rounded:
                continue
            self._queue_trading_stop(
                TradingStopRequest(
                    symbol=position.symbol,
                    position_idx=position_idx,
                    take_profit=tp_price,
                    stop_loss=sl_price,
                    source="sync",
                )
            )

    def _get_tp_sl_prices(
        self,
        entry_price: float,
        last_price: float,
        side: str,
        base_tp: Optional[float] = None,
        base_sl: Optional[float] = None,
    ) -> tuple[float, float]:
        tp_pct = self.tp_input.value() / 100
        sl_pct = self.sl_input.value() / 100
        direction = 1 if side.lower() == "buy" else -1
        trigger_pct = self.trailing_trigger_input.value() / 100
        profit_pct = 0.0
        if entry_price > 0:
            profit_pct = (last_price - entry_price) / entry_price * direction
        trailing_enabled = self.trailing_tp_sl_checkbox.isChecked() and profit_pct >= trigger_pct
        tp_base_price = entry_price
        sl_base_price = last_price if trailing_enabled else entry_price
        tp_price = tp_base_price * (1 + (tp_pct * direction))
        sl_price = sl_base_price * (1 - (sl_pct * direction))
        if base_tp is not None:
            tp_price = max(tp_price, base_tp) if direction > 0 else min(tp_price, base_tp)
        if base_sl is not None:
            sl_price = max(sl_price, base_sl) if direction > 0 else min(sl_price, base_sl)
        return tp_price, sl_price

    def _evaluate_tp_sl(
        self,
        entry_price: float,
        last_price: float,
        side: str,
        base_tp: Optional[float] = None,
        base_sl: Optional[float] = None,
    ) -> tuple[float, float, bool, bool]:
        tp_pct = self.tp_input.value() / 100
        sl_pct = self.sl_input.value() / 100
        tp_price, sl_price = self._get_tp_sl_prices(
            entry_price,
            last_price,
            side,
            base_tp=base_tp,
            base_sl=base_sl,
        )
        direction = 1 if side.lower() == "buy" else -1
        hit_tp = False
        hit_sl = False
        if tp_pct > 0:
            hit_tp = last_price >= tp_price if direction > 0 else last_price <= tp_price
        if sl_pct > 0:
            hit_sl = last_price <= sl_price if direction > 0 else last_price >= sl_price
        return tp_price, sl_price, hit_tp, hit_sl

    def _resolve_exit_reason(
        self,
        hit_tp: bool,
        hit_sl: bool,
        entry_price: Optional[float] = None,
        last_price: Optional[float] = None,
        side: Optional[str] = None,
    ) -> Optional[str]:
        if hit_tp and hit_sl:
            if entry_price is None or last_price is None or side is None:
                logging.warning("TP and SL both triggered; defaulting to TP due to missing context.")
                return "TP"
            direction = 1 if side.lower() == "buy" else -1
            in_profit = (last_price - entry_price) * direction >= 0
            exit_reason = "TP" if in_profit else "SL"
            logging.warning(
                "TP and SL both triggered; resolving to %s based on P&L.",
                exit_reason,
            )
            return exit_reason
        if hit_tp:
            return "TP"
        if hit_sl:
            return "SL"
        return None

    def _monitor_positions_for_exit(self) -> None:
        if not self.auto_trading_toggle.isChecked():
            return
        for position in self.open_positions:
            last_price = self.ticker_last_price_map.get(position.symbol, position.entry_price)
            if not last_price or position.entry_price <= 0:
                continue
            direction = 1 if position.side.lower() == "buy" else -1
            tp_price, sl_price, hit_tp, hit_sl = self._evaluate_tp_sl(
                position.entry_price,
                last_price,
                position.side,
            )
            exit_reason = self._resolve_exit_reason(
                hit_tp,
                hit_sl,
                entry_price=position.entry_price,
                last_price=last_price,
                side=position.side,
            )
            if exit_reason:
                close_side = "Sell" if direction > 0 else "Buy"
                position_idx = (
                    position.position_idx
                    if position.position_idx is not None
                    else self._resolve_position_idx(position.side)
                )
                self._place_order(
                    position.symbol,
                    close_side,
                    position.size,
                    price=last_price,
                    reduce_only=True,
                    position_idx_override=position_idx,
                    force_market=True,
                )
                exit_pnl = (last_price - position.entry_price) * position.size * direction
                self._add_history_entry(
                    position.symbol,
                    close_side,
                    "Exit",
                    position.size,
                    last_price,
                    notional_usdt=position.size * last_price,
                    pnl_usdt=exit_pnl,
                    reason=exit_reason,
                )
                logging.info(
                    "%s exit %s @ %.2f (TP %.2f / SL %.2f)",
                    position.symbol,
                    exit_reason,
                    last_price,
                    tp_price,
                    sl_price,
                )

    def _run_backtest(self) -> None:
        steps = self.backtest_steps_input.value()
        logging.info("Running backtest for %s steps", steps)
        fee_buffer = self.fee_buffer_input.value() / 100
        self.backtest_engine.run_dummy(steps, fee_buffer)

    def _update_backtest_results(self, timestamps: list, pnl_series: list) -> None:
        if not pnl_series:
            return
        total = pnl_series[-1]
        max_dd = min(pnl_series)
        summary = (
            f"Start: {datetime.now():%Y-%m-%d %H:%M}\n"
            f"Steps: {len(pnl_series)}\n"
            f"Total P&L: {total:,.2f} USDT\n"
            f"Max drawdown: {max_dd:,.2f} USDT\n"
        )
        self.backtest_output.setText(summary)
        self.pnl_label.setText(f"P&L: {total:,.2f} USDT")
        chart_preview = "\n".join(
            f"{idx:03d} | {value:,.2f}" for idx, value in zip(timestamps[-30:], pnl_series[-30:])
        )
        self.pnl_chart.setText(chart_preview)

    def _run_trading_cycle(self) -> None:
        self.strategy.maker_fee = self.maker_fee_input.value() / 100
        self.strategy.taker_fee = self.taker_fee_input.value() / 100
        fee_buffer = self.fee_buffer_input.value() / 100
        active_symbols = self._get_active_symbols()
        for symbol in active_symbols:
            snapshot = self._simulate_market_snapshot(symbol)
            self._apply_strategy(snapshot, fee_buffer)

    def _get_active_symbols(self) -> List[str]:
        if self.auto_select_checkbox.isChecked():
            return [metric.symbol for metric in self.symbol_metrics[: self.top_n_input.value()]]
        return [symbol for symbol in self.selected_symbols if symbol in {m.symbol for m in self.symbol_metrics}]

    def _simulate_market_snapshot(self, symbol: str) -> MarketSnapshot:
        base = 30000 if symbol == "BTCUSDT" else 2000 if symbol == "ETHUSDT" else 100
        mid = base + random.uniform(-1, 1) * base * 0.001
        spread = random.uniform(0.02, 0.08) * base * 0.001
        bid = mid - spread / 2
        ask = mid + spread / 2
        bid_size = random.uniform(10, 80)
        ask_size = random.uniform(10, 80)
        imbalance = (bid_size - ask_size) / max(bid_size + ask_size, 1e-9)
        volatility = random.uniform(0.3, 2.5)
        return MarketSnapshot(
            symbol=symbol,
            mid=mid,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volatility=volatility,
            imbalance=imbalance,
        )

    def _apply_strategy(self, snapshot: MarketSnapshot, fee_buffer: float) -> None:
        position = self.positions.get(snapshot.symbol, PositionState(symbol=snapshot.symbol))
        use_maker = self.maker_mode_checkbox.isChecked()
        required_edge = self.strategy.required_edge(use_maker, fee_buffer)
        momentum = snapshot.volatility * snapshot.imbalance

        if self._has_open_position(snapshot.symbol):
            return

        if position.qty != 0:
            self._check_exit(snapshot, position)
            self.positions[snapshot.symbol] = position
            return

        if abs(momentum) > required_edge:
            if self.connected and not self.portfolio_ready:
                logging.info("Portfolio not synced yet; skipping new entry.")
                return
            if self._count_open_positions() >= self.max_positions_input.value():
                logging.info("Max positions reached; skipping new entry.")
                return
            if not self.instrument_specs_ready:
                logging.warning("Instrument specs not loaded; skipping entry sizing.")
                return
            direction = 1 if momentum > 0 else -1
            entry = snapshot.ask if direction > 0 else snapshot.bid
            desired_usdt = self.position_size_input.value()
            reference_price = self.ticker_last_price_map.get(snapshot.symbol)
            if not reference_price:
                logging.warning("No live price for %s; skipping entry sizing.", snapshot.symbol)
                return
            raw_qty = desired_usdt / reference_price
            normalized_qty = self._normalize_qty(snapshot.symbol, raw_qty, reference_price)
            if normalized_qty is None:
                logging.error(
                    "Order rejected locally: %s %s %.2f USDT (min notional/min qty)",
                    "Buy" if direction > 0 else "Sell",
                    snapshot.symbol,
                    desired_usdt,
                )
                return
            position.qty = direction * normalized_qty
            position.entry_price = entry
            tp_pct = self.tp_input.value() / 100
            sl_pct = self.sl_input.value() / 100
            position.tp_price = entry * (1 + tp_pct * direction)
            position.sl_price = entry * (1 - sl_pct * direction)
            position_idx = self._resolve_position_idx("Buy" if direction > 0 else "Sell")
            position.position_idx = position_idx
            self.positions[snapshot.symbol] = position
            self._place_order(
                snapshot.symbol,
                "Buy" if direction > 0 else "Sell",
                abs(position.qty),
                price=entry,
                position_idx_override=position_idx,
                tp_price=position.tp_price,
                sl_price=position.sl_price,
                set_trading_stop=True,
                force_market=True,
            )
            self._add_history_entry(
                snapshot.symbol,
                "Buy" if direction > 0 else "Sell",
                "Entry",
                abs(position.qty),
                entry,
                notional_usdt=abs(position.qty) * entry,
                reason="Momentum",
            )
            logging.info(
                "%s momentum entry %s @ %.2f (TP %.2f / SL %.2f)",
                snapshot.symbol,
                "LONG" if direction > 0 else "SHORT",
                entry,
                position.tp_price,
                position.sl_price,
            )
            return

        if use_maker:
            quote_bid, quote_ask = self.strategy.compute_quotes(
                snapshot.bid,
                snapshot.ask,
                snapshot.bid_size,
                snapshot.ask_size,
                risk_skew=self.risk_skew_input.value(),
                spread_multiplier=self.spread_multiplier_input.value(),
            )
            logging.info(
                "%s maker quotes bid=%.2f ask=%.2f (imbalance %.2f)",
                snapshot.symbol,
                quote_bid,
                quote_ask,
                snapshot.imbalance,
            )

    def _check_exit(self, snapshot: MarketSnapshot, position: PositionState) -> None:
        if position.qty == 0:
            return
        if self.open_positions and not any(
            open_position.symbol == snapshot.symbol for open_position in self.open_positions
        ):
            position.qty = 0
            position.entry_price = 0.0
            position.tp_price = 0.0
            position.sl_price = 0.0
            position.position_idx = None
            self.positions[snapshot.symbol] = position
            logging.warning("Local position cleared (not on exchange): %s", snapshot.symbol)
            return
        direction = 1 if position.qty > 0 else -1
        tp_price, sl_price, hit_tp, hit_sl = self._evaluate_tp_sl(
            position.entry_price,
            snapshot.mid,
            "Buy" if direction > 0 else "Sell",
            base_tp=position.tp_price,
            base_sl=position.sl_price,
        )
        position.tp_price = tp_price
        position.sl_price = sl_price
        exit_reason = self._resolve_exit_reason(
            hit_tp,
            hit_sl,
            entry_price=position.entry_price,
            last_price=snapshot.mid,
            side="Buy" if direction > 0 else "Sell",
        )
        if exit_reason:
            exit_price = snapshot.bid if direction > 0 else snapshot.ask
            pnl = (exit_price - position.entry_price) * position.qty
            position_side = "Buy" if direction > 0 else "Sell"
            position_idx = (
                position.position_idx
                if position.position_idx is not None
                else self._resolve_position_idx(position_side)
            )
            self._place_order(
                snapshot.symbol,
                "Sell" if direction > 0 else "Buy",
                abs(position.qty),
                price=exit_price,
                reduce_only=True,
                position_idx_override=position_idx,
                force_market=True,
            )
            self._add_history_entry(
                snapshot.symbol,
                "Sell" if direction > 0 else "Buy",
                "Exit",
                abs(position.qty),
                exit_price,
                notional_usdt=abs(position.qty) * exit_price,
                pnl_usdt=pnl,
                reason=exit_reason,
            )
            logging.info(
                "%s exit %s @ %.2f P&L %.2f",
                snapshot.symbol,
                exit_reason,
                exit_price,
                pnl,
            )
            position.qty = 0
            position.entry_price = 0.0
            position.tp_price = 0.0
            position.sl_price = 0.0
            position.position_idx = None

    def _count_open_positions(self) -> int:
        open_symbols = {position.symbol for position in self.open_positions}
        local_symbols = {symbol for symbol, pos in self.positions.items() if pos.qty != 0}
        return len(open_symbols | local_symbols)

    def _sync_local_positions(self) -> None:
        if not self.open_positions:
            return
        open_symbols = {position.symbol for position in self.open_positions}
        for symbol, position in self.positions.items():
            if position.qty != 0 and symbol not in open_symbols:
                position.qty = 0
                position.entry_price = 0.0
                position.tp_price = 0.0
                position.sl_price = 0.0
                position.position_idx = None
                self.positions[symbol] = position
                logging.warning("Synced local position to exchange: %s cleared", symbol)

    def _has_open_position(self, symbol: str) -> bool:
        if self.open_positions:
            return any(position.symbol == symbol for position in self.open_positions)
        position = self.positions.get(symbol)
        return bool(position and position.qty != 0)

    def _place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
        position_idx_override: Optional[int] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        set_trading_stop: bool = False,
        force_market: bool = False,
    ) -> None:
        if not self.auto_trading_toggle.isChecked():
            logging.warning("Order blocked (auto-trading disabled): %s %s %.6f", side, symbol, qty)
            return
        if not self.connected or not self.client:
            logging.warning("Order skipped (not connected): %s %s %.6f", side, symbol, qty)
            return
        reference_price = self.ticker_last_price_map.get(symbol) or price
        normalized_qty = self._normalize_qty(symbol, qty, reference_price)
        if normalized_qty is None:
            specs = self.symbol_specs.get(symbol, {})
            min_notional = specs.get("min_notional", 0.0)
            if price and min_notional > 0:
                logging.error(
                    "Order rejected locally: %s %s %.6f (min notional %.2f USDT)",
                    side,
                    symbol,
                    qty,
                    min_notional,
                )
            else:
                logging.error("Order rejected locally: %s %s %.6f (below min qty)", side, symbol, qty)
            return
        position_idx = (
            position_idx_override
            if position_idx_override is not None
            else self._resolve_position_idx(side)
        )
        order_type = "Market" if force_market else self.order_type_input.currentText()
        limit_price = None
        if order_type == "Limit" and not force_market:
            limit_price = self.limit_price_input.value()
            if limit_price <= 0 and price is not None:
                limit_price = price
            if limit_price is None or limit_price <= 0:
                logging.error("Limit price must be greater than 0.")
                return
            if self.auto_shift_checkbox.isChecked():
                shift_key = (symbol, side)
                attempt = self.limit_shift_attempts.get(shift_key, 0) + 1
                direction = 1 if side == "Buy" else -1
                shift_pct = self.shift_bps_input.value() / 100
                limit_price = limit_price * (1 + (shift_pct * attempt * direction))
                self.limit_shift_attempts[shift_key] = attempt
                logging.info(
                    "Auto-shift limit price (%s attempt %s): %.4f",
                    symbol,
                    attempt,
                    limit_price,
                )
        if limit_price:
            normalized_qty = self._normalize_qty(symbol, normalized_qty, limit_price)
            if normalized_qty is None:
                logging.error("Order rejected locally: %s %s (limit min notional/min qty)", side, symbol)
                return
        request = OrderRequest(
            symbol=symbol,
            side=side,
            qty=normalized_qty,
            position_idx=position_idx,
            order_type=order_type,
            limit_price=limit_price,
            reduce_only=reduce_only,
            tp_price=tp_price,
            sl_price=sl_price,
            set_trading_stop=set_trading_stop,
            remaining_qty=normalized_qty if order_type == "Limit" else None,
        )
        self._dispatch_order(request)

    def _dispatch_order(self, request: OrderRequest) -> None:
        if not self.client:
            return
        thread = OrderThread(self.client, request)
        thread.finished.connect(self._on_order_finished)
        self.order_threads.append(thread)
        thread.start()

    def _queue_trading_stop(self, request: TradingStopRequest) -> None:
        if not self.client:
            return
        thread = TradingStopThread(self.client, request)
        thread.finished.connect(self._on_trading_stop_finished)
        self.trading_stop_threads.append(thread)
        thread.start()

    def _on_order_finished(self, request: OrderRequest, response: Optional[dict], error: object) -> None:
        if error:
            logging.error("Order failed: %s", error)
            return
        if response is None:
            logging.error("Order failed: empty response.")
            return
        if self._is_position_mode_error(response) and request.retry == 0:
            fallback_idx = 0 if request.position_idx in (1, 2) else (1 if request.side == "Buy" else 2)
            if fallback_idx == request.position_idx:
                logging.error(
                    "Position mode mismatch persists; no alternate positionIdx available."
                )
                return
            logging.warning("Position mode mismatch; retrying with positionIdx=%s", fallback_idx)
            retry_request = OrderRequest(
                symbol=request.symbol,
                side=request.side,
                qty=request.qty,
                position_idx=fallback_idx,
                order_type=request.order_type,
                limit_price=request.limit_price,
                reduce_only=request.reduce_only,
                retry=1,
            )
            self._dispatch_order(retry_request)
            return
        ret_code = response.get("retCode")
        if ret_code != 0:
            if not request.reduce_only:
                position = self.positions.get(request.symbol)
                if position:
                    position.qty = 0
                    position.entry_price = 0.0
                    position.tp_price = 0.0
                    position.sl_price = 0.0
                    position.position_idx = None
                    self.positions[request.symbol] = position
                    logging.warning(
                        "Reset local position state after rejected entry: %s %s %.6f",
                        request.side,
                        request.symbol,
                        request.qty,
                    )
            logging.error(
                "Order rejected: %s %s %.6f -> %s",
                request.side,
                request.symbol,
                request.qty,
                response,
            )
            return
        logging.info(
            "Order sent: %s %s %.6f -> %s",
            request.side,
            request.symbol,
            request.qty,
            response,
        )
        if request.set_trading_stop and not request.reduce_only:
            if request.tp_price is not None or request.sl_price is not None:
                self._queue_trading_stop(
                    TradingStopRequest(
                        symbol=request.symbol,
                        position_idx=request.position_idx,
                        take_profit=request.tp_price,
                        stop_loss=request.sl_price,
                        source="entry",
                    )
                )
        if request.order_type == "Limit" and request.remaining_qty:
            filled = self._extract_filled_qty(response)
            remaining = max(request.remaining_qty - filled, 0)
            if remaining > 0:
                logging.info(
                    "Partial fill detected: %.6f remaining for %s %s",
                    remaining,
                    request.side,
                    request.symbol,
                )
                retry_request = OrderRequest(
                    symbol=request.symbol,
                    side=request.side,
                    qty=remaining,
                    position_idx=request.position_idx,
                    order_type=request.order_type,
                    limit_price=request.limit_price,
                    reduce_only=request.reduce_only,
                    retry=request.retry + 1,
                    remaining_qty=remaining,
                )
                self._dispatch_order(retry_request)
                return
        if request.order_type == "Limit":
            self.limit_shift_attempts.pop((request.symbol, request.side), None)

    def _on_trading_stop_finished(
        self,
        request: TradingStopRequest,
        response: Optional[dict],
        error: object,
    ) -> None:
        if error:
            logging.error(
                "Failed to set TP/SL (%s) for %s: %s",
                request.source,
                request.symbol,
                error,
            )
            return
        if response is None:
            logging.error("Failed to set TP/SL (%s) for %s: empty response.", request.source, request.symbol)
            return
        ret_code = response.get("retCode")
        if ret_code != 0:
            logging.error(
                "TP/SL rejected (%s) for %s -> %s",
                request.source,
                request.symbol,
                response,
            )
            return
        cache_key = (request.symbol, request.position_idx)
        tp_val = round(request.take_profit, 6) if request.take_profit is not None else 0.0
        sl_val = round(request.stop_loss, 6) if request.stop_loss is not None else 0.0
        self.trading_stop_cache[cache_key] = (tp_val, sl_val)
        logging.info(
            "TP/SL set (%s) for %s: TP %.6f / SL %.6f",
            request.source,
            request.symbol,
            tp_val,
            sl_val,
        )

    def _is_position_mode_error(self, response: dict) -> bool:
        ret_msg = str(response.get("retMsg", "")).lower()
        return response.get("retCode") == 10001 and "position idx not match position mode" in ret_msg

    def _extract_filled_qty(self, response: dict) -> float:
        result = response.get("result", {})
        qty = result.get("qty")
        if qty is not None:
            try:
                return float(qty)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _resolve_position_idx(self, side: str) -> Optional[int]:
        selection = self.position_mode_input.currentIndex()
        if selection == 1:
            return 0
        if selection == 2:
            return 1 if side == "Buy" else 2
        if self.position_mode_detected == "one-way":
            return 0
        if self.position_mode_detected == "hedge":
            return 1 if side == "Buy" else 2
        logging.warning("Position mode unknown; defaulting to one-way (posIdx 0).")
        return 0

    def _detect_position_mode(self) -> Optional[str]:
        if not self.client:
            return None
        try:
            mode = self.client.fetch_position_mode()
            if mode:
                logging.info("Detected position mode: %s", mode)
            else:
                logging.warning("Position mode detection failed; defaulting to one-way.")
                mode = "one-way"
            return mode
        except requests.RequestException as exc:
            logging.error("Position mode detection error: %s. Defaulting to one-way.", exc)
            return "one-way"

    def _normalize_qty(self, symbol: str, qty: float, price: Optional[float]) -> Optional[float]:
        specs = self.symbol_specs.get(symbol, {"min_qty": 0.001, "step": 0.001, "min_notional": 0.0})
        step = specs["step"]
        min_qty = specs["min_qty"]
        min_notional = specs.get("min_notional", 0.0)
        if step <= 0 or min_qty <= 0:
            return None
        normalized = (qty // step) * step
        normalized = round(normalized, 6)
        if normalized < min_qty:
            return None
        if price and min_notional > 0 and (normalized * price) < min_notional:
            return None
        return normalized


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("HFT Lab")
    app.setApplicationName("Bybit HFT Suite")
    window = TradingApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
