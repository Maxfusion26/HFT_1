import json
import logging
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6 import QtCore, QtWidgets
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


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: float
    position_idx: int
    order_type: str
    limit_price: Optional[float]
    retry: int = 0
    remaining_qty: Optional[float] = None


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
            )
            self.finished.emit(self.request, response, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(self.request, None, exc)


class TickerThread(QtCore.QThread):
    finished = QtCore.pyqtSignal(list, dict, object)

    def __init__(self, client: BybitRestClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            tickers = self.client.fetch_linear_tickers()
            change_map = {}
            symbols = []
            for ticker in tickers:
                symbol = ticker.get("symbol")
                last_price = float(ticker.get("lastPrice", 0) or 0)
                prev_price = float(ticker.get("prevPrice24h", 0) or 0)
                if symbol:
                    symbols.append(symbol)
                    if prev_price > 0:
                        change_map[symbol] = ((last_price - prev_price) / prev_price) * 100
            self.finished.emit(symbols, change_map, None)
        except Exception as exc:  # noqa: BLE001
            self.finished.emit([], {}, exc)


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
        self.resize(980, 660)
        self.config = ConfigManager(CONFIG_FILE)
        self.strategy = HFTStrategy()
        self.backtest_engine = BacktestEngine(self.strategy)
        self.client: Optional[BybitRestClient] = None
        self.connected = False
        self.position_mode_detected: Optional[str] = None
        self.limit_shift_attempts: Dict[tuple, int] = {}
        self.order_threads: List[OrderThread] = []
        self.ticker_thread: Optional[TickerThread] = None
        self.instrument_thread: Optional[InstrumentThread] = None
        self.ticker_symbols: List[str] = []
        self.ticker_change_map: Dict[str, float] = {}
        self.symbol_specs = {
            "BTCUSDT": {"min_qty": 0.001, "step": 0.001, "min_notional": 0.0},
            "ETHUSDT": {"min_qty": 0.01, "step": 0.01, "min_notional": 0.0},
            "BNBUSDT": {"min_qty": 0.1, "step": 0.1, "min_notional": 0.0},
            "SOLUSDT": {"min_qty": 0.1, "step": 0.1, "min_notional": 0.0},
            "XRPUSDT": {"min_qty": 1.0, "step": 1.0, "min_notional": 0.0},
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

        self.tabs.addTab(self.trading_tab, "Trading")
        self.tabs.addTab(self.backtest_tab, "Backtesting")
        self.tabs.addTab(self.dashboard_tab, "Dashboard")

        self._setup_trading_tab()
        self._setup_backtest_tab()
        self._setup_dashboard_tab()

    def _setup_trading_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.trading_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Bybit Futures HFT Suite")
        title.setProperty("role", "title")
        subtitle = QtWidgets.QLabel("Adaptive market maker • Momentum overlay")
        subtitle.setProperty("role", "subtitle")
        title_wrap = QtWidgets.QVBoxLayout()
        title_wrap.addWidget(title)
        title_wrap.addWidget(subtitle)

        self.connection_status_label = QtWidgets.QLabel("Disconnected")
        self.connection_status_label.setProperty("status", "idle")
        self.trading_status_label = QtWidgets.QLabel("Auto-trading Off")
        self.trading_status_label.setProperty("status", "idle")
        status_wrap = QtWidgets.QVBoxLayout()
        status_wrap.addWidget(self.connection_status_label, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        status_wrap.addWidget(self.trading_status_label, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        header.addLayout(title_wrap)
        header.addStretch()
        header.addLayout(status_wrap)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

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
        controls_layout.setHorizontalSpacing(10)
        controls_layout.setVerticalSpacing(8)

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
        self.top_n_input.setRange(5, 5)
        self.top_n_input.setValue(5)
        self.top_n_input.setEnabled(False)

        self.auto_select_checkbox = QtWidgets.QCheckBox("Auto-select top symbols")
        self.auto_select_checkbox.setChecked(True)
        self.auto_select_checkbox.setEnabled(False)
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
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.symbol_table.setMinimumHeight(220)

        self.symbol_list = QtWidgets.QListWidget()
        self.symbol_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.symbol_list.setMinimumHeight(120)

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
        left_layout.addStretch()

        right_layout.addWidget(universe_group)
        right_layout.addWidget(log_group)

        self.main_splitter.addWidget(left_panel)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setSizes([420, 560])

        layout.addLayout(header)
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

    def _setup_dashboard_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.dashboard_tab)
        self.pnl_label = QtWidgets.QLabel("P&L: 0.0 USDT")
        self.pnl_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pnl_chart = QtWidgets.QTextEdit()
        self.pnl_chart.setReadOnly(True)
        self.pnl_chart.setPlaceholderText("P&L chart placeholder")
        layout.addWidget(self.pnl_label)
        layout.addWidget(self.pnl_chart)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #0b0f1a; }
            QLabel, QCheckBox { color: #e6edf3; font-size: 12px; }
            QLabel[role="title"] { font-size: 18px; font-weight: 600; color: #f8fafc; }
            QLabel[role="subtitle"] { font-size: 11px; color: #94a3b8; }
            QLabel[status="idle"] { color: #94a3b8; }
            QLabel[status="ok"] { color: #22c55e; }
            QLabel[status="warn"] { color: #f59e0b; }
            QGroupBox { border: 1px solid #1f2937; border-radius: 12px; margin-top: 8px; background: #0f1422; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #94a3b8; }
            QGroupBox[card="true"] { background: #0f172a; }
            QPushButton { background: #1f6feb; color: white; border-radius: 10px; padding: 7px 16px; }
            QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1d4ed8, stop:1 #3b82f6); }
            QPushButton:checked { background: #22c55e; }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox { background: #0b1220; color: #e6edf3; border: 1px solid #1f2937; padding: 6px; border-radius: 8px; }
            QTextEdit { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; border-radius: 10px; }
            QTableWidget { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; }
            QHeaderView::section { background: #111827; color: #94a3b8; padding: 6px; border: none; }
            QListWidget { background: #0b1220; color: #c9d1d9; border: 1px solid #1f2937; border-radius: 10px; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #111827; color: #94a3b8; padding: 7px 14px; border-radius: 10px; margin-right: 6px; }
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
        self.tp_input.setValue(data.get("tp_pct", 0.8))
        self.sl_input.setValue(data.get("sl_pct", 0.4))
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
        self.tp_input.valueChanged.connect(self._persist_config)
        self.sl_input.valueChanged.connect(self._persist_config)
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
            "tp_pct": self.tp_input.value(),
            "sl_pct": self.sl_input.value(),
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
        self.position_mode_detected = self._detect_position_mode()
        self._request_tickers()
        self._request_instruments()
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
        if self.instrument_thread and self.instrument_thread.isRunning():
            self.instrument_thread.quit()
        self.instrument_thread = None
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
        self.symbol_list.clear()
        for metric in self.symbol_metrics:
            item = QtWidgets.QListWidgetItem(metric.symbol)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            is_selected = self._is_symbol_selected(metric.symbol)
            if self.auto_select_checkbox.isChecked():
                is_selected = metric in self.symbol_metrics[: self.top_n_input.value()]
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if is_selected else QtCore.Qt.CheckState.Unchecked
            )
            self.symbol_list.addItem(item)

    def _is_symbol_selected(self, symbol: str) -> bool:
        for idx in range(self.symbol_list.count()):
            item = self.symbol_list.item(idx)
            if item.text() == symbol and item.checkState() == QtCore.Qt.CheckState.Checked:
                return True
        return False

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

    def _on_instruments_ready(self, specs: dict, error: object) -> None:
        if error:
            logging.error("Failed to fetch instrument specs: %s", error)
            return
        if specs:
            self.symbol_specs.update(specs)

    def _on_tickers_ready(self, symbols: list, change_map: dict, error: object) -> None:
        if error:
            logging.error("Failed to fetch symbol universe: %s", error)
            return
        self.ticker_symbols = symbols
        self.ticker_change_map = change_map
        self._refresh_symbol_table()

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
        top_n = 5
        return [metric.symbol for metric in self.symbol_metrics[:top_n]]

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

        if position.qty != 0:
            self._check_exit(snapshot, position)
            self.positions[snapshot.symbol] = position
            return

        if abs(momentum) > required_edge:
            direction = 1 if momentum > 0 else -1
            entry = snapshot.ask if direction > 0 else snapshot.bid
            position.qty = direction * (self.position_size_input.value() / snapshot.mid)
            position.entry_price = entry
            tp_pct = self.tp_input.value() / 100
            sl_pct = self.sl_input.value() / 100
            position.tp_price = entry * (1 + tp_pct * direction)
            position.sl_price = entry * (1 - sl_pct * direction)
            self.positions[snapshot.symbol] = position
            self._place_order(
                snapshot.symbol,
                "Buy" if direction > 0 else "Sell",
                abs(position.qty),
                price=entry,
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
        direction = 1 if position.qty > 0 else -1
        hit_tp = snapshot.mid >= position.tp_price if direction > 0 else snapshot.mid <= position.tp_price
        hit_sl = snapshot.mid <= position.sl_price if direction > 0 else snapshot.mid >= position.sl_price
        if hit_tp or hit_sl:
            exit_price = snapshot.bid if direction > 0 else snapshot.ask
            pnl = (exit_price - position.entry_price) * position.qty
            self._place_order(
                snapshot.symbol,
                "Sell" if direction > 0 else "Buy",
                abs(position.qty),
                price=exit_price,
            )
            logging.info(
                "%s exit %s @ %.2f P&L %.2f",
                snapshot.symbol,
                "TP" if hit_tp else "SL",
                exit_price,
                pnl,
            )
            position.qty = 0
            position.entry_price = 0.0
            position.tp_price = 0.0
            position.sl_price = 0.0

    def _place_order(self, symbol: str, side: str, qty: float, price: Optional[float] = None) -> None:
        if not self.connected or not self.client:
            logging.warning("Order skipped (not connected): %s %s %.6f", side, symbol, qty)
            return
        normalized_qty = self._normalize_qty(symbol, qty, price)
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
        position_idx = self._resolve_position_idx(side)
        order_type = self.order_type_input.currentText()
        limit_price = None
        if order_type == "Limit":
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
        request = OrderRequest(
            symbol=symbol,
            side=side,
            qty=normalized_qty,
            position_idx=position_idx,
            order_type=order_type,
            limit_price=limit_price,
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
                retry=1,
            )
            self._dispatch_order(retry_request)
            return
        ret_code = response.get("retCode")
        if ret_code != 0:
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
                    retry=request.retry + 1,
                    remaining_qty=remaining,
                )
                self._dispatch_order(retry_request)
                return
        if request.order_type == "Limit":
            self.limit_shift_attempts.pop((request.symbol, request.side), None)

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
            normalized = min_qty
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
