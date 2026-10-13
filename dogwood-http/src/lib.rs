//! An HTTP + OpenAPI adapter in front of `dogwood-server`'s Unix-socket API.
//!
//! # What this crate is, and what it is not
//!
//! It is a **deployment shim**. It links no engine, no store, and no policy code:
//! it holds two socket paths and a deadline, and every request it serves is one
//! framed JSON round trip over `dogwood_server::client`. Anything this adapter can
//! do to the engine, a third-party client with a socket and twenty lines of JSON
//! could do — which is the property that keeps it honest.
//!
//! It is not a new API surface for the engine. It is the engine's existing one,
//! re-spelled in HTTP.
//!
//! # The two planes stay two
//!
//! The engine splits its API across two sockets with **disjoint verb sets**: the
//! data socket's `DataRequest` has no variant that reads, replaces, or disables a
//! policy, and that structural absence — not a check — is what makes "the agent has
//! exactly two verbs" true. This crate keeps the split:
//!
//! | | data plane | control plane |
//! |---|---|---|
//! | default bind | `127.0.0.1:8080` | `127.0.0.1:8081` |
//! | routes | `POST /v1/events`, `GET /v1/ping`, `GET /healthz` | `/v1/policy*`, `/v1/status`, `/v1/schema`, `/v1/checkpoint`, `/healthz` |
//! | auth | none by default | bearer token, **required** |
//! | OpenAPI | [`openapi::DataApi`] | [`openapi::ControlApi`] |
//! | UI | `/docs` | `/docs` |
//!
//! Two listeners, two documents, and neither document mentions the other's routes —
//! so a client generated from the data document cannot express a policy edit. Run
//! `--plane data` and `--plane control` as separate processes (and, in Docker,
//! separate uids) to restore the separation the two sockets had; `--plane both` is
//! the single-process convenience, and it says so at startup.
//!
//! # The status-code contract, which is the actual interface
//!
//! | Situation | Status | Caller should |
//! |---|---|---|
//! | allowed | 200 | proceed |
//! | **denied** | **403** | refuse |
//! | recorded (history event) | 200 | proceed (nothing to enforce) |
//! | malformed body / unknown field | 400 | refuse |
//! | body over the engine's 16 MiB frame cap | 413 | refuse |
//! | missing or wrong bearer token | 401 | refuse |
//! | engine answered with an error | 502 | refuse |
//! | engine unreachable or transport failure | 502 | refuse |
//! | engine did not answer in time | 504 | refuse |
//!
//! **A deny is a 403, not a 200 with `"allowed": false`.** That inverts the usual
//! REST habit and is deliberate: it means a caller that proceeds on 2xx *and* a
//! caller that proceeds when the request does not throw are both fail-closed, and
//! the only way to get it wrong is to specifically interpret a 403 as permission.
//! The cost is that deny rates land in 4xx counters. The engine's own CLI made the
//! same call first — `dogwood-server submit` exits non-zero on a deny, so a shell
//! `&&` means "proceed if allowed".
//!
//! Every non-2xx body is one shape, [`dto::ErrorBody`], so a caller never has to
//! parse a second format to learn why it was refused.
//!
//! # What the HTTP surface costs, stated plainly
//!
//! **The engine's authorization is gone and a bearer token stands in for it.** The
//! engine's control socket authorizes by kernel-attested peer credential: the client
//! never supplies its uid and cannot forge it, so "who may author policy" is "which
//! OS user are you" — a question the OS already answers and can already revoke. TCP
//! has no equivalent, so [`auth`] implements the smallest thing that can substitute.
//! It is strictly weaker: a bearer token leaks through logs and shell history, has
//! no expiry of its own, and authenticates a deployment rather than a person.
//!
//! **Two operational consequences, both stated at startup rather than left in this
//! comment:**
//!
//! 1. Do not bind the control plane to anything but loopback without a reverse
//!    proxy in front of it that does real authentication. The default is loopback;
//!    changing it prints a warning because it is the one configuration change that
//!    turns "a local tool" into "an unauthenticated policy-authoring endpoint on
//!    your network".
//! 2. A timed-out `submit` (504) is genuinely ambiguous — the engine may or may not
//!    have appended the event — and the engine does not deduplicate, so a blind
//!    retry can change what a `formerly` or `count_within` rule sees afterwards.
//!    Refuse the action and resolve it out of band.
//!
//! # Where the engine's other guarantees went
//!
//! Everything else survives the hop, because it is enforced on the far side of the
//! socket and this crate does not get to weaken it: append-then-decide ordering
//! (there is no verdict without the event being on disk), the store-assigned
//! unforgeable timestamp, decisions that fail closed under evaluation errors, and
//! atomic installs that leave the running set serving when a bundle is rejected.

