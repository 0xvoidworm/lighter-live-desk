use anyhow::{Context, Result, bail};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{BufWriter, Write},
    net::{IpAddr, Ipv4Addr},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    net::TcpStream,
    time::{Instant, sleep, timeout, timeout_at},
};
use tokio_tungstenite::{client_async_tls, tungstenite::Message};

const URL: &str =
    "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms";
const PROVIDERS: [&str; 2] = [
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
];

#[derive(Clone)]
struct Log {
    file: Arc<Mutex<BufWriter<File>>>,
    start: Instant,
}
impl Log {
    fn write(&self, kind: &str, body: Value) -> Result<()> {
        let mut out = self
            .file
            .lock()
            .map_err(|_| anyhow::anyhow!("log lock poisoned"))?;
        serde_json::to_writer(
            &mut *out,
            &json!({"at": epoch(), "elapsed": self.start.elapsed().as_secs_f64(), "kind": kind, "body": body}),
        )?;
        out.write_all(b"\n")?;
        out.flush()?;
        Ok(())
    }
}
fn epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs_f64()
}
fn backoff(failures: u32) -> u64 {
    (1_u64 << failures.min(5)).min(30)
}
fn addresses(body: &Value) -> Result<Vec<Ipv4Addr>> {
    if body["Status"].as_u64() != Some(0) {
        bail!("DoH nonzero status");
    }
    let mut ips = BTreeSet::new();
    for answer in body["Answer"].as_array().context("missing DNS answers")? {
        if answer["type"] != 1 {
            continue;
        }
        let ip: Ipv4Addr = answer["data"].as_str().context("missing A data")?.parse()?;
        if ip.is_private()
            || ip.is_loopback()
            || ip.is_link_local()
            || ip.is_unspecified()
            || ip.is_multicast()
            || ip.is_broadcast()
        {
            bail!("non-public DNS address");
        }
        ips.insert(ip);
    }
    if ips.is_empty() {
        bail!("no IPv4 A answers");
    }
    Ok(ips.into_iter().collect())
}
async fn resolve(client: &reqwest::Client, log: &Log, id: u64) -> Result<Vec<Ipv4Addr>> {
    for provider in PROVIDERS {
        let start = Instant::now();
        let result: Result<(Vec<Ipv4Addr>, Value, Option<std::net::SocketAddr>)> = async {
            let response = client
                .get(format!("{provider}?name=fstream.binance.com&type=A"))
                .header("accept", "application/dns-json")
                .send()
                .await?
                .error_for_status()?;
            let peer = response.remote_addr();
            let body: Value = response.json().await?;
            Ok((addresses(&body)?, body, peer))
        }
        .await;
        match result {
            Ok((ips, body, peer)) => {
                log.write("dns", json!({"connection": id, "provider": provider, "ms": start.elapsed().as_secs_f64()*1000.0, "ips": ips, "answers": body["Answer"], "provider_peer": peer.map(|p|p.to_string())}))?;
                return Ok(ips);
            }
            Err(error) => log.write("dns_failure", json!({"connection": id, "provider": provider, "ms": start.elapsed().as_secs_f64()*1000.0, "error": format!("{error:#}")}))?,
        }
    }
    bail!("all DoH providers failed")
}

