//! The data plane: the HTTP face of the socket the monitored agent is allowed to
//! reach.
//!
//! # The load-bearing decision: a deny is a 403
//!
//! The tempting mapping is "200 with `allowed: false`", which is what most REST
//! APIs do with a business outcome. It is the wrong mapping here, and the reason is
//! the failure mode, not the aesthetics.
//!
//! A caller can be naive in exactly two ways:
//!
//! - it proceeds when the status is 2xx, or
//! - it proceeds when the call does not throw.
//!
//! With "200 + `allowed: false`", the first caller treats a deny as permission —
//! silently, and with no symptom other than a tool that should not have run. With a
//! 403, *both* naive callers fail closed: the status is not 2xx, and most HTTP
//! clients surface a 4xx as an error. The only caller that gets it wrong is one
//! that specifically interprets a 403 body as permission, which is not a mistake
//! anyone makes by accident.
//!
//! This also settles the more common question — "isn't a deny a normal outcome, so
//! shouldn't it be 2xx?" — against the engine's own precedent: the CLI's `submit`
//! **exits non-zero on a deny** (`dogwood-server/src/main.rs`, `report_data`),
//! because "a shell user piping this into `&&` means: proceed if allowed". The
//! adapter is making the same choice at a different layer. The cost is real and
//! worth naming: deny rates land in 4xx counters, so dashboards will need to
//! exclude them rather than treat them as client errors.
//!
//! # Every other non-2xx is also "no answer"
//!
//! | Situation | Status | Caller should |
//! |---|---|---|
//! | `allow` | 200 | proceed |
//! | `deny` | 403 | refuse |
//! | malformed event | 400 | refuse |
//! | body over 16 MiB | 413 | refuse |
//! | engine answered with an error | 502 | refuse |
//! | engine unreachable / transport failure | 502 | refuse |
//! | engine did not answer in time | 504 | refuse |
//! | token required but missing/wrong | 401 | refuse |
//!
//! # Do not retry a 504 blindly
//!
//! A timed-out `submit` is genuinely ambiguous: the engine may have appended the
//! event and decided, or never seen it. The engine does not deduplicate, so a blind
//! retry can double-append a history event and change what a `formerly` or
//! `count_within` rule sees afterwards. Treat a 504 as "refuse this action", and
//! resolve the ambiguity out of band (the timestamp comes back on a successful
//! call; a `response` event can be replayed by the harness if the action never
//! happened).

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, State};
use axum::middleware::from_fn_with_state;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use dogwood_server::protocol::{DataResponse, MAX_FRAME};
use utoipa::OpenApi;
use utoipa_swagger_ui::SwaggerUi;

use crate::auth::{self, BearerAuth};
use crate::dto::{ErrorBody, EventBody, HealthResponse, PongResponse, SubmitResponse};
use crate::engine::{Engine, Outcome};
use crate::openapi::DataApi;

/// Shared state for the data-plane handlers.
#[derive(Clone)]
pub struct DataState {
    engine: Engine,
}

/// Build the data plane's router.
///
/// # Why this is assembled in two pieces
///
/// `Router::layer` covers the routes that exist *when it is called*; anything
/// merged in afterwards is uncovered. That is load-bearing here rather than
/// incidental — `/docs`, `/openapi.json`, and `/healthz` must stay reachable
/// without a token (a browser fetching the Swagger UI's HTML cannot attach an
/// `Authorization` header to that first request, and a container health probe
/// cannot either), while everything under `/v1` must not.
///
/// The `protected()` closure takes the token so that adding a route inside it is
/// the only way to get one: a route added to the private router is under the layer
/// by construction, and a route added to `public` is deliberately not.
pub fn router(engine: Engine, token: Option<Arc<str>>) -> Router {
    let state = DataState {
        engine: engine.clone(),
    };

    // Everything under `/v1`, with the token layer applied as the last call — so a
    // route added above it is covered by construction.
    let protected = Router::new()
        .route("/v1/ping", get(ping))
        .route("/v1/events", post(submit))
        .with_state(state.clone())
        .layer(from_fn_with_state(
            BearerAuth::new(token),
            auth::require_bearer,
        ));

    // Built after that layer, so these are deliberately uncovered by it: a browser
    // fetching the UI's HTML cannot attach an `Authorization` header to that first
    // request, and a health probe should not need the secret.
    let public = Router::new()
        .route("/healthz", get(healthz))
        .with_state(state)
        // Serves the raw document at /openapi.json and the UI at /docs.
        .merge(SwaggerUi::new("/docs").url("/openapi.json", DataApi::openapi()));

    protected
        // Matches the engine's own frame cap, so a body the engine would reject as
        // an oversized frame is refused at the edge instead of after a socket hop.
        .layer(DefaultBodyLimit::max(MAX_FRAME as usize))
        .merge(public)
}

/// Liveness and version. Touches the engine, so it reports more than "the process
/// is up": a 200 here means the data socket answered.
#[utoipa::path(
    get,
    path = "/v1/ping",
    tag = "data",
    summary = "Liveness and version handshake",
    description = "Pings the engine over its data socket. Carries no policy \
                   information. A 200 means the engine answered, not merely that \
                   this adapter is running.",
    responses(
        (status = 200, description = "The engine answered.", body = PongResponse),
        (status = 401, description = "A data-plane token is configured and was missing or wrong.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or answered with an error.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn ping(State(state): State<DataState>) -> Response {
    match state.engine.ping().await {
        Outcome::Replied(DataResponse::Pong { version }) => {
            (axum::http::StatusCode::OK, Json(PongResponse { version })).into_response()
        }
        // The data socket's protocol admits exactly one reply to `Ping`; anything
        // else means this adapter and that engine disagree about the protocol, so
        // the honest answer is "no answer".
        Outcome::Replied(other) => no_answer(
            axum::http::StatusCode::BAD_GATEWAY,
            "transport",
            format!("engine sent an unexpected reply to ping: {other:?}"),
        ),
        Outcome::Rejected(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "engine", message)
        }
        Outcome::Unreachable(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "transport", message)
        }
        Outcome::Timeout => timeout(state.engine.timeout()),
    }
}

