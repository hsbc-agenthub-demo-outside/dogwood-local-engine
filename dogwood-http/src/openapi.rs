//! The two OpenAPI documents, and why there are two.
//!
//! The engine splits its API across two sockets because the vocabularies are
//! disjoint and the split is what makes "the agent has no policy verb" structural
//! rather than procedural. Publishing a single merged document would throw that
//! away at the last hop: a client generated from it would hold control methods and
//! data methods on one object, and the first thing anyone would do is point it at
//! the data port and get a 404 that looks like a bug rather than a boundary.
//!
//! So there are two documents, one per listener, and neither mentions the other's
//! routes. A caller that has the data document has a client that *cannot express* a
//! policy edit — the same property the socket's two verb sets have, one layer up.

use utoipa::OpenApi;

use crate::dto::{
    AppliedResponse, BatchBody, BatchedResponse, CheckpointResponse, ErrorBody, EventBody,
    HealthResponse, InstallBody, PolicyListResponse, PolicyResponse, PolicySummaryBody,
    PongResponse, SchemaResponse, StatusResponse, SubmitResponse, VerbBody,
};
use crate::{control, data};

/// The data plane's document: what the monitored agent may do.
///
/// Two routes and no way to read, replace, or disable a rule — the same vocabulary
/// as `DataRequest`.
#[derive(OpenApi)]
#[openapi(
    info(
        title = "Dogwood data plane",
        version = env!("CARGO_PKG_VERSION"),
        description = "An HTTP adapter over the Dogwood policy engine's data socket. \
                       \
                       \n\n**This API issues verdicts; it does not enforce them.** \
                       The caller must act on the result — a deny means do not \
                       perform the action. The engine does not intercept tool calls, \
                       does not execute them, and does not authenticate the \
                       `principal` in an event; that is the harness's job. \
                       \
                       \n\n**Read the status code, or read the body — either is \
                       safe.** A 200 means allowed (or, for a history-kind event, \
                       durably recorded). **A 403 means denied.** Every other status \
                       means no verdict was reached, and a caller that treats \
                       \"not a 2xx\" as a refusal is correct in every case. \
                       \
                       \n\n**An event has no timestamp on the wire.** The store \
                       assigns it at append, so a caller cannot backdate an event \
                       outside a temporal window or reorder itself ahead of another \
                       caller. Sending a `ts` is rejected rather than ignored, so a \
                       misspelled or invented field is a 400 with a reason, not a \
                       silent deny."
    ),
    paths(data::ping, data::submit, data::healthz),
    components(schemas(
        EventBody,
        SubmitResponse,
        PongResponse,
        HealthResponse,
        ErrorBody,
    )),
    tags(
        (name = "data", description = "Submit events and get verdicts. Reachable by the monitored agent.")
    )
)]
pub struct DataApi;

/// The control plane's document: what an operator may do.
///
/// Everything the data plane cannot express, and nothing it can.
#[derive(OpenApi)]
#[openapi(
    info(
        title = "Dogwood control plane",
        version = env!("CARGO_PKG_VERSION"),
        description = "An HTTP adapter over the Dogwood policy engine's privileged \
                       socket. \
                       \
                       \n\n**This API rewrites the rules.** Every route except \
                       `/docs`, `/openapi.json`, and `/healthz` requires \
                       `Authorization: Bearer <token>`; use the Authorize button. \
                       The engine's own control socket is protected by a `0700` \
                       mode and a kernel-attested uid allowlist, and this listener \
                       has neither — a bearer token is what stands in, which is \
                       strictly weaker: it leaks through logs and shell history, it \
                       does not expire on its own, and it authenticates a \
                       deployment rather than a person. \
                       \
                       \n\nInstalls are atomic: a rejected bundle leaves the running \
                       set serving. `POST /v1/policy` replaces everything and \
                       discards history; `POST /v1/policy/verbs` is the incremental \
                       path that keeps it. \
                       \
                       \n\nA temporal rule that is new or edited starts with an \
                       empty window, so it does not fully bite until its window has \
                       elapsed. `POST /v1/policy` reports how many leaves start cold."
    ),
    paths(
        control::install,
        control::batch,
        control::list,
        control::get_policy_by_id,
        control::show_policy,
        control::get_schema,
        control::status,
        control::checkpoint,
        control::healthz,
    ),
    components(schemas(
        InstallBody,
        BatchBody,
        VerbBody,
        AppliedResponse,
        BatchedResponse,
        PolicyListResponse,
        PolicySummaryBody,
        PolicyResponse,
        SchemaResponse,
        StatusResponse,
        CheckpointResponse,
        HealthResponse,
        ErrorBody,
    )),
    security(("bearer" = [])),
    modifiers(&BearerSecurityScheme),
    tags(
        (name = "policy", description = "Author policy. Requires a bearer token."),
        (name = "control", description = "Unauthenticated probes.")
    )
)]
pub struct ControlApi;

/// Adds the `bearer` security scheme that the document requires.
///
/// utoipa 6's `components(...)` accepts only `schemas` and `responses`, so a
/// security scheme has to be added from a modifier. Skipping it would leave the
/// document declaring a requirement on a scheme it never defines — valid enough to
/// parse, and enough to make Swagger UI omit the Authorize button, so every "try it
/// out" would 401 with no visible way to fix it. Asserted by
/// `tests/http_e2e.rs::the_control_document_declares_its_bearer_scheme`.
struct BearerSecurityScheme;

impl utoipa::Modify for BearerSecurityScheme {
    fn modify(&self, document: &mut utoipa::openapi::OpenApi) {
        use utoipa::openapi::security::{Http, HttpAuthScheme, SecurityScheme};

        let mut http = Http::new(HttpAuthScheme::Bearer);
        http.bearer_format = Some("opaque".to_string());
        http.description = Some(
            "The shared secret this deployment was started with \
             (DOGWOOD_CONTROL_TOKEN). A bearer token is not an identity: anyone \
             holding it may author policy."
                .to_string(),
        );

        document
            .components
            .get_or_insert_with(utoipa::openapi::Components::new)
            .add_security_scheme("bearer", SecurityScheme::Http(http));
    }
}