pub mod auth;
pub mod control;
pub mod data;
pub mod dto;
pub mod engine;
pub mod healthcheck;
pub mod openapi;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::ValueEnum;

use crate::engine::Engine;

/// Which listener(s) this process runs.
///
/// `Both` is the single-process convenience. `Data` and `Control` as separate
/// processes are what reproduce the engine's own topology — and, under Docker, what
/// lets the two run as different uids so that the data-plane process genuinely
/// cannot reach the control socket (which is `0700`, owned by the engine's uid).
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Plane {
    Data,
    Control,
    Both,
}

impl Plane {
    pub fn includes_data(self) -> bool {
        matches!(self, Plane::Data | Plane::Both)
    }

    pub fn includes_control(self) -> bool {
        matches!(self, Plane::Control | Plane::Both)
    }
}

/// Everything the adapter needs to know: where the engine is, what to bind, and
/// the shared secrets.
///
/// Note what is absent: the store directory is used **only** to derive two socket
/// paths. The adapter never opens the store, so it cannot corrupt or read the log
/// even if it wanted to.
#[derive(Debug, Clone)]
pub struct Config {
    pub plane: Plane,
    /// The engine's data socket. Taken explicitly rather than derived from a state
    /// directory, so a deployment can point this adapter at a path that has no
    /// readable state directory behind it.
    pub data_socket: PathBuf,
    /// The engine's control socket.
    pub control_socket: PathBuf,
    pub data_bind: SocketAddr,
    pub control_bind: SocketAddr,
    /// Optional on the data plane: the loopback bind is what protects it, exactly
    /// as the data socket's mode is not what protects *it*.
    pub data_token: Option<Arc<str>>,
    /// Required whenever the control plane is served.
    pub control_token: Option<Arc<str>>,
    pub engine_timeout: Duration,
    /// Acknowledge a control bind that is not loopback.
    ///
    /// Not a way to silence a warning: a way to *make the decision once, where a
    /// reviewer can see it*. A container must listen on `0.0.0.0` for a published
    /// port to reach it, so the honest Docker configuration would otherwise warn on
    /// every start — which is how a warning becomes noise. Declaring the intent in
    /// the deployment file turns a nag into a reviewed line.
    pub acknowledge_non_loopback_control: bool,
}

impl Config {
    /// Refuse a configuration that would serve an unauthenticated control plane.
    ///
    /// Fail closed at startup rather than at request time: a control listener that
    /// rejects everything is indistinguishable, from the outside, from a working one
    /// with a bug — and the operator who meant to set a token would rather find out
    /// now.
    pub fn validate(&self) -> Result<(), String> {
        if self.plane.includes_control() && self.control_token.is_none() {
            return Err(
                "the control plane requires a bearer token: set DOGWOOD_CONTROL_TOKEN \
                 (or DOGWOOD_CONTROL_TOKEN_FILE). Refusing to serve a policy-authoring \
                 endpoint without one."
                    .to_string(),
            );
        }
        if self.plane.includes_control() && self.control_token.as_deref() == Some("") {
            return Err("DOGWOOD_CONTROL_TOKEN is set but empty".to_string());
        }
        Ok(())
    }