/// Submit one event.
///
/// Which reply you get — a verdict or a bare acknowledgement — is the *installed
/// schema's* decision, not this adapter's: a decision-kind event fuses durable
/// append, monitor step, and verdict; a history-kind event is an append acked with
/// no verdict. Under the default schema those are `request` and `response`.
#[utoipa::path(
    post,
    path = "/v1/events",
    tag = "data",
    summary = "Submit an event and, if its kind is a decision point, get a verdict",
    description = "The single schema-driven primitive. \
                   \n\n**200 means allowed; 403 means denied.** Every other status \
                   means no verdict was reached and the caller must refuse: see the \
                   crate documentation for the full table. \
                   \n\nAn event carries no timestamp — the store assigns it at \
                   append, so a caller cannot backdate an event outside a window. \
                   Sending a `ts` is rejected, not ignored. \
                   \n\nA 504 is ambiguous: the engine may or may not have appended \
                   the event. Do not retry blindly.",
    request_body = EventBody,
    responses(
        (status = 200, description = "Allowed (a decision-kind event), or durably recorded (a history-kind event).", body = SubmitResponse),
        (status = 400, description = "The event body is not a valid event — including an unknown field, which is rejected rather than dropped.", body = ErrorBody),
        (status = 401, description = "A data-plane token is configured and was missing or wrong.", body = ErrorBody),
        (status = 403, description = "Denied. The body carries the determining policies; a client that only reads status codes is also safe, because this is not a 2xx.", body = SubmitResponse),
        (status = 413, description = "The body exceeds the engine's 16 MiB frame cap.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or answered with an error.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline. Ambiguous — do not retry blindly.", body = ErrorBody),
    )
)]
pub async fn submit(State(state): State<DataState>, body: Bytes) -> Response {
    // Deserialized by hand rather than with `Json<EventBody>`, so that a malformed
    // body produces this crate's one error shape instead of axum's. A caller that
    // has to parse two error formats to learn why it was refused is a caller that
    // will get it wrong once.
    let event: EventBody = match serde_json::from_slice(&body) {
        Ok(event) => event,
        Err(error) => {
            return no_answer(
                axum::http::StatusCode::BAD_REQUEST,
                "request",
                format!("invalid event: {error}"),
            );
        }
    };

    match state.engine.submit(event.into()).await {
        Outcome::Replied(DataResponse::Decision {
            allowed,
            reason,
            errors,
            recorded_at_nanos,
        }) => {
            let status = if allowed {
                axum::http::StatusCode::OK
            } else {
                axum::http::StatusCode::FORBIDDEN
            };
            (
                status,
                Json(SubmitResponse::Decision {
                    allowed,
                    reason,
                    errors,
                    recorded_at_nanos,
                }),
            )
                .into_response()
        }
        Outcome::Replied(DataResponse::Recorded { recorded_at_nanos }) => (
            axum::http::StatusCode::OK,
            Json(SubmitResponse::Recorded { recorded_at_nanos }),
        )
            .into_response(),
        Outcome::Replied(other) => no_answer(
            axum::http::StatusCode::BAD_GATEWAY,
            "transport",
            format!("engine sent an unexpected reply to submit: {other:?}"),
        ),
        Outcome::Rejected(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "engine", message)
        }
        Outcome::Unreachable(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "transport", message)
        }
        Outcome::Timeout => timeout(state.engine.timeout()),
    }
}

/// Container/readiness probe. Mirrors [`ping`] but is deliberately unauthenticated
/// and lives outside the token layer, so an orchestrator does not need the secret.
#[utoipa::path(
    get,
    path = "/healthz",
    tag = "data",
    summary = "Readiness probe (unauthenticated)",
    responses(
        (status = 200, description = "The engine answered a ping.", body = HealthResponse),
        (status = 502, description = "The engine could not be reached.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn healthz(State(state): State<DataState>) -> Response {
    match state.engine.ping().await {
        Outcome::Replied(DataResponse::Pong { version }) => (
            axum::http::StatusCode::OK,
            Json(HealthResponse {
                status: "ok".to_string(),
                version: Some(version),
            }),
        )
            .into_response(),
        Outcome::Replied(other) => no_answer(
            axum::http::StatusCode::BAD_GATEWAY,
            "transport",
            format!("engine sent an unexpected reply to ping: {other:?}"),
        ),
        Outcome::Rejected(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "engine", message)
        }
        Outcome::Unreachable(message) => {
            no_answer(axum::http::StatusCode::BAD_GATEWAY, "transport", message)
        }
        Outcome::Timeout => timeout(state.engine.timeout()),
    }
}

fn no_answer(status: axum::http::StatusCode, source: &'static str, error: String) -> Response {
    (status, Json(ErrorBody { error, source })).into_response()
}

fn timeout(after: std::time::Duration) -> Response {
    no_answer(
        axum::http::StatusCode::GATEWAY_TIMEOUT,
        "transport",
        format!(
            "the engine did not answer within {}ms; this is not a verdict — refuse the action",
            after.as_millis()
        ),
    )
}
