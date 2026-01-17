using System.Globalization;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BybitHftSuite;

public sealed class BybitRestClient
{
    private readonly HttpClient _httpClient;
    private readonly string _apiKey;
    private readonly string _apiSecret;
    private readonly string _baseUrl;

    public BybitRestClient(HttpClient httpClient, string apiKey, string apiSecret, string baseUrl)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _apiKey = apiKey ?? string.Empty;
        _apiSecret = apiSecret ?? string.Empty;
        _baseUrl = string.IsNullOrWhiteSpace(baseUrl) ? "https://api.bybit.com" : baseUrl.TrimEnd('/');
    }

    public Task<BybitResponse<TickerListResult>> FetchLinearTickersAsync(CancellationToken cancellationToken = default)
    {
        var uri = BuildUri("/v5/market/tickers", new Dictionary<string, string> { ["category"] = "linear" });
        return SendPublicAsync<TickerListResult>(uri, cancellationToken);
    }

    public Task<BybitResponse<PositionListResult>> FetchPositionsAsync(CancellationToken cancellationToken = default)
    {
        var uri = BuildUri("/v5/position/list", new Dictionary<string, string> { ["category"] = "linear" });
        return SendSignedGetAsync<PositionListResult>(uri, cancellationToken);
    }

    public Task<BybitResponse<WalletBalanceResult>> FetchWalletBalanceAsync(CancellationToken cancellationToken = default)
    {
        var uri = BuildUri("/v5/account/wallet-balance", new Dictionary<string, string> { ["accountType"] = "UNIFIED" });
        return SendSignedGetAsync<WalletBalanceResult>(uri, cancellationToken);
    }

    public Task<BybitResponse<ServerTimeResult>> FetchServerTimeAsync(CancellationToken cancellationToken = default)
    {
        var uri = BuildUri("/v5/market/time", null);
        return SendPublicAsync<ServerTimeResult>(uri, cancellationToken);
    }

    private async Task<BybitResponse<T>> SendPublicAsync<T>(Uri uri, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        using var response = await _httpClient.SendAsync(request, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        var payload = await JsonSerializer.DeserializeAsync<BybitResponse<T>>(stream, JsonOptions, cancellationToken)
            .ConfigureAwait(false);
        return payload ?? new BybitResponse<T>();
    }

    private async Task<BybitResponse<T>> SendSignedGetAsync<T>(Uri uri, CancellationToken cancellationToken)
    {
        var timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString(CultureInfo.InvariantCulture);
        const string recvWindow = "5000";
        var query = uri.Query.StartsWith("?") ? uri.Query[1..] : uri.Query;
        var signature = SignPayload(timestamp, recvWindow, query);

        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        request.Headers.Add("X-BAPI-API-KEY", _apiKey);
        request.Headers.Add("X-BAPI-SIGN", signature);
        request.Headers.Add("X-BAPI-SIGN-TYPE", "2");
        request.Headers.Add("X-BAPI-TIMESTAMP", timestamp);
        request.Headers.Add("X-BAPI-RECV-WINDOW", recvWindow);

        using var response = await _httpClient.SendAsync(request, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        var payload = await JsonSerializer.DeserializeAsync<BybitResponse<T>>(stream, JsonOptions, cancellationToken)
            .ConfigureAwait(false);
        return payload ?? new BybitResponse<T>();
    }

    private Uri BuildUri(string path, Dictionary<string, string>? queryParams)
    {
        var builder = new UriBuilder($"{_baseUrl}{path}");
        if (queryParams is { Count: > 0 })
        {
            var query = string.Join("&", queryParams
                .OrderBy(pair => pair.Key)
                .Select(pair => $"{pair.Key}={Uri.EscapeDataString(pair.Value)}"));
            builder.Query = query;
        }

        return builder.Uri;
    }

    private string SignPayload(string timestamp, string recvWindow, string payload)
    {
        var message = string.Concat(timestamp, _apiKey, recvWindow, payload);
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(_apiSecret));
        var hash = hmac.ComputeHash(Encoding.UTF8.GetBytes(message));
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };
}

public sealed class BybitResponse<T>
{
    public int RetCode { get; set; }
    public string RetMsg { get; set; } = string.Empty;
    public T? Result { get; set; }
    public long Time { get; set; }
}

public sealed class TickerListResult
{
    public List<TickerItem> List { get; set; } = new();
}

public sealed class TickerItem
{
    public string Symbol { get; set; } = string.Empty;
    public string LastPrice { get; set; } = string.Empty;
    public string PrevPrice24h { get; set; } = string.Empty;
    public string HighPrice24h { get; set; } = string.Empty;
    public string LowPrice24h { get; set; } = string.Empty;
    public string Bid1Price { get; set; } = string.Empty;
    public string Ask1Price { get; set; } = string.Empty;
    public string Bid1Size { get; set; } = string.Empty;
    public string Ask1Size { get; set; } = string.Empty;
    public string Turnover24h { get; set; } = string.Empty;
    public string Volume24h { get; set; } = string.Empty;
}

public sealed class PositionListResult
{
    public List<PositionItem> List { get; set; } = new();
}

public sealed class PositionItem
{
    public string Symbol { get; set; } = string.Empty;
    public string Side { get; set; } = string.Empty;
    public string Size { get; set; } = string.Empty;
    public string AvgPrice { get; set; } = string.Empty;
    public string UnrealisedPnl { get; set; } = string.Empty;
    public string PositionIdx { get; set; } = string.Empty;
}

public sealed class WalletBalanceResult
{
    public List<WalletAccount> List { get; set; } = new();
}

public sealed class WalletAccount
{
    public string AccountType { get; set; } = string.Empty;
    public List<WalletCoin> Coin { get; set; } = new();
}

public sealed class WalletCoin
{
    public string Coin { get; set; } = string.Empty;
    public string Equity { get; set; } = string.Empty;
    public string WalletBalance { get; set; } = string.Empty;
    public string AvailableToWithdraw { get; set; } = string.Empty;
}

public sealed class ServerTimeResult
{
    public string TimeSecond { get; set; } = string.Empty;
    public string TimeNano { get; set; } = string.Empty;
}
