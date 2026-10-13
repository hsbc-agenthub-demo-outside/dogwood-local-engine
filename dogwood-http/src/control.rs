//! The control plane: the HTTP face of the engine's privileged socket.
//!
//! # This listener is the new attack surface, and it is a deliberate trade
//!
//! Everything the data plane says about fail-closed applies here too, but the
//! stakes are different in kind. A data-plane mistake lets one action through; a
//! control-plane mistake lets whoever reaches it **rewrite the rules**. The engine
//! protects its control socket with a `0700` mode and a kernel-attested uid
//! allowlist; an HTTP listener has neither, so a bearer token is what stands in
//! ([`crate::auth`]), and the binary refuses to start a control listener without
//! one.
//!
//! Two things follow, and both are stated in the startup banner rather than left in
//! a document nobody reads: **bind this to loopback unless you mean it**, and put a
//! reverse proxy with real authentication in front of it before exposing it beyond
//! the machine.
//!
//! # Status codes are as precise as the engine's error type allows
//!
//! The wire returns one `Error { message }` variant, so the adapter cannot in
//! general tell a rejected policy from a broken store. Where an endpoint has
//! exactly one failure mode it is mapped precisely (`GET /v1/policies/{id}` can
//! only fail because the id is unknown, so it is a 404); where it has several, the
//! engine's message travels verbatim in the body and the status is a 400. Each
//! handler records which assumption it is making, so a future engine that gains a
//! failure mode makes the assumption visible rather than silently wrong.

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::StatusCode;
use axum::middleware::from_fn_with_state;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use dogwood_server::protocol::{ControlRequest, ControlResponse, DataResponse, MAX_FRAME};
use serde::Deserialize;
use utoipa::{IntoParams, OpenApi};
use utoipa_swagger_ui::SwaggerUi;

use crate::auth::{self, BearerAuth};
use crate::dto::{
    AppliedResponse, BatchBody, BatchedResponse, CheckpointResponse, ErrorBody, HealthResponse,
    InstallBody, PolicyListResponse, PolicyResponse, PolicySummaryBody, SchemaResponse,
    StatusResponse, VerbBody,
};
use crate::engine::{Engine, Outcome};
use crate::openapi::ControlApi;

/// Shared state for the control-plane handlers.
#[derive(Clone)]
pub struct ControlState {
    engine: Engine,
}

/// Build the control plane's router. `token` is required: there is no
/// unauthenticated control plane.
///
/// Assembly mirrors [`crate::data::router`] — see its comment for why the
/// protected and public halves are separate routers rather than one router with a
/// layer, and why the order of `layer` and `merge` is load-bearing.
pub fn router(engine: Engine, token: Arc<str>) -> Router {
    let state = ControlState {
        engine: engine.clone(),
    };

    // Everything under `/v1`, with the token layer applied as the last call — so a
    // route added above it is covered by construction.
    let protected = Router::new()
        .route("/v1/policy", get(show_policy).post(install))
        .route("/v1/policy/verbs", post(batch))
        .route("/v1/policies", get(list))
        .route("/v1/policies/{id}", get(get_policy_by_id))
        .route("/v1/schema", get(get_schema))
        .route("/v1/status", get(status))
        .route("/v1/checkpoint", post(checkpoint))
        .with_state(state.clone())
        .layer(from_fn_with_state(
            BearerAuth::new(Some(token)),
            auth::require_bearer,
        ));

    // Built after that layer, so these are deliberately uncovered by it.
    let public = Router::new()
        .route("/healthz", get(healthz))
        .with_state(state)
        .merge(SwaggerUi::new("/docs").url("/openapi.json", ControlApi::openapi()));

    protected
        .layer(DefaultBodyLimit::max(MAX_FRAME as usize))
        .merge(public)
}

