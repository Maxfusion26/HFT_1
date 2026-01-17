use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use clap::{Parser, Subcommand};
use hmac::{Hmac, Mac};
use log::{info, warn};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::Sha256;
use uuid::Uuid;

const DEFAULT_BASE_URL: &str = "https://api.bybit.com";

#[derive(Debug, Deserialize, Serialize, Default, Clone)]
struct Config {
    api_key: Option<String>,
    api_secret: Option<String>,
    base_url: Option<String>,
}

struct ConfigManager {
    path: PathBuf,
}

impl ConfigManager {
    fn new(path: PathBuf) -> Self {
        if let Some(parent) = path.parent() {
            if let Err(err) = fs::create_dir_all(parent) {
                warn!("Failed to create config dir: {err}");
            }
        }
        Self { path }
    }

    fn load(&self) -> Result<Config> {
        if !self.path.exists() {
            return Ok(Config::default());
        }
        let contents = fs::read_to_string(&self.path).context("Read config file")?;
        let config = serde_json::from_str(&contents).unwrap_or_default();
        Ok(config)
    }

    fn save(&self, config: &Config) -> Result<()> {
        let serialized = serde_json::to_string_pretty(config)?;
        fs::write(&self.path, serialized).context("Write config file")?;
        Ok(())
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct BybitResponse<T> {
    #[serde(rename = "retCode")]
    ret_code: i32,
    #[serde(rename = "retMsg")]
    ret_msg: Option<String>,
    result: Option<T>,
}

#[derive(Debug, Serialize, Deserialize)]
struct TickerResult {
    list: Vec<HashMap<String, serde_json::Value>>,
}

#[derive(Debug, Serialize, Deserialize)]
struct WalletResult {
    #[serde(flatten)]
    data: HashMap<String, serde_json::Value>,
}

#[derive(Clone)]
struct BybitRestClient {
    api_key: String,
    api_secret: String,
    base_url: String,
    client: Client,
}

impl BybitRestClient {
    fn new(api_key: String, api_secret: String, base_url: String) -> Result<Self> {
        let client = Client::builder().timeout(std::time::Duration::from_secs(10)).build()?;
        Ok(Self {
            api_key,
            api_secret,
            base_url: base_url.trim_end_matches('/').to_string(),
            client,
        })
    }

    fn sign(&self, timestamp: &str, recv_window: &str, payload: &str) -> Result<String> {
        let message = format!("{timestamp}{}{recv_window}{payload}", self.api_key);
        let mut mac = Hmac::<Sha256>::new_from_slice(self.api_secret.as_bytes())?;
        mac.update(message.as_bytes());
        Ok(hex::encode(mac.finalize().into_bytes()))
    }

    fn create_order(
        &self,
        symbol: &str,
        side: &str,
        qty: f64,
        order_type: &str,
        position_idx: i32,
        price: Option<f64>,
        time_in_force: &str,
        reduce_only: bool,
    ) -> Result<serde_json::Value> {
        let endpoint = "/v5/order/create";
        let timestamp = current_millis();
        let recv_window = "5000";
        let mut payload = json!({
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": format!("{qty:.6}"),
            "category": "linear",
            "timeInForce": time_in_force,
            "orderLinkId": Uuid::new_v4().to_string(),
            "positionIdx": position_idx,
        });
        if reduce_only {
            payload["reduceOnly"] = json!(true);
        }
        if order_type.eq_ignore_ascii_case("Limit") {
            let limit_price = price.ok_or_else(|| anyhow!("Limit orders require a price"))?;
            payload["price"] = json!(format!("{limit_price:.2}"));
        }
        let payload_str = serde_json::to_string(&payload)?;
        let signature = self.sign(&timestamp, recv_window, &payload_str)?;
        let response = self
            .client
            .post(format!("{}{}", self.base_url, endpoint))
            .header("X-BAPI-API-KEY", &self.api_key)
            .header("X-BAPI-SIGN", signature)
            .header("X-BAPI-SIGN-TYPE", "2")
            .header("X-BAPI-TIMESTAMP", timestamp)
            .header("X-BAPI-RECV-WINDOW", recv_window)
            .json(&payload)
            .send()?
            .error_for_status()?;
        Ok(response.json()?)
    }

    fn fetch_linear_tickers(&self) -> Result<Vec<HashMap<String, serde_json::Value>>> {
        let endpoint = "/v5/market/tickers";
        let response = self
            .client
            .get(format!("{}{}", self.base_url, endpoint))
            .query(&[("category", "linear")])
            .send()?
            .error_for_status()?;
        let payload: BybitResponse<TickerResult> = response.json()?;
        if payload.ret_code != 0 {
            return Ok(vec![]);
        }
        Ok(payload.result.map(|result| result.list).unwrap_or_default())
    }

    fn fetch_server_time(&self) -> Result<Option<f64>> {
        let endpoint = "/v5/market/time";
        let response = self
            .client
            .get(format!("{}{}", self.base_url, endpoint))
            .send()?
            .error_for_status()?;
        let payload: BybitResponse<HashMap<String, serde_json::Value>> = response.json()?;
        if payload.ret_code != 0 {
            return Ok(None);
        }
        let result = payload.result.unwrap_or_default();
        if let Some(value) = result.get("timeSecond") {
            if let Some(text) = value.as_str() {
                if let Ok(parsed) = text.parse::<f64>() {
                    return Ok(Some(parsed * 1000.0));
                }
            }
            if let Some(number) = value.as_f64() {
                return Ok(Some(number * 1000.0));
            }
        }
        if let Some(value) = result.get("timeNano") {
            if let Some(text) = value.as_str() {
                if let Ok(parsed) = text.parse::<f64>() {
                    return Ok(Some(parsed / 1_000_000.0));
                }
            }
            if let Some(number) = value.as_f64() {
                return Ok(Some(number / 1_000_000.0));
            }
        }
        Ok(None)
    }

    fn fetch_positions(&self) -> Result<Vec<HashMap<String, serde_json::Value>>> {
        let endpoint = "/v5/position/list";
        let timestamp = current_millis();
        let recv_window = "5000";
        let params = [("category", "linear")];
        let query = "category=linear";
        let signature = self.sign(&timestamp, recv_window, query)?;
        let response = self
            .client
            .get(format!("{}{}", self.base_url, endpoint))
            .query(&params)
            .header("X-BAPI-API-KEY", &self.api_key)
            .header("X-BAPI-SIGN", signature)
            .header("X-BAPI-SIGN-TYPE", "2")
            .header("X-BAPI-TIMESTAMP", timestamp)
            .header("X-BAPI-RECV-WINDOW", recv_window)
            .send()?
            .error_for_status()?;
        let payload: BybitResponse<HashMap<String, Vec<HashMap<String, serde_json::Value>>>> =
            response.json()?;
        if payload.ret_code != 0 {
            return Ok(vec![]);
        }
        Ok(payload
            .result
            .and_then(|mut result| result.remove("list"))
            .unwrap_or_default())
    }

    fn fetch_wallet_balance(&self) -> Result<HashMap<String, serde_json::Value>> {
        let endpoint = "/v5/account/wallet-balance";
        let timestamp = current_millis();
        let recv_window = "5000";
        let query = "accountType=UNIFIED";
        let signature = self.sign(&timestamp, recv_window, query)?;
        let response = self
            .client
            .get(format!("{}{}", self.base_url, endpoint))
            .query(&[("accountType", "UNIFIED")])
            .header("X-BAPI-API-KEY", &self.api_key)
            .header("X-BAPI-SIGN", signature)
            .header("X-BAPI-SIGN-TYPE", "2")
            .header("X-BAPI-TIMESTAMP", timestamp)
            .header("X-BAPI-RECV-WINDOW", recv_window)
            .send()?
            .error_for_status()?;
        let payload: BybitResponse<WalletResult> = response.json()?;
        if payload.ret_code != 0 {
            return Ok(HashMap::new());
        }
        Ok(payload.result.map(|r| r.data).unwrap_or_default())
    }
}

#[derive(Parser)]
#[command(name = "hft_bybit", version, about = "Bybit HFT CLI in Rust")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    Config {
        #[arg(long)]
        api_key: Option<String>,
        #[arg(long)]
        api_secret: Option<String>,
        #[arg(long)]
        base_url: Option<String>,
    },
    Tickers,
    ServerTime,
    Positions,
    Wallet,
    Order {
        symbol: String,
        side: String,
        qty: f64,
        #[arg(long, default_value = "Market")]
        order_type: String,
        #[arg(long, default_value_t = 0)]
        position_idx: i32,
        #[arg(long)]
        price: Option<f64>,
        #[arg(long, default_value = "GTC")]
        time_in_force: String,
        #[arg(long, default_value_t = false)]
        reduce_only: bool,
    },
}