    pub fn engine(&self) -> Engine {
        Engine::new(
            self.data_socket.clone(),
            self.control_socket.clone(),
            self.engine_timeout,
        )
    }

    /// Whatever is worth saying out loud about this configuration, as
    /// `(level, message)` pairs. Separated from [`serve`] so it can be asserted in
    /// tests rather than only observed in a log.
    pub fn advisories(&self) -> Vec<(&'static str, String)> {
        let mut advisories = Vec::new();

        if self.plane.includes_control() && !self.control_bind.ip().is_loopback() {
            advisories.push(if self.acknowledge_non_loopback_control {
                (
                    "info",
                    format!(
                        "the control plane is bound to {} — not loopback, as \
                         acknowledged. Anyone who can reach this port and holds the \
                         bearer token can rewrite the policy set: make sure the port is \
                         not published beyond the host (in Compose, publish to \
                         127.0.0.1), or that something in front of it authenticates.",
                        self.control_bind
                    ),
                )
            } else {
                (
                    "warn",
                    format!(
                        "the control plane is bound to {} — not loopback. It rewrites \
                         the policy set and is protected only by a bearer token. Bind it \
                         to 127.0.0.1, put a reverse proxy with real authentication in \
                         front of it, or pass --allow-non-loopback-control to say you \
                         mean it.",
                        self.control_bind
                    ),
                )
            });
        }

        if self.plane == Plane::Both {
            advisories.push((
                "warn",
                "one process is serving both planes, so the process that takes the \
                 agent's events can also rewrite the rules. Run `--plane data` and \
                 `--plane control` as separate processes (and, under Docker, \
                 separate uids) to restore the split the engine's two sockets had."
                    .to_string(),
            ));
        }

        for (plane, socket) in [
            ("data", self.data_socket.clone()),
            ("control", self.control_socket.clone()),
        ] {
            let in_play = match plane {
                "data" => self.plane.includes_data(),
                _ => self.plane.includes_control(),
            };
            if in_play && !socket.exists() {
                advisories.push((
                    "warn",
                    format!(
                        "the engine's {plane} socket {} does not exist yet — start \
                         `dogwood-server run` first, or every request here will be a \
                         502",
                        socket.display()
                    ),
                ));
            }
        }

        advisories
    }
}

/// Bind the configured listeners and serve until a shutdown signal.
///
/// On `SIGTERM`/`SIGINT` both listeners stop accepting and the in-flight requests
/// drain; the engine is a separate process and is **not** stopped, so its
/// clean-shutdown checkpoint is its own concern.
pub async fn serve(config: Config) -> Result<(), String> {
    config.validate()?;
    let engine = config.engine();

    let mut tasks = Vec::new();

    if config.plane.includes_data() {
        let listener = tokio::net::TcpListener::bind(config.data_bind)
            .await
            .map_err(|e| format!("bind data plane on {}: {e}", config.data_bind))?;
        tracing::info!(
            addr = %config.data_bind,
            engine_socket = %config.data_socket.display(),
            token = config.data_token.is_some(),
            "data plane listening (Swagger UI at /docs, document at /openapi.json)"
        );
        let app = data::router(engine.clone(), config.data_token.clone());
        tasks.push(tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown_signal())
                .await
                .map_err(|e| format!("data plane: {e}"))
        }));
    }

    if config.plane.includes_control() {
        // `validate()` guarantees this; the `ok_or` is here so that removing that
        // guarantee cannot turn into an unauthenticated listener.
        let token = config
            .control_token
            .clone()
            .ok_or_else(|| "control plane requires a bearer token".to_string())?;
        let listener = tokio::net::TcpListener::bind(config.control_bind)
            .await
            .map_err(|e| format!("bind control plane on {}: {e}", config.control_bind))?;
        tracing::info!(
            addr = %config.control_bind,
            engine_socket = %config.control_socket.display(),
            "control plane listening (Swagger UI at /docs; everything under /v1 requires a bearer token)"
        );
        let app = control::router(engine.clone(), token);
        tasks.push(tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown_signal())
                .await
                .map_err(|e| format!("control plane: {e}"))
        }));
    }

    for task in tasks {
        task.await
            .map_err(|e| format!("listener task failed: {e}"))??;
    }
    Ok(())
}

