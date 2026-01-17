using System;
using System.Linq;
using System.Net.Http;
using System.Windows;

namespace BybitHftSuite;

public partial class MainWindow : Window
{
    private readonly HttpClient _httpClient = new()
    {
        Timeout = TimeSpan.FromSeconds(15)
    };

    public MainWindow()
    {
        InitializeComponent();
        AppendLog("UI initialized. Trading logic not yet ported.");
    }

    private async void OnConnectClick(object sender, RoutedEventArgs e)
    {
        ConnectionStatus.Text = "Connection status: Connecting...";
        ConnectionStatus.Foreground = System.Windows.Media.Brushes.Gold;

        var apiKey = ApiKeyInput.Text?.Trim() ?? string.Empty;
        var apiSecret = ApiSecretInput.Text?.Trim() ?? string.Empty;
        var baseUrl = BaseUrlInput.Text?.Trim() ?? "https://api.bybit.com";
        var client = new BybitRestClient(_httpClient, apiKey, apiSecret, baseUrl);

        try
        {
            var serverTime = await client.FetchServerTimeAsync();
            AppendLog($"Server time: {serverTime.Result?.TimeSecond ?? "n/a"}");

            var tickers = await client.FetchLinearTickersAsync();
            AppendLog($"Tickers fetched: {tickers.Result?.List.Count ?? 0}");

            var positions = await client.FetchPositionsAsync();
            AppendLog($"Positions fetched: {positions.Result?.List.Count ?? 0}");

            var balance = await client.FetchWalletBalanceAsync();
            var account = balance.Result?.List.FirstOrDefault();
            var equity = account?.Coin.FirstOrDefault(coin => coin.Coin == "USDT")?.Equity;
            AppendLog($"Wallet balance fetched. USDT equity: {equity ?? "n/a"}");

            ConnectionStatus.Text = "Connection status: Connected";
            ConnectionStatus.Foreground = System.Windows.Media.Brushes.LightGreen;
        }
        catch (Exception ex)
        {
            ConnectionStatus.Text = "Connection status: Error";
            ConnectionStatus.Foreground = System.Windows.Media.Brushes.IndianRed;
            AppendLog($"Connection failed: {ex.Message}");
        }
    }

    private void OnDisconnectClick(object sender, RoutedEventArgs e)
    {
        ConnectionStatus.Text = "Connection status: Disconnected";
        ConnectionStatus.Foreground = System.Windows.Media.Brushes.Gold;
        AppendLog("Disconnected (stub).");
    }

    private void OnManualBuyClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Manual BUY requested (stub).");
    }

    private void OnManualSellClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Manual SELL requested (stub).");
    }

    private void OnFlattenClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Flatten requested (stub).");
    }

    private void OnRefreshSymbolsClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Refresh symbols requested (stub).");
    }

    private void OnClearLogsClick(object sender, RoutedEventArgs e)
    {
        LogOutput.Clear();
        AppendLog("Log cleared.");
    }

    private void OnExportLogsClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Export logs requested (stub).");
    }

    private void OnRunBacktestClick(object sender, RoutedEventArgs e)
    {
        AppendLog("Backtest requested (stub).");
    }

    private void AppendLog(string message)
    {
        var timestamp = DateTime.Now.ToString("HH:mm:ss");
        LogOutput.AppendText($"[{timestamp}] {message}{Environment.NewLine}");
        LogOutput.ScrollToEnd();
    }
}