async fn connection(
    client: &reqwest::Client,
    log: &Log,
    id: u64,
    deadline: Instant,
) -> Result<bool> {
    let ips = resolve(client, log, id).await?;
    let mut tcp = None;
    for ip in ips {
        let started = Instant::now();
        match timeout(Duration::from_secs(5), TcpStream::connect((ip, 443))).await {
            Ok(Ok(stream)) => {
                log.write("tcp_connected", json!({"connection": id, "peer": stream.peer_addr()?.to_string(), "local": stream.local_addr()?.to_string(), "family": "IPv4", "ms": started.elapsed().as_secs_f64()*1000.0}))?;
                tcp = Some(stream); break;
            }
            result => log.write("tcp_failure", json!({"connection": id, "ip": ip.to_string(), "error": format!("{result:?}"), "ms": started.elapsed().as_secs_f64()*1000.0}))?,
        }
    }
    let tcp = tcp.context("no TCP connection")?;
    let started = Instant::now();
    let handshake = timeout(Duration::from_secs(10), client_async_tls(URL, tcp))
        .await
        .context("TLS/WS timeout")?;
    let (mut ws, response) = handshake.context("TLS/WS handshake")?;
    log.write("connected", json!({"connection": id, "status": response.status().as_u16(), "url": URL, "tls_ws_ms": started.elapsed().as_secs_f64()*1000.0}))?;
    let connected = Instant::now();
    let mut last = connected;
    let mut pong_deadline: Option<Instant> = None;
    let mut previous_u = None;
    let mut first = true;
    loop {
        let heartbeat = pong_deadline.unwrap_or(last + Duration::from_secs(20));
        tokio::select! {
            biased;
            _ = sleep(deadline.saturating_duration_since(Instant::now())) => {
                log.write("local_close", json!({"connection": id, "reason": "measurement_deadline", "age": connected.elapsed().as_secs_f64()}))?;
                let _ = timeout(Duration::from_millis(500), ws.close(None)).await;
                return Ok(false);
            }
            frame = ws.next() => {
                let uptime = connected.elapsed();
                let Some(frame) = frame else {
                    log.write("stream_ended", json!({"connection": id, "age": uptime.as_secs_f64(), "last_rx_age": last.elapsed().as_secs_f64(), "close_owner": "unknown_no_close_frame"}))?;
                    return Ok(uptime >= Duration::from_secs(30));
                };
                let frame = match frame {
                    Ok(frame) => frame,
                    Err(error) => {
                        log.write("receive_error", json!({"connection": id, "age": uptime.as_secs_f64(), "last_rx_age": last.elapsed().as_secs_f64(), "error": format!("{error:?}"), "close_owner": "unknown_no_close_frame"}))?;
                        return Ok(uptime >= Duration::from_secs(30));
                    }
                };
                // Like aiohttp, any frame resets client heartbeat. Never refresh a quote with this.
                last = Instant::now(); pong_deadline = None;
                match frame {
                    Message::Text(text) => {
                        let v: Value = serde_json::from_str(&text).context("invalid stream JSON")?;
                        let d = &v["data"];
                        let pu = d["pu"].as_u64(); let u = d["u"].as_u64();
                        let mismatch = d["e"] == "depthUpdate" && previous_u.is_some() && pu != previous_u;
                        log.write("message", json!({"connection": id, "stream": v["stream"], "event": d["e"], "symbol": d["s"], "st": d["st"], "E": d["E"], "T": d["T"], "U": d["U"], "u": d["u"], "pu": d["pu"], "bid": d["b"], "ask": d["a"], "bid_qty": d["B"], "ask_qty": d["A"], "sequence_gap": mismatch, "bytes": text.len(), "first_text_ms": first.then(||connected.elapsed().as_secs_f64()*1000.0)}))?;
                        if d["e"] == "depthUpdate" { previous_u = u; }
                        first = false;
                    }
                    Message::Ping(payload) => {
                        log.write("server_ping", json!({"connection": id, "bytes": payload.len()}))?;
                        // Tungstenite queues the matching pong; flush once, do not send a duplicate.
                        ws.flush().await?;
                        log.write("auto_pong_flushed", json!({"connection": id}))?;
                    }
                    Message::Pong(payload) => { log.write("pong", json!({"connection": id, "bytes": payload.len()}))?; }
                    Message::Close(frame) => {
                        log.write("remote_close", json!({"connection": id, "age": connected.elapsed().as_secs_f64(), "code": frame.as_ref().map(|f|u16::from(f.code)), "reason": frame.map(|f|f.reason.to_string())}))?;
                        let _ = ws.flush().await;
                        return Ok(connected.elapsed() >= Duration::from_secs(30));
                    }
                    _ => { log.write("other_frame", json!({"connection": id}))?; }
                }
            }
            _ = sleep(heartbeat.saturating_duration_since(Instant::now())) => {
                if pong_deadline.is_some() {
                    log.write("local_close", json!({"connection": id, "reason": "client_heartbeat_timeout", "age": connected.elapsed().as_secs_f64(), "last_rx_age": last.elapsed().as_secs_f64()}))?;
                    let _ = timeout(Duration::from_millis(500), ws.close(None)).await;
                    return Ok(connected.elapsed() >= Duration::from_secs(30));
                }
                ws.send(Message::Ping(Vec::new().into())).await?;
                log.write("client_ping", json!({"connection": id}))?;
                pong_deadline = Some(Instant::now() + Duration::from_secs(10));
            }
        }
    }
}