/// Resolve when the process is asked to stop.
async fn shutdown_signal() {
    let interrupt = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            // No SIGTERM handler available: fall back to Ctrl-C only rather than
            // exiting, which would take the listeners down at startup.
            Err(_) => std::future::pending::<()>().await,
        }
    };
    tokio::select! {
        _ = interrupt => tracing::info!("interrupt: draining"),
        _ = terminate => tracing::info!("SIGTERM: draining"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(plane: Plane, control_token: Option<&'static str>) -> Config {
        let store = std::path::Path::new("/tmp/dogwood-adapter-test-does-not-exist");
        let engine = Engine::from_store_dir(store, Duration::from_millis(500));
        Config {
            plane,
            data_socket: engine.data_socket().to_path_buf(),
            control_socket: engine.control_socket().to_path_buf(),
            data_bind: "127.0.0.1:8080".parse().unwrap(),
            control_bind: "127.0.0.1:8081".parse().unwrap(),
            data_token: None,
            control_token: control_token.map(Arc::from),
            engine_timeout: Duration::from_millis(500),
            acknowledge_non_loopback_control: false,
        }
    }

    /// The one configuration error that must stop startup.
    #[test]
    fn a_control_plane_without_a_token_is_refused() {
        assert!(config(Plane::Control, None).validate().is_err());
        assert!(config(Plane::Both, None).validate().is_err());
        assert!(config(Plane::Control, Some("x")).validate().is_ok());
        // The data plane has no such requirement: loopback is its protection.
        assert!(config(Plane::Data, None).validate().is_ok());
    }

    #[test]
    fn an_empty_token_is_not_a_token() {
        assert!(config(Plane::Control, Some("")).validate().is_err());
    }

    #[test]
    fn a_non_loopback_control_bind_is_flagged() {
        let mut c = config(Plane::Control, Some("x"));
        c.control_bind = "0.0.0.0:8081".parse().unwrap();
        let advisories = c.advisories();
        assert!(
            advisories
                .iter()
                .any(|(level, message)| *level == "warn" && message.contains("not loopback")),
            "expected a non-loopback warning, got {advisories:?}"
        );
        // ...and a loopback bind is not.
        let quiet = config(Plane::Control, Some("x")).advisories();
        assert!(
            !quiet.iter().any(|(_, m)| m.contains("not loopback")),
            "loopback should not warn: {quiet:?}"
        );
    }

    /// Acknowledging a non-loopback bind turns a warning into a notice. It must
    /// still *say* something: the decision is reviewed, not erased.
    #[test]
    fn acknowledging_a_non_loopback_bind_downgrades_rather_than_silences() {
        let mut c = config(Plane::Control, Some("x"));
        c.control_bind = "0.0.0.0:8081".parse().unwrap();
        c.acknowledge_non_loopback_control = true;
        let advisories = c.advisories();
        let (level, message) = advisories
            .iter()
            .find(|(_, m)| m.contains("not loopback"))
            .expect("the notice must still be printed");
        assert_eq!(*level, "info");
        assert!(message.contains("acknowledged"), "{message}");
    }

    /// Serving both planes in one process is the configuration that quietly
    /// dissolves the boundary, so it must not pass without a word.
    #[test]
    fn serving_both_planes_in_one_process_is_flagged() {
        let advisories = config(Plane::Both, Some("x")).advisories();
        assert!(
            advisories
                .iter()
                .any(|(_, message)| message.contains("both planes")),
            "expected a same-process warning, got {advisories:?}"
        );
    }
}
