//! End-to-end tests for the HTTP adapter, over a real TCP listener.
//!
//! # Two far ends, on purpose
//!
//! Most tests drive a **fake engine**: a socket server that speaks the framing and
//! answers from a script. That is not a shortcut, it is the only way to test the
//! adapter's own mapping on this platform. A real allow or deny needs an installed
//! policy, installing one needs the engine's control plane, and that plane refuses
//! every caller on a non-Linux host (peer-credential attestation is `SO_PEERCRED` or
//! nothing). A fake far end makes 200 / 403 / 502 / 504 deterministic and
//! platform-independent, so the *adapter's* contract is always under test.
//!
//! One test drives the **real engine**, because agreement with a fake proves only
//! that the fake and the adapter share an author. The real engine is what proves the
//! adapter speaks the published protocol.
//!
//! Writing a second implementation of the framing inline is also a small check on
//! the claim that the protocol is small enough to reimplement from its description —
//! about twenty lines, here, with no code generation and no schema.
//!
//! # What is asserted that a reader might not expect
//!
//! - a **deny is a 403**, not a 200 with `allowed: false` — and every failure is a
//!   non-2xx, so a caller that proceeds on 2xx is fail-closed by construction;
//! - an **unreachable engine is never an allow**;
//! - the two OpenAPI documents are **disjoint**, so a client generated from the data
//!   document cannot express a policy edit;
//! - the token layer covers everything under `/v1` and nothing else — `/docs` is
//!   reachable without a token, and `/v1/status` is not.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use axum::Router;
use dogwood_http::engine::Engine;
use dogwood_server::{ControlAllowlist, Paths};
use serde_json::{Value, json};

// ─── A fake engine ───────────────────────────────────────────────────

/// What the fake far end does with the one frame it receives.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Reply {
    Allow,
    Deny,
    Recorded,
    /// The engine's own `Error` variant — a request it could not serve.
    EngineError,
    /// Read the frame, then hold the connection open and answer nothing, so the
    /// adapter's own deadline is what ends the call.
    Silent,
}

/// Bind both of the engine's sockets under `dir` and answer every request with
/// `reply`.
fn fake_engine(dir: &Path, reply: Reply) {
    let data = dir.join("run").join("dogwood.sock");
    let control = dir.join("control.sock");
    for path in [&data, &control] {
        let _ = std::fs::remove_file(path);
    }
    let data_listener = UnixListener::bind(&data).expect("bind the fake data socket");
    let control_listener = UnixListener::bind(&control).expect("bind the fake control socket");
    thread::spawn(move || accept_forever(data_listener, reply));
    thread::spawn(move || accept_forever(control_listener, reply));
}

fn accept_forever(listener: UnixListener, reply: Reply) {
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        thread::spawn(move || answer_one_frame(stream, reply));
    }
}

fn answer_one_frame(mut stream: UnixStream, reply: Reply) {
    // Read one frame: 4-byte big-endian length, then that many bytes of JSON.
    let mut header = [0u8; 4];
    if stream.read_exact(&mut header).is_err() {
        return;
    }
    let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
    if stream.read_exact(&mut body).is_err() {
        return;
    }
    // The request is deliberately not inspected: the subject here is how the
    // adapter maps a *reply*, and a fake that parsed requests would be a second
    // engine to keep correct.

    if reply == Reply::Silent {
        // Long enough that no reasonable deadline in these tests outlasts it, and
        // bounded so the thread ends even if the test panics.
        thread::sleep(Duration::from_secs(30));
        return;
    }

    let payload = match reply {
        Reply::Allow => json!({
            "status": "decision",
            "recorded_at_nanos": 111,
            "allowed": true,
            "reason": ["permit-1"],
        }),
        Reply::Deny => json!({
            "status": "decision",
            "recorded_at_nanos": 222,
            "allowed": false,
            "reason": ["forbid-1"],
        }),
        Reply::Recorded => json!({"status": "recorded", "recorded_at_nanos": 333}),
        Reply::EngineError => {
            json!({"status": "error", "message": "no policy set installed"})
        }
        Reply::Silent => unreachable!("handled above"),
    };

    let bytes = serde_json::to_vec(&payload).expect("the scripted reply serializes");
    let _ = stream.write_all(&(bytes.len() as u32).to_be_bytes());
    let _ = stream.write_all(&bytes);
    let _ = stream.flush();
}

// ─── Harnesses ───────────────────────────────────────────────────────

