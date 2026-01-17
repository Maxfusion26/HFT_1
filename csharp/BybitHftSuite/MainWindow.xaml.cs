using System;
using System.Windows;

namespace BybitHftSuite;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        AppendLog("UI initialized. Trading logic not yet ported.");
    }

    private void OnConnectClick(object sender, RoutedEventArgs e)
    {
        ConnectionStatus.Text = "Connection status: Connected (stub)";
        ConnectionStatus.Foreground = System.Windows.Media.Brushes.LightGreen;
        AppendLog("Connect requested. Network layer will be added in the next phase.");
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