fn safe_status(v: &Value, pid: u64) -> Result<()> {
    if v["engine_pid"].as_u64() != Some(pid) {
        bail!("engine PID changed");
    }
    if v["allow_live_orders"] != false || v["strategy_session"]["status"] != "closed" {
        bail!("runtime permissions changed; stop diagnosis only");
    }
    Ok(())
}
fn sockets(pid: u64) -> Value {
    let inodes: BTreeSet<String> = fs::read_dir(format!("/proc/{pid}/fd"))
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| fs::read_link(entry.path()).ok())
        .filter_map(|p| p.to_str().map(str::to_owned))
        .filter_map(|s| {
            s.strip_prefix("socket:[")
                .and_then(|s| s.strip_suffix(']'))
                .map(str::to_owned)
        })
        .collect();
    let mut rows = Vec::new();
    for family in ["tcp", "tcp6"] {
        if let Ok(body) = fs::read_to_string(format!("/proc/{pid}/net/{family}")) {
            for line in body.lines().skip(1) {
                let fields: Vec<_> = line.split_whitespace().collect();
                if fields.len() > 9 && inodes.contains(fields[9]) {
                    rows.push(json!({"family": family, "local_hex": fields[1], "remote_hex": fields[2], "state": fields[3], "inode": fields[9]}));
                }
            }
        }
    }
    json!(rows)
}
async fn observe(client: reqwest::Client, log: Log, pid: u64, deadline: Instant) -> Result<()> {
    while Instant::now() < deadline {
        let started = Instant::now();
        let fetched = timeout_at(deadline, async {
            Ok::<Value, anyhow::Error>(
                client
                    .get("http://127.0.0.1:8765/api/status")
                    .send()
                    .await?
                    .error_for_status()?
                    .json()
                    .await?,
            )
        })
        .await;
        let v = match fetched {
            Ok(result) => result?,
            Err(_) => break,
        };
        safe_status(&v, pid)?;
        let keys = [
            "engine_pid",
            "state",
            "allow_live_orders",
            "strategy_session",
            "account",
            "reconciled",
            "account_state_diagnostic",
            "history_audit",
            "net_cash_audit",
            "pending_orders",
            "quotes",
            "feed_channels",
            "market_processing",
            "coverage",
            "diagnostic_observation",
            "decision",
            "decision_inputs",
            "binance_transport_trace",
            "fx_diagnostic",
            "queue_backlog",
            "event_loop_lag_ms",
            "request_budget",
        ];
        let selected: serde_json::Map<String, Value> = keys
            .iter()
            .filter_map(|k| v.get(k).map(|x| (k.to_string(), x.clone())))
            .collect();
        log.write("app_snapshot", json!({"status": selected, "sockets": sockets(pid), "local_status_ms": started.elapsed().as_secs_f64()*1000.0}))?;
        sleep(
            Duration::from_secs(1)
                .saturating_sub(started.elapsed())
                .min(deadline.saturating_duration_since(Instant::now())),
        )
        .await;
    }
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        bail!("usage: binance-ws-compare OUTPUT ENGINE_PID SECONDS(1..900)");
    }
    let output = PathBuf::from(&args[1]);
    let pid: u64 = args[2].parse()?;
    let seconds: u64 = args[3].parse()?;
    if !(1..=900).contains(&seconds) {
        bail!("duration must be 1..900 seconds");
    }
    fs::create_dir_all(&output)?;
    let client = reqwest::Client::builder()
        .no_proxy()
        .local_address(IpAddr::V4(Ipv4Addr::UNSPECIFIED))
        .timeout(Duration::from_secs(3))
        .build()?;
    let initial: Value = client
        .get("http://127.0.0.1:8765/api/status")
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    safe_status(&initial, pid)?;
    let started = Instant::now();
    let deadline = started + Duration::from_secs(seconds);
    let marker = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("started.json"))
        .context("one-run marker already exists or output unavailable")?;
    serde_json::to_writer_pretty(
        marker,
        &json!({"at": epoch(), "seconds": seconds, "engine_pid": pid, "url": URL, "resolver": "DoH A Cloudflare then Google", "proxy": "disabled", "family": "IPv4", "client": "Rust tokio-tungstenite 0.30.0 native-root TLS; no engine/signer/key; no REST book bootstrap", "heartbeat": "idle 20s/client pong 10s, auto server pong", "measurement_includes": "DNS, connect, handshake, backoff"}),
    )?;
    let log = Log {
        file: Arc::new(Mutex::new(BufWriter::new(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output.join("trace.jsonl"))?,
        ))),
        start: started,
    };
    let observation = observe(client.clone(), log.clone(), pid, deadline);
    let minimal = async {
        let mut id = 0;
        let mut failures = 0;
        while Instant::now() < deadline {
            id += 1;
            log.write("connecting", json!({"connection": id, "url": URL}))?;
            let result = timeout_at(deadline, connection(&client, &log, id, deadline)).await;
            if Instant::now() >= deadline {
                log.write(
                    "measurement_deadline",
                    json!({"connection": id, "local_cancel": true}),
                )?;
                break;
            }
            match result {
                Ok(Ok(stable)) => {
                    if stable {
                        failures = 0;
                    }
                }
                Ok(Err(error)) => {
                    log.write(
                        "connection_failure",
                        json!({"connection": id, "error": format!("{error:#}")}),
                    )?;
                }
                Err(error) => {
                    log.write(
                        "connection_failure",
                        json!({"connection": id, "error": error.to_string()}),
                    )?;
                }
            }
            failures += 1;
            let delay = backoff(failures);
            log.write(
                "backoff",
                json!({"connection": id, "seconds": delay, "consecutive_failures": failures}),
            )?;
            sleep(
                Duration::from_secs(delay).min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
        Ok::<_, anyhow::Error>(())
    };
    // try_join aborts the minimal observer if runtime permission/PID checks fail.
    let result = tokio::try_join!(observation, minimal);
    serde_json::to_writer_pretty(
        File::create(output.join("finished.json"))?,
        &json!({"at": epoch(), "elapsed": started.elapsed().as_secs_f64(), "result": result.as_ref().err().map(|e|format!("{e:#}")), "new_orders": "no transaction transport or trading dependencies"}),
    )?;
    result?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reject_nonpublic_dns_and_deduplicate() {
        assert!(addresses(&json!({"Status":0,"Answer":[{"type":1,"data":"127.0.0.1"}]})).is_err());
        assert!(addresses(&json!({"Status":0,"Answer":[{"type":1,"data":"10.0.0.8"}]})).is_err());
        assert_eq!(addresses(&json!({"Status":0,"Answer":[{"type":1,"data":"8.8.8.8"},{"type":1,"data":"8.8.8.8"}]})).unwrap().len(),1);
    }
    #[test]
    fn observing_cannot_load_or_reuse_live_permission() {
        assert!(safe_status(&json!({"engine_pid":7,"allow_live_orders":false,"strategy_session":{"status":"closed"}}),7).is_ok());
        for v in [
            json!({"engine_pid":8,"allow_live_orders":false,"strategy_session":{"status":"closed"}}),
            json!({"engine_pid":7,"allow_live_orders":true,"strategy_session":{"status":"closed"}}),
            json!({"engine_pid":7,"allow_live_orders":false,"strategy_session":{"status":"authorized"}}),
        ] {
            assert!(safe_status(&v, 7).is_err());
        }
    }
    #[test]
    fn capped_backoff() {
        assert_eq!(
            (1..8).map(backoff).collect::<Vec<_>>(),
            vec![2, 4, 8, 16, 30, 30, 30]
        );
    }

    #[tokio::test]
    async fn server_ping_gets_one_matching_auto_pong() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
            ws.send(Message::Ping(b"binance-heartbeat".to_vec().into()))
                .await
                .unwrap();
            let received = timeout(Duration::from_secs(1), ws.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            assert_eq!(
                received,
                Message::Pong(b"binance-heartbeat".to_vec().into())
            );
            assert!(
                timeout(Duration::from_millis(50), ws.next()).await.is_err(),
                "duplicate pong"
            );
        });
        let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        assert!(matches!(
            ws.next().await.unwrap().unwrap(),
            Message::Ping(_)
        ));
        ws.flush().await.unwrap();
        server.await.unwrap();
    }
}