static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// A fresh store directory per test, so no two tests share durable state.
fn temp_store(tag: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("dogwood-http-{}-{tag}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("run")).expect("create the store directory");
    dir
}

/// Run a router on an ephemeral loopback port and return where it landed.
fn spawn_adapter(router: Router) -> SocketAddr {
    let (address_tx, address_rx) = mpsc::channel();
    thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("build a runtime");
        runtime.block_on(async move {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("bind an ephemeral port");
            address_tx
                .send(listener.local_addr().expect("read the bound address"))
                .expect("report the bound address");
            // No graceful shutdown: the thread is a fixture for the length of the
            // test process, and dropping the listener at process exit is fine.
            axum::serve(listener, router).await.expect("serve");
        });
    });
    address_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("the adapter binds within ten seconds")
}

/// An adapter over a fake engine that always answers `reply`.
fn adapter_over_fake(tag: &str, reply: Reply) -> SocketAddr {
    let dir = temp_store(tag);
    fake_engine(&dir, reply);
    let engine = Engine::from_store_dir(&dir, Duration::from_secs(5));
    spawn_adapter(dogwood_http::data::router(engine, None))
}

/// Start the real engine and wait for its sockets, returning its paths.
fn start_real_engine(tag: &str) -> PathBuf {
    let paths = Paths::new(temp_store(tag));
    let server = dogwood_server::Server::open(paths.clone(), ControlAllowlist::own_uid(), 10_000)
        .expect("the engine opens its store");
    thread::spawn(move || {
        let _ = server.serve();
    });
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while !paths.data_socket().exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "the engine never bound {}",
            paths.data_socket().display()
        );
        thread::sleep(Duration::from_millis(20));
    }
    paths.dir
}

// ─── A minimal HTTP client ───────────────────────────────────────────

struct HttpResponse {
    status: u16,
    headers: Vec<(String, String)>,
    body: String,
}

impl HttpResponse {
    fn json(&self) -> Value {
        serde_json::from_str(&self.body)
            .unwrap_or_else(|e| panic!("body was not JSON ({e}): {}", self.body))
    }

    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
    }
}

fn get(address: SocketAddr, path: &str) -> HttpResponse {
    call(address, "GET", path, None, &[])
}

fn call(
    address: SocketAddr,
    method: &str,
    path: &str,
    body: Option<&str>,
    headers: &[(&str, &str)],
) -> HttpResponse {
    let mut stream = TcpStream::connect(address).expect("connect to the adapter");
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .expect("set a read timeout");
    let body = body.unwrap_or("");

    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\
         Content-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    request.push_str(body);

    stream
        .write_all(request.as_bytes())
        .expect("write the request");
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).expect("read the response");
    parse(&raw)
}

fn parse(raw: &[u8]) -> HttpResponse {
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .expect("a header terminator");
    let head = String::from_utf8_lossy(&raw[..split]).to_string();
    let rest = &raw[split + 4..];

    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .expect("a status line");
    let headers: Vec<(String, String)> = lines
        .filter_map(|line| {
            line.split_once(':')
                .map(|(name, value)| (name.trim().to_ascii_lowercase(), value.trim().to_string()))
        })
        .collect();

    let chunked = headers.iter().any(|(name, value)| {
        name == "transfer-encoding" && value.to_ascii_lowercase().contains("chunked")
    });
    let body = if chunked {
        decode_chunked(rest)
    } else {
        rest.to_vec()
    };

    HttpResponse {
        status,
        headers,
        body: String::from_utf8_lossy(&body).to_string(),
    }
}

fn decode_chunked(mut input: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    loop {
        let Some(end) = input.windows(2).position(|w| w == b"\r\n") else {
            return out;
        };
        let size_line = String::from_utf8_lossy(&input[..end]);
        let size = usize::from_str_radix(size_line.split(';').next().unwrap_or("0").trim(), 16)
            .unwrap_or(0);
        input = &input[end + 2..];
        if size == 0 {
            return out;
        }
        out.extend_from_slice(&input[..size.min(input.len())]);
        input = &input[(size + 2).min(input.len())..];
    }
}

// ─── The data plane's verdict mapping ────────────────────────────────

const EVENT: &str =
    r#"{"action":"A::Action::Read","kind":"request","principal":"A::User::\"alice\""}"#;

#[test]
fn an_allowed_action_is_a_200() {
    let address = adapter_over_fake("allow", Reply::Allow);
    let response = call(
        address,
        "POST",
        "/v1/events",
        Some(EVENT),
        &[("Content-Type", "application/json")],
    );
    assert_eq!(response.status, 200, "body: {}", response.body);
    let body = response.json();
    assert_eq!(body["status"], "decision");
    assert_eq!(body["allowed"], true);
    assert_eq!(body["recorded_at_nanos"], 111);
}