fn main() -> Result<()> {
    env_logger::init();
    let cli = Cli::parse();
    let config_path = config_file_path();
    let manager = ConfigManager::new(config_path.clone());

    match cli.command {
        Commands::Config {
            api_key,
            api_secret,
            base_url,
        } => {
            let mut config = manager.load()?;
            if let Some(value) = api_key {
                config.api_key = Some(value);
            }
            if let Some(value) = api_secret {
                config.api_secret = Some(value);
            }
            if let Some(value) = base_url {
                config.base_url = Some(value);
            }
            manager.save(&config)?;
            println!("Updated config at {}", config_path.display());
            Ok(())
        }
        command => {
            let config = manager.load()?;
            let api_key = config
                .api_key
                .ok_or_else(|| anyhow!("Missing api_key in config"))?;
            let api_secret = config
                .api_secret
                .ok_or_else(|| anyhow!("Missing api_secret in config"))?;
            let base_url = config.base_url.unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
            let client = BybitRestClient::new(api_key, api_secret, base_url)?;

            match command {
                Commands::Tickers => {
                    let tickers = client.fetch_linear_tickers()?;
                    println!("Fetched {} tickers", tickers.len());
                    Ok(())
                }
                Commands::ServerTime => {
                    let server_time = client.fetch_server_time()?;
                    println!("Server time: {server_time:?}");
                    Ok(())
                }
                Commands::Positions => {
                    let positions = client.fetch_positions()?;
                    println!("Positions: {}", positions.len());
                    Ok(())
                }
                Commands::Wallet => {
                    let wallet = client.fetch_wallet_balance()?;
                    println!("Wallet fields: {}", wallet.len());
                    Ok(())
                }
                Commands::Order {
                    symbol,
                    side,
                    qty,
                    order_type,
                    position_idx,
                    price,
                    time_in_force,
                    reduce_only,
                } => {
                    let response = client.create_order(
                        &symbol,
                        &side,
                        qty,
                        &order_type,
                        position_idx,
                        price,
                        &time_in_force,
                        reduce_only,
                    )?;
                    info!("Order response: {response}");
                    println!("Order response: {response}");
                    Ok(())
                }
                Commands::Config { .. } => unreachable!(),
            }
        }
    }
}

fn config_file_path() -> PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| Path::new(".").to_path_buf());
    home.join(".hft_bybit").join("config.json")
}

fn current_millis() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    now.as_millis().to_string()
}
