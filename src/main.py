import json
import logging
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PyQt6 import QtCore, QtGui, QtWidgets


CONFIG_DIR = Path.home() / ".hft_bybit"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "trading.log"


@dataclass
class SymbolMetrics:
    symbol: str
    volume_usd: float
    volatility: float
    imbalance: float

    @property
    def score(self) -> float:
        return (self.volume_usd * 0.6) + (self.volatility * 0.3) + (self.imbalance * 0.1)


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


class HFTStrategy:
    def __init__(self, maker_fee: float = 0.0002, taker_fee: float = 0.001) -> None:
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

    def estimate_roundtrip_fee(self, notional: float) -> float:
        return notional * (self.taker_fee + self.taker_fee)


class BacktestEngine(QtCore.QObject):
    finished = QtCore.pyqtSignal(list, list)

    def __init__(self, strategy: HFTStrategy, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.strategy = strategy

    def run_dummy(self, steps: int = 100) -> None:
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
            pnl -= self.strategy.estimate_roundtrip_fee(1000)
            timestamps.append(idx)
            pnl_series.append(pnl)
        self.finished.emit(timestamps, pnl_series)


class TradingApp(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HFT Bybit Futures - Adaptive Market Maker")
        self.resize(1100, 720)
        self.config = ConfigManager(CONFIG_FILE)
        self.strategy = HFTStrategy()
        self.backtest_engine = BacktestEngine(self.strategy)
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

        creds_group = QtWidgets.QGroupBox("API Keys")
        creds_layout = QtWidgets.QGridLayout(creds_group)

        self.api_key_input = QtWidgets.QLineEdit()
        self.api_secret_input = QtWidgets.QLineEdit()
        self.api_secret_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.auto_save_checkbox = QtWidgets.QCheckBox("Auto-save")

        creds_layout.addWidget(QtWidgets.QLabel("API Key"), 0, 0)
        creds_layout.addWidget(self.api_key_input, 0, 1)
        creds_layout.addWidget(QtWidgets.QLabel("API Secret"), 1, 0)
        creds_layout.addWidget(self.api_secret_input, 1, 1)
        creds_layout.addWidget(self.auto_save_checkbox, 2, 0, 1, 2)

        controls_group = QtWidgets.QGroupBox("Trading Controls")
        controls_layout = QtWidgets.QGridLayout(controls_group)

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

        self.spread_multiplier_input = QtWidgets.QDoubleSpinBox()
        self.spread_multiplier_input.setRange(1.0, 5.0)
        self.spread_multiplier_input.setValue(1.2)

        controls_layout.addWidget(self.connect_button, 0, 0)
        controls_layout.addWidget(self.disconnect_button, 0, 1)
        controls_layout.addWidget(self.auto_trading_toggle, 0, 2)
        controls_layout.addWidget(QtWidgets.QLabel("Symbol"), 1, 0)
        controls_layout.addWidget(self.symbol_input, 1, 1)
        controls_layout.addWidget(QtWidgets.QLabel("Position size"), 1, 2)
        controls_layout.addWidget(self.position_size_input, 1, 3)
        controls_layout.addWidget(QtWidgets.QLabel("TP"), 2, 0)
        controls_layout.addWidget(self.tp_input, 2, 1)
        controls_layout.addWidget(QtWidgets.QLabel("SL"), 2, 2)
        controls_layout.addWidget(self.sl_input, 2, 3)
        controls_layout.addWidget(self.maker_mode_checkbox, 3, 0)
        controls_layout.addWidget(QtWidgets.QLabel("Skew"), 3, 1)
        controls_layout.addWidget(self.risk_skew_input, 3, 2)
        controls_layout.addWidget(QtWidgets.QLabel("Spread"), 3, 3)
        controls_layout.addWidget(self.spread_multiplier_input, 3, 4)

        self.symbol_table = QtWidgets.QTableWidget(0, 5)
        self.symbol_table.setHorizontalHeaderLabels(
            ["Symbol", "Volume $", "Volatility", "Imbalance", "Score"]
        )
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        self.auto_select_button = QtWidgets.QPushButton("Auto-select top symbols")

        self.log_output = QtWidgets.QTextEdit()
        self.log_output.setReadOnly(True)

        layout.addWidget(creds_group)
        layout.addWidget(controls_group)
        layout.addWidget(self.symbol_table)
        layout.addWidget(self.auto_select_button)
        layout.addWidget(QtWidgets.QLabel("Live Log"))
        layout.addWidget(self.log_output)

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
            QMainWindow { background: #0f1115; }
            QLabel, QCheckBox { color: #e4e6eb; font-size: 13px; }
            QGroupBox { border: 1px solid #2b2f36; border-radius: 8px; margin-top: 12px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9aa4b2; }
            QPushButton { background: #1f6feb; color: white; border-radius: 8px; padding: 8px 16px; }
            QPushButton:checked { background: #3fb950; }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox { background: #151922; color: #e4e6eb; border: 1px solid #2b2f36; padding: 6px; border-radius: 6px; }
            QTextEdit { background: #11151d; color: #c9d1d9; border: 1px solid #2b2f36; border-radius: 8px; }
            QTableWidget { background: #11151d; color: #c9d1d9; border: 1px solid #2b2f36; }
            QHeaderView::section { background: #151922; color: #9aa4b2; }
            """
        )

    def _load_config(self) -> None:
        data = self.config.load()
        self.api_key_input.setText(data.get("api_key", ""))
        self.api_secret_input.setText(data.get("api_secret", ""))
        self.auto_save_checkbox.setChecked(data.get("auto_save", False))

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
        self.auto_select_button.clicked.connect(self._refresh_symbol_table)
        self.backtest_button.clicked.connect(self._run_backtest)
        self.backtest_engine.finished.connect(self._update_backtest_results)

    def _persist_config(self) -> None:
        if not self.auto_save_checkbox.isChecked():
            return
        data = {
            "api_key": self.api_key_input.text().strip(),
            "api_secret": self.api_secret_input.text().strip(),
            "auto_save": self.auto_save_checkbox.isChecked(),
        }
        self.config.save(data)

    def _connect(self) -> None:
        logging.info("Connecting to Bybit futures API (stub)")

    def _disconnect(self) -> None:
        logging.info("Disconnecting from Bybit futures API (stub)")

    def _toggle_auto_trading(self, enabled: bool) -> None:
        if enabled:
            self.auto_trading_toggle.setText("Stop Auto Trading")
            logging.info("Auto trading enabled")
            self._log_strategy_overview()
        else:
            self.auto_trading_toggle.setText("Start Auto Trading")
            logging.info("Auto trading disabled")

    def _log_strategy_overview(self) -> None:
        logging.info(
            "Strategy: micro-price market making with imbalance skew; taker fee=0.10%% each side."
        )

    def _refresh_symbol_table(self) -> None:
        metrics = self._generate_symbol_metrics()
        metrics.sort(key=lambda item: item.score, reverse=True)

        self.symbol_table.setRowCount(len(metrics))
        for row, metric in enumerate(metrics):
            self.symbol_table.setItem(row, 0, QtWidgets.QTableWidgetItem(metric.symbol))
            self.symbol_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{metric.volume_usd:,.0f}"))
            self.symbol_table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{metric.volatility:.3f}"))
            self.symbol_table.setItem(row, 3, QtWidgets.QTableWidgetItem(f"{metric.imbalance:.3f}"))
            self.symbol_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{metric.score:,.2f}"))

    def _generate_symbol_metrics(self) -> List[SymbolMetrics]:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        metrics = []
        for symbol in symbols:
            metrics.append(
                SymbolMetrics(
                    symbol=symbol,
                    volume_usd=random.uniform(10_000_000, 200_000_000),
                    volatility=random.uniform(0.5, 3.0),
                    imbalance=random.uniform(-1.0, 1.0),
                )
            )
        return metrics

    def _run_backtest(self) -> None:
        steps = self.backtest_steps_input.value()
        logging.info("Running backtest for %s steps", steps)
        self.backtest_engine.run_dummy(steps)

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


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("HFT Lab")
    app.setApplicationName("Bybit HFT Suite")
    window = TradingApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