/// Install a complete policy set plus its schema, replacing whatever is running —
/// the declarative path.
///
/// The engine validates before accepting and swaps atomically; a rejected install
/// leaves the running set serving, so this endpoint is safe to re-run with a
/// corrected bundle.
///
/// This is also the store-configuration entry point: it sets the event schema,
/// once. A later install that changes it is rejected.
#[utoipa::path(
    post,
    path = "/v1/policy",
    tag = "policy",
    summary = "Install a policy set and its schemas (atomic; replaces what is running)",
    description = "Also the store-configuration entry point: it fixes the event \
                   schema, and a later install that changes it is rejected. Every \
                   policy is reborn, so existing history is discarded — use \
                   POST /v1/policy/verbs to keep it.",
    request_body = InstallBody,
    responses(
        (status = 200, description = "Installed. Reports which temporal leaves start cold.", body = AppliedResponse),
        (status = 400, description = "The bundle was rejected; the running set is untouched. The body carries the engine's validation findings.", body = ErrorBody),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 413, description = "The bundle exceeds the engine's 16 MiB frame cap.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn install(State(state): State<ControlState>, body: Bytes) -> Response {
    let body: InstallBody = match serde_json::from_slice(&body) {
        Ok(body) => body,
        Err(error) => return bad_request(format!("invalid install body: {error}")),
    };

    // `Install` has two failure modes — a rejected bundle and a store that will not
    // take the write — and they are indistinguishable on the wire. A 400 is chosen
    // for both because the overwhelmingly likely cause is the bundle, and the
    // engine's message distinguishes them for a human.
    match state
        .engine
        .control(ControlRequest::Install {
            policy: body.policy,
            action_schema: body.action_schema,
            event_schema: body.event_schema,
        })
        .await
    {
        Outcome::Replied(ControlResponse::Applied {
            applied_at_nanos,
            rule_count,
            leaf_count,
            leaves_retained,
            leaves_prospective,
        }) => (
            StatusCode::OK,
            Json(AppliedResponse {
                applied_at_nanos,
                rule_count,
                leaf_count,
                leaves_retained,
                leaves_prospective,
            }),
        )
            .into_response(),
        other => classify(other, StatusCode::BAD_REQUEST),
    }
}

/// Apply a batch of policy verbs atomically over the current set — the incremental
/// authoring path.
///
/// All verbs take effect together, or the whole batch is rejected and the running
/// set is untouched. This is the path that keeps existing history, so it is what
/// you want after an initial install.
#[utoipa::path(
    post,
    path = "/v1/policy/verbs",
    tag = "policy",
    summary = "Apply a batch of policy verbs atomically",
    description = "`add` mints an id and returns it; `update` keeps the id but \
                   resets that policy's history (\"update means reset\"); \
                   `delete_all` leaves the empty set installed, so decisions fail \
                   closed. `set_action_schema` and `append_action_schema` \
                   revalidate the whole set and carry history when it validates.",
    request_body = BatchBody,
    responses(
        (status = 200, description = "Applied. Carries the ids minted for the batch's `add` verbs, in order.", body = BatchedResponse),
        (status = 400, description = "The batch was rejected; the running set is untouched. The body carries the engine's findings.", body = ErrorBody),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 413, description = "The batch exceeds the engine's 16 MiB frame cap.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn batch(State(state): State<ControlState>, body: Bytes) -> Response {
    let body: BatchBody = match serde_json::from_slice(&body) {
        Ok(body) => body,
        Err(error) => return bad_request(format!("invalid batch body: {error}")),
    };

    let verbs: Vec<_> = body.verbs.into_iter().map(VerbBody::into).collect();
    match state.engine.control(ControlRequest::Batch { verbs }).await {
        Outcome::Replied(ControlResponse::Batched {
            minted,
            applied_at_nanos,
        }) => (
            StatusCode::OK,
            Json(BatchedResponse {
                minted,
                applied_at_nanos,
            }),
        )
            .into_response(),
        other => classify(other, StatusCode::BAD_REQUEST),
    }
}

/// The query string of [`list`].
#[derive(Debug, Deserialize, IntoParams)]
pub struct ListQuery {
    /// Cap the page size. Omit to return everything.
    pub max_results: Option<usize>,
    /// Resume after this handle — the `next_token` a previous page returned.
    pub after: Option<String>,
}