/// The load-bearing one. A deny must not be a 2xx, so that a caller which only
/// looks at the status code is fail-closed.
#[test]
fn a_denied_action_is_a_403_and_never_a_2xx() {
    let address = adapter_over_fake("deny", Reply::Deny);
    let response = call(
        address,
        "POST",
        "/v1/events",
        Some(EVENT),
        &[("Content-Type", "application/json")],
    );
    assert_eq!(response.status, 403, "body: {}", response.body);
    assert!(
        !(200..300).contains(&response.status),
        "a deny must not be a 2xx: a caller that proceeds on 2xx would be defeated"
    );
    let body = response.json();
    assert_eq!(body["allowed"], false);
    assert_eq!(body["reason"][0], "forbid-1");
}

#[test]
fn a_history_event_is_acknowledged_with_a_200_and_no_verdict() {
    let address = adapter_over_fake("recorded", Reply::Recorded);
    let response = call(
        address,
        "POST",
        "/v1/events",
        Some(r#"{"action":"A::Action::Read","kind":"response"}"#),
        &[],
    );
    assert_eq!(response.status, 200, "body: {}", response.body);
    let body = response.json();
    assert_eq!(body["status"], "recorded");
    assert_eq!(body["recorded_at_nanos"], 333);
    assert!(body.get("allowed").is_none(), "no verdict applies");
}

/// An engine-reported error is not a verdict, and must land in the "refuse" range
/// rather than being visible as a 2xx.
#[test]
fn an_engine_error_is_a_502_and_not_a_verdict() {
    let address = adapter_over_fake("engine-error", Reply::EngineError);
    let response = call(address, "POST", "/v1/events", Some(EVENT), &[]);
    assert_eq!(response.status, 502, "body: {}", response.body);
    let body = response.json();
    assert_eq!(body["source"], "engine");
    assert!(
        body["error"].as_str().unwrap().contains("no policy"),
        "the engine's own words travel: {body}"
    );
}

/// The deadline. Ambiguity is the point: the event may or may not have been
/// appended, so the answer must be "no verdict", not "allowed".
#[test]
fn an_engine_that_does_not_answer_is_a_504() {
    let dir = temp_store("timeout");
    fake_engine(&dir, Reply::Silent);
    let engine = Engine::from_store_dir(&dir, Duration::from_millis(250));
    let address = spawn_adapter(dogwood_http::data::router(engine, None));

    let response = call(address, "POST", "/v1/events", Some(EVENT), &[]);
    assert_eq!(response.status, 504, "body: {}", response.body);
    assert!(!(200..300).contains(&response.status));
}

/// The one that matters most: with no engine at all, the adapter must not invent a
/// verdict. A policy engine that answers "allow" when it cannot be reached is worse
/// than no policy engine.
#[test]
fn an_unreachable_engine_never_produces_an_allow() {
    // A directory whose sockets were never bound.
    let dir = temp_store("no-engine");
    let engine = Engine::from_store_dir(&dir, Duration::from_millis(500));
    let address = spawn_adapter(dogwood_http::data::router(engine, None));

    let response = call(address, "POST", "/v1/events", Some(EVENT), &[]);
    assert_eq!(response.status, 502, "body: {}", response.body);
    assert!(!(200..300).contains(&response.status), "must not be a 2xx");
    assert_eq!(response.json()["source"], "transport");

    // ...and the probe agrees, rather than reporting healthy.
    assert_eq!(get(address, "/v1/ping").status, 502);
    assert_eq!(get(address, "/healthz").status, 502);
}

// ─── Requests the adapter refuses itself ─────────────────────────────

/// A misspelled field must be a visible 400 rather than a silent deny — and it must
/// be refused *before* the engine is consulted, which is what the Allow-configured
/// fake proves: an engine that would have allowed never gets to.
#[test]
fn an_unknown_event_field_is_refused_before_the_engine_can_allow_it() {
    let address = adapter_over_fake("unknown-field", Reply::Allow);
    let response = call(
        address,
        "POST",
        "/v1/events",
        Some(r#"{"action":"A::Action::Read","kind":"request","ts":1700000000}"#),
        &[],
    );
    assert_eq!(response.status, 400, "body: {}", response.body);
    let body = response.json();
    assert_eq!(body["source"], "request");
    assert!(
        body["error"].as_str().unwrap().contains("ts"),
        "the message must name the offending field: {body}"
    );
}

#[test]
fn a_body_that_is_not_json_is_a_400() {
    let address = adapter_over_fake("not-json", Reply::Allow);
    let response = call(address, "POST", "/v1/events", Some("not json at all"), &[]);
    assert_eq!(response.status, 400, "body: {}", response.body);
}

#[test]
fn ping_reports_the_engines_version() {
    let address = adapter_over_fake("ping", Reply::Allow);
    // The fake answers a *decision* to a ping, which the adapter must refuse to
    // interpret: a reply it did not expect is "no answer", not a healthy server.
    assert_eq!(get(address, "/v1/ping").status, 502);

    let dir = start_real_engine("ping-real");
    let engine = Engine::from_store_dir(&dir, Duration::from_secs(5));
    let address = spawn_adapter(dogwood_http::data::router(engine, None));
    let response = get(address, "/v1/ping");
    assert_eq!(response.status, 200, "body: {}", response.body);
    assert!(response.json()["version"].is_string());
    assert_eq!(get(address, "/healthz").status, 200);
}

// ─── Authentication ──────────────────────────────────────────────────

fn control_adapter(tag: &str, token: &str) -> SocketAddr {
    let dir = temp_store(tag);
    fake_engine(&dir, Reply::Allow);
    let engine = Engine::from_store_dir(&dir, Duration::from_secs(5));
    spawn_adapter(dogwood_http::control::router(engine, token.into()))
}

#[test]
fn every_control_route_requires_the_bearer_token() {
    let address = control_adapter("control-auth", "s3cret");

    for path in ["/v1/status", "/v1/policies", "/v1/policy", "/v1/schema"] {
        let missing = get(address, path);
        assert_eq!(
            missing.status, 401,
            "{path} without a token: {}",
            missing.body
        );
        assert_eq!(
            missing.header("www-authenticate"),
            Some("Bearer"),
            "a 401 must say how to authenticate"
        );

        let wrong = call(
            address,
            "GET",
            path,
            None,
            &[("Authorization", "Bearer not-the-token")],
        );
        assert_eq!(wrong.status, 401, "{path} with a wrong token");

        let wrong_scheme = call(
            address,
            "GET",
            path,
            None,
            &[("Authorization", "Basic czNjcmV0")],
        );
        assert_eq!(wrong_scheme.status, 401, "{path} with a foreign scheme");
    }
}

/// The token layer is not a blanket over the port: the docs and the probe must stay
/// reachable, or a browser could never render the UI and an orchestrator could not
/// probe without the secret.
#[test]
fn the_token_layer_covers_the_api_and_not_the_docs() {
    let address = control_adapter("control-public", "s3cret");

    assert_eq!(
        get(address, "/healthz").status,
        502,
        "the fake answers a ping oddly"
    );
    assert_ne!(get(address, "/openapi.json").status, 401);
    let docs = get(address, "/docs");
    assert!(
        docs.status == 200 || (300..400).contains(&docs.status),
        "/docs must not be behind the token: {}",
        docs.status
    );
}

#[test]
fn the_right_token_gets_past_the_layer() {
    let address = control_adapter("control-ok", "s3cret");
    let response = call(
        address,
        "GET",
        "/v1/status",
        None,
        &[("Authorization", "Bearer s3cret")],
    );
    // The fake replies with a data-plane body on the control socket, so the adapter
    // cannot decode it — that is a 502, and the point is only that it is *not* a
    // 401: the token gate let the request through.
    assert_ne!(response.status, 401, "body: {}", response.body);
    assert_eq!(response.status, 502, "body: {}", response.body);
}

#[test]
fn the_data_plane_can_require_a_token_too() {
    let dir = temp_store("data-token");
    fake_engine(&dir, Reply::Allow);
    let engine = Engine::from_store_dir(&dir, Duration::from_secs(5));
    let address = spawn_adapter(dogwood_http::data::router(engine, Some("d4t4".into())));

    assert_eq!(
        call(address, "POST", "/v1/events", Some(EVENT), &[]).status,
        401
    );
    let allowed = call(
        address,
        "POST",
        "/v1/events",
        Some(EVENT),
        &[("Authorization", "Bearer d4t4")],
    );
    assert_eq!(allowed.status, 200, "body: {}", allowed.body);
    // `/healthz` stays open even then: a probe should not need the secret.
    assert_ne!(get(address, "/healthz").status, 401);
}

// ─── The OpenAPI documents ───────────────────────────────────────────

fn document(address: SocketAddr) -> Value {
    let response = get(address, "/openapi.json");
    assert_eq!(response.status, 200, "body: {}", response.body);
    response.json()
}

fn paths_of(document: &Value) -> Vec<String> {
    document["paths"]
        .as_object()
        .expect("a paths object")
        .keys()
        .cloned()
        .collect()
}

/// The property the whole two-listener design exists to preserve: a client
/// generated from the data document cannot express a policy edit.
#[test]
fn the_data_document_has_no_policy_route() {
    let address = adapter_over_fake("doc-data", Reply::Allow);
    let mut paths = paths_of(&document(address));
    paths.sort();
    assert_eq!(
        paths,
        vec!["/healthz", "/v1/events", "/v1/ping"],
        "the data plane's vocabulary must be exactly the agent's"
    );

    // Check the *routes*, not the prose: the descriptions legitimately explain that
    // this is a policy engine. Two things a generated client derives its method set
    // from, and neither may reach authoring:
    let document = document(address);
    let routes = document["paths"].as_object().unwrap();
    for (path, operations) in routes {
        for forbidden in ["policy", "checkpoint", "schema"] {
            assert!(
                !path.contains(forbidden),
                "the data plane must not expose a route named {forbidden:?}: {path}"
            );
        }
        // Every request body on the data plane is an event and nothing else, so no
        // generated method can carry a policy bundle.
        for operation in operations.as_object().unwrap().values() {
            if let Some(schemas) =
                operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].as_str()
            {
                assert_eq!(
                    schemas, "#/components/schemas/EventBody",
                    "the data plane's only writable input is an event"
                );
            }
        }
    }
}

/// The complement: the control document has the authoring vocabulary, and declares
/// the security scheme its requirements refer to — without which Swagger UI would
/// have no Authorize button and every "try it out" would 401 with no visible fix.
#[test]
fn the_control_document_declares_its_own_routes_and_bearer_scheme() {
    let address = control_adapter("doc-control", "s3cret");
    let document = document(address);

    let mut paths = paths_of(&document);
    paths.sort();
    assert_eq!(
        paths,
        vec![
            "/healthz",
            "/v1/checkpoint",
            "/v1/policies",
            "/v1/policies/{id}",
            "/v1/policy",
            "/v1/policy/verbs",
            "/v1/schema",
            "/v1/status",
        ]
    );
    assert!(
        !paths.iter().any(|p| p == "/v1/events"),
        "the control plane must not offer the data plane's verb"
    );

    assert_eq!(
        document["components"]["securitySchemes"]["bearer"]["scheme"], "bearer",
        "the document requires a scheme it must also define"
    );
    assert!(
        document["security"][0]["bearer"].is_array(),
        "the requirement must be declared at the document level"
    );
}

#[test]
fn the_swagger_ui_is_served() {
    let address = adapter_over_fake("docs", Reply::Allow);
    let mut response = get(address, "/docs");
    if (300..400).contains(&response.status) {
        let location = response
            .header("location")
            .expect("a redirect must say where")
            .to_string();
        response = get(address, &location);
    }
    assert_eq!(response.status, 200, "body: {}", response.body);
    assert!(
        response
            .header("content-type")
            .is_some_and(|value| value.contains("text/html")),
        "expected HTML, got {:?}",
        response.header("content-type")
    );
    assert!(
        response.body.to_ascii_lowercase().contains("swagger"),
        "the served page should be the Swagger UI"
    );
}

// ─── Against the real engine, where the platform allows it ───────────

/// The engine's control plane authorizes by kernel peer credential, which on a
/// non-Linux host it cannot attest — so it refuses every caller, and the adapter
/// faithfully reports that as an upstream failure. This test pins both behaviours
/// rather than skipping, so the platform gap is *stated* by the suite instead of
/// being an absence.
#[test]
fn the_real_engine_answers_through_the_adapter() {
    let dir = start_real_engine("real");
    let engine = Engine::from_store_dir(&dir, Duration::from_secs(5));
    let data = spawn_adapter(dogwood_http::data::router(engine.clone(), None));
    let control = spawn_adapter(dogwood_http::control::router(engine, "s3cret".into()));

    // The data plane works everywhere: this is the transport the agent uses.
    assert_eq!(get(data, "/v1/ping").status, 200);

    let status = call(
        control,
        "GET",
        "/v1/status",
        None,
        &[("Authorization", "Bearer s3cret")],
    );
    if cfg!(target_os = "linux") {
        assert_eq!(status.status, 200, "body: {}", status.body);
        assert_eq!(status.json()["rule_count"], 0, "nothing is installed yet");
    } else {
        assert_eq!(
            status.status, 502,
            "off Linux the engine refuses to attest the peer, so the control plane \
             must fail closed rather than appear to work: {}",
            status.body
        );
        assert!(
            status.json()["error"]
                .as_str()
                .unwrap_or_default()
                .contains("peer attestation"),
            "the reason should be legible: {}",
            status.body
        );
    }
}
