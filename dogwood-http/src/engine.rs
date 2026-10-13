//! The bridge to the engine: one blocking socket call per HTTP request.
//!
//! # One connection per request
//!
//! `dogwood-server` keeps connections open because a client's cost floor is the
//! fsync in the append path, and paying a `connect` per event would rival it. Here
//! that trade has already been made and lost twice over — the HTTP round trip and
//! the adapter hop each dwarf a local `connect` — and the server serializes every
//! submit on one mutex anyway, so a connection pool would win concurrency the
//! engine cannot use. A fresh connection is therefore both simpler and more
//! honest: it removes any chance of a stale connection becoming a stale *verdict*.
//!
//! # Why `spawn_blocking`
//!
//! The published client is blocking `std` I/O. Calling it directly from an async
//! handler would park a runtime worker for the duration of an fsync, which is the
//! one way to turn "many concurrent callers" into "one at a time". Every call
//! moves to the blocking pool.
//!
//! # Outcomes are four-valued, not three
//!
//! A verdict, an engine-reported error, an unreachable engine, and a timeout are
//! kept distinct all the way to [`crate::data`] and [`crate::control`], because
//! collapsing them is exactly the bug this whole design exists to prevent: three
//! of the four mean "no answer", and a caller that folds "no answer" into "allow"
//! has been defeated without the engine ever being consulted.

use std::path::{Path, PathBuf};
use std::time::Duration;

use dogwood_server::Paths;
use dogwood_server::client::{ControlClient, DataClient};
use dogwood_server::protocol::{
    ControlRequest, ControlResponse, DataRequest, DataResponse, WireEvent,
};

/// A configured engine endpoint: where its sockets are, and how long we wait.
///
/// Deliberately holds no engine state, no store handle, and no policy — only the
/// paths of two sockets. That is the whole claim of this adapter: everything it
/// can do to the engine, a third-party client with a socket and twenty lines of
/// JSON could do.
///
/// The two paths are taken **explicitly**, rather than derived from the engine's
/// `--dir`, because deriving them is what forces the caller to hold a path that can
/// read the engine's private directory. A deployment that needs the data socket but
/// must not be able to see the store or the control socket — a container that mounts
/// only the engine's `run/` directory, say — can say so here.
#[derive(Debug, Clone)]
pub struct Engine {
    data_socket: PathBuf,
    control_socket: PathBuf,
    timeout: Duration,
}

impl Engine {
    pub fn new(data_socket: PathBuf, control_socket: PathBuf, timeout: Duration) -> Self {
        Engine {
            data_socket,
            control_socket,
            timeout,
        }
    }

    /// The conventional layout: both sockets under one state directory, which is
    /// where `dogwood-server --dir <dir>` puts them.
    pub fn from_store_dir(dir: impl AsRef<Path>, timeout: Duration) -> Self {
        let paths = Paths::new(dir);
        Engine::new(paths.data_socket(), paths.control_socket(), timeout)
    }

    /// The data socket — the one the monitored agent's side reaches.
    pub fn data_socket(&self) -> &Path {
        &self.data_socket
    }

    /// The control socket — the privileged one.
    pub fn control_socket(&self) -> &Path {
        &self.control_socket
    }

    /// The per-call deadline.
    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    /// Submit an event over the data socket.
    pub async fn submit(&self, event: WireEvent) -> Outcome<DataResponse> {
        let data_socket = self.data_socket.clone();
        self.call(move || {
            let mut client = DataClient::connect(&data_socket)?;
            client.call(&DataRequest::Submit { event })
        })
        .await
        .into_rejection(|response: &DataResponse| match response {
            DataResponse::Error { message } => Some(message.clone()),
            _ => None,
        })
    }

    /// Liveness and version, over the data socket.
    pub async fn ping(&self) -> Outcome<DataResponse> {
        let data_socket = self.data_socket.clone();
        self.call(move || {
            let mut client = DataClient::connect(&data_socket)?;
            client.call(&DataRequest::Ping)
        })
        .await
        .into_rejection(|response: &DataResponse| match response {
            DataResponse::Error { message } => Some(message.clone()),
            _ => None,
        })
    }

    /// Send one control request over the engine's privileged socket.
    pub async fn control(&self, request: ControlRequest) -> Outcome<ControlResponse> {
        let control_socket = self.control_socket.clone();
        self.call(move || {
            let mut client = ControlClient::connect(&control_socket)?;
            client.call(&request)
        })
        .await
        .into_rejection(|response: &ControlResponse| match response {
            ControlResponse::Error { message } => Some(message.clone()),
            _ => None,
        })
    }

    /// Run one blocking socket call on the blocking pool, bounded by the
    /// configured deadline.
    async fn call<T, F>(&self, call: F) -> Outcome<T>
    where
        F: FnOnce() -> Result<T, String> + Send + 'static,
        T: Send + 'static,
    {
        let join = tokio::task::spawn_blocking(call);
        match tokio::time::timeout(self.timeout, join).await {
            Err(_elapsed) => Outcome::Timeout,
            // A panic in the blocking closure: nothing about the request can be
            // concluded, least of all that it was allowed.
            Ok(Err(join_error)) => {
                Outcome::Unreachable(format!("engine call failed: {join_error}"))
            }
            Ok(Ok(Err(message))) => Outcome::Unreachable(message),
            Ok(Ok(Ok(response))) => Outcome::Replied(response),
        }
    }
}

/// The result of one engine call, with "no answer" kept separate from "answered".
pub enum Outcome<T> {
    /// The engine answered.
    Replied(T),
    /// The engine answered with its own `Error` variant — `ControlResponse::Error`
    /// carries validation findings; `DataResponse::Error` means the request could
    /// not be served.
    Rejected(String),
    /// The socket could not be reached, or the call failed below the protocol: no
    /// server listening, `EACCES` (including the control socket's `0700` refusing
    /// a caller that does not own the engine process), a truncated frame, a panic.
    Unreachable(String),
    /// The engine did not answer inside the deadline.
    Timeout,
}

impl<T> Outcome<T> {
    /// Promote an engine-reported `Error` variant into [`Outcome::Rejected`].
    ///
    /// The split happens here because "which variant is the error variant" is a
    /// property of the response type, and the two planes have different ones.
    fn into_rejection(self, classify: impl FnOnce(&T) -> Option<String>) -> Outcome<T> {
        match self {
            Outcome::Replied(response) => match classify(&response) {
                Some(message) => Outcome::Rejected(message),
                None => Outcome::Replied(response),
            },
            other => other,
        }
    }
}