/// List the installed policies' metadata, most-stable-id first, paginated.
#[utoipa::path(
    get,
    path = "/v1/policies",
    tag = "policy",
    summary = "List installed policies (metadata only, paginated)",
    description = "Metadata only — fetch a statement with GET /v1/policies/{id}. \
                   Pagination is best-effort under concurrent mutation: if the \
                   policy named by `after` is deleted between pages, the walk \
                   resumes at the end and the remaining tail is skipped. Re-list \
                   from the top if that matters.",
    params(ListQuery),
    responses(
        (status = 200, description = "A page of policy metadata.", body = PolicyListResponse),
        (status = 400, description = "`max_results=0` is rejected: a zero-size page can never make progress.", body = ErrorBody),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn list(State(state): State<ControlState>, Query(query): Query<ListQuery>) -> Response {
    match state
        .engine
        .control(ControlRequest::List {
            max_results: query.max_results,
            next_token: query.after,
        })
        .await
    {
        Outcome::Replied(ControlResponse::PolicyList {
            policies,
            next_token,
        }) => (
            StatusCode::OK,
            Json(PolicyListResponse {
                policies: policies
                    .into_iter()
                    .map(|p| PolicySummaryBody {
                        id: p.id,
                        created: p.created,
                        updated: p.updated,
                    })
                    .collect(),
                next_token,
            }),
        )
            .into_response(),
        // `List`'s only request-shaped failure is a zero page size.
        other => classify(other, StatusCode::BAD_REQUEST),
    }
}

/// Print one policy's canonical statement by id.
#[utoipa::path(
    get,
    path = "/v1/policies/{id}",
    tag = "policy",
    summary = "Fetch one policy's source by id",
    params(("id" = String, Path, description = "The opaque engine-minted handle, from GET /v1/policies")),
    responses(
        (status = 200, description = "The policy's canonical statement.", body = PolicyResponse),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 404, description = "No policy with that id.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn get_policy_by_id(
    State(state): State<ControlState>,
    Path(id): Path<String>,
) -> Response {
    // `GetPolicyById` has exactly one failure mode — an unknown id — so a 404 is
    // precise rather than a guess.
    match state
        .engine
        .control(ControlRequest::GetPolicyById { id })
        .await
    {
        Outcome::Replied(ControlResponse::Policy { source }) => {
            (StatusCode::OK, Json(PolicyResponse { source })).into_response()
        }
        other => classify(other, StatusCode::NOT_FOUND),
    }
}

/// Print the whole installed policy source, every policy concatenated.
#[utoipa::path(
    get,
    path = "/v1/policy",
    tag = "policy",
    summary = "Fetch the whole installed policy source",
    responses(
        (status = 200, description = "Every policy's canonical statement, concatenated.", body = PolicyResponse),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 404, description = "No policy set is installed.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn show_policy(State(state): State<ControlState>) -> Response {
    // `GetPolicy` has exactly one failure mode — nothing is installed.
    match state.engine.control(ControlRequest::GetPolicy).await {
        Outcome::Replied(ControlResponse::Policy { source }) => {
            (StatusCode::OK, Json(PolicyResponse { source })).into_response()
        }
        other => classify(other, StatusCode::NOT_FOUND),
    }
}

/// Print the store's schemas: the original action schema and the configured event
/// schema.
#[utoipa::path(
    get,
    path = "/v1/schema",
    tag = "policy",
    summary = "Fetch the store's action and event schemas",
    description = "The **original** action schema the operator authored, not the \
                   schema the engine augments it into internally.",
    responses(
        (status = 200, description = "The store's schemas. `event_schema` is absent when the built-in default is in use.", body = SchemaResponse),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 404, description = "No policy set is installed, so no schema is configured.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn get_schema(State(state): State<ControlState>) -> Response {
    // `GetSchema` has exactly one failure mode — no policy set, so no schema.
    match state.engine.control(ControlRequest::GetSchema).await {
        Outcome::Replied(ControlResponse::Schema {
            action_schema,
            event_schema,
        }) => (
            StatusCode::OK,
            Json(SchemaResponse {
                action_schema,
                event_schema,
            }),
        )
            .into_response(),
        other => classify(other, StatusCode::NOT_FOUND),
    }
}

/// Report what is installed and running.
#[utoipa::path(
    get,
    path = "/v1/status",
    tag = "policy",
    summary = "Report what is installed and running",
    description = "Read `decision_kinds` rather than hardcoding `request`: which \
                   kinds decide is schema configuration. An empty `partition_key` \
                   means the stream is unshardable, so the engine runs a single \
                   instance.",
    responses(
        (status = 200, description = "The engine's current state.", body = StatusResponse),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its state is unavailable.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn status(State(state): State<ControlState>) -> Response {
    // `Status`'s only failure mode is a poisoned state mutex ("server state
    // unavailable; restart required"), i.e. an upstream problem, so 502.
    match state.engine.control(ControlRequest::Status).await {
        Outcome::Replied(ControlResponse::Status {
            rule_count,
            leaf_count,
            incremental_leaves,
            decision_kinds,
            partition_key,
            log_offset,
            control_uids,
        }) => (
            StatusCode::OK,
            Json(StatusResponse {
                rule_count,
                leaf_count,
                incremental_leaves,
                decision_kinds,
                partition_key,
                log_offset,
                control_uids,
            }),
        )
            .into_response(),
        other => classify(other, StatusCode::BAD_GATEWAY),
    }
}

/// Force a state snapshot now.
#[utoipa::path(
    post,
    path = "/v1/checkpoint",
    tag = "policy",
    summary = "Force a state snapshot now",
    description = "The engine also checkpoints on a clean shutdown and every N \
                   events, so this is for pinning the log's watermark before \
                   something risky, not a routine duty.",
    responses(
        (status = 200, description = "Checkpointed through the reported offset.", body = CheckpointResponse),
        (status = 401, description = "Missing or invalid bearer token.", body = ErrorBody),
        (status = 502, description = "The engine could not be reached, or its store refused the write.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn checkpoint(State(state): State<ControlState>) -> Response {
    // `Checkpoint`'s only failure mode is a store error — always upstream, so 502.
    match state.engine.control(ControlRequest::Checkpoint).await {
        Outcome::Replied(ControlResponse::Checkpointed { up_to_offset }) => {
            (StatusCode::OK, Json(CheckpointResponse { up_to_offset })).into_response()
        }
        other => classify(other, StatusCode::BAD_GATEWAY),
    }
}

/// Readiness probe, deliberately outside the token layer so an orchestrator does
/// not need the control secret. It exercises the **data** socket: that is reachable
/// without privileges, and it is enough to tell "the engine is up" from "this
/// adapter is up".
#[utoipa::path(
    get,
    path = "/healthz",
    tag = "control",
    summary = "Readiness probe (unauthenticated)",
    responses(
        (status = 200, description = "The engine answered a ping over its data socket.", body = HealthResponse),
        (status = 502, description = "The engine could not be reached.", body = ErrorBody),
        (status = 504, description = "The engine did not answer inside the deadline.", body = ErrorBody),
    )
)]
pub async fn healthz(State(state): State<ControlState>) -> Response {
    match state.engine.ping().await {
        Outcome::Replied(DataResponse::Pong { version }) => (
            StatusCode::OK,
            Json(HealthResponse {
                status: "ok".to_string(),
                version: Some(version),
            }),
        )
            .into_response(),
        Outcome::Replied(other) => (
            StatusCode::BAD_GATEWAY,
            Json(ErrorBody {
                error: format!("engine sent an unexpected reply to ping: {other:?}"),
                source: "transport",
            }),
        )
            .into_response(),
        Outcome::Rejected(message) => (
            StatusCode::BAD_GATEWAY,
            Json(ErrorBody {
                error: message,
                source: "engine",
            }),
        )
            .into_response(),
        Outcome::Unreachable(message) => (
            StatusCode::BAD_GATEWAY,
            Json(ErrorBody {
                error: message,
                source: "transport",
            }),
        )
            .into_response(),
        Outcome::Timeout => (
            StatusCode::GATEWAY_TIMEOUT,
            Json(ErrorBody {
                error: "the engine did not answer within the deadline".to_string(),
                source: "transport",
            }),
        )
            .into_response(),
    }
}

/// Map a non-success [`Outcome`], or a reply the handler did not expect, to a
/// response.
///
/// `rejection_status` is the status for the endpoint-specific "the engine said no"
/// case: 400 where the reply is the engine's judgement on the request, 404 where
/// the engine has exactly one reason to say no and it means "nothing there", 502
/// where the only failure is upstream. An unexpected *reply* is always 502 — it
/// means this adapter and that engine disagree about the protocol, which is not
/// something the adapter can adjudicate.
fn classify(outcome: Outcome<ControlResponse>, rejection_status: StatusCode) -> Response {
    match outcome {
        Outcome::Replied(response) => (
            StatusCode::BAD_GATEWAY,
            Json(ErrorBody {
                error: format!("engine sent an unexpected reply: {response:?}"),
                source: "transport",
            }),
        )
            .into_response(),
        Outcome::Rejected(message) => (
            rejection_status,
            Json(ErrorBody {
                error: message,
                source: "engine",
            }),
        )
            .into_response(),
        Outcome::Unreachable(message) => (
            StatusCode::BAD_GATEWAY,
            Json(ErrorBody {
                error: message,
                source: "transport",
            }),
        )
            .into_response(),
        Outcome::Timeout => (
            StatusCode::GATEWAY_TIMEOUT,
            Json(ErrorBody {
                error: "the engine did not answer within the deadline".to_string(),
                source: "transport",
            }),
        )
            .into_response(),
    }
}

fn bad_request(error: String) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(ErrorBody {
            error,
            source: "request",
        }),
    )
        .into_response()
}
