//! The HTTP-facing request and response types.
//!
//! # Why these are not the wire types
//!
//! `dogwood-server`'s [`WireEvent`] and friends are the *socket* contract, and
//! this crate does not get to derive `ToSchema` on them (they live in another
//! crate, and that crate is a read-only mirror). So the HTTP contract is spelled
//! out here as its own set of types, and [`EventBody`] converts into the wire
//! type at the boundary.
//!
//! That duplication is a real cost — two definitions that can drift — and it is
//! paid deliberately, because the two contracts genuinely are different things:
//! the wire one is pinned to the engine, the HTTP one is what you publish to
//! callers and version independently. The cost is contained by
//! `tests/dto_drift.rs`, which asserts that an `EventBody` and the `WireEvent` it
//! becomes serialize to *identical* JSON — so a field added on one side and not
//! the other fails the build rather than producing a silent hole.
//!
//! # Unknown fields are rejected
//!
//! [`EventBody`] carries `deny_unknown_fields`, matching the wire type. The
//! engine's reasoning applies unchanged at the HTTP boundary: an event whose
//! field name is misspelled would otherwise become an event with no history —
//! which fails closed, but for a reason nobody can see. A 400 naming the field is
//! strictly more useful than a deny nobody can explain.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use utoipa::ToSchema;

use dogwood_server::protocol::{WireEvent, WireVerb};

// ─── The event ───────────────────────────────────────────────────────

/// An event, as an HTTP caller sends it.
///
/// Schema-neutral by construction, exactly like the wire type: `kind` is an
/// arbitrary string the *installed schema* interprets (it decides which kinds
/// decide), and the field bags are free-form JSON whose dotted paths are whatever
/// the schema declares.
///
/// Note what is absent: **a timestamp**. The store assigns it at append, so a
/// caller cannot backdate an event outside a window or reorder itself ahead of
/// another caller. Sending one is rejected rather than ignored, so a caller is
/// never left believing it set the event's time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
#[schema(example = json!({
    "action": "HSBC::Action::transfer",
    "kind": "request",
    "principal": "HSBC::User::\"alice\"",
    "resource": "HSBC::Account::\"main\"",
    "logged": { "input": { "amount": 1000 } },
    "context": { "input": { "amount": 1000 } }
}))]
pub struct EventBody {
    /// The qualified action, for example `HSBC::Action::transfer`.
    pub action: String,
    /// The event kind — `request` (a decision point) or `response` (history) under
    /// the default schema. What a kind *means* is the installed schema's business,
    /// not this adapter's; ask the control plane's `GET /v1/status` which kinds
    /// decide.
    pub kind: String,
    /// The request principal's entity uid, for example `HSBC::User::"alice"`.
    ///
    /// History is keyed by principal, so **an event sent without one files its
    /// history under a different key** — the symptom is a silent deny on a rule
    /// that should have matched.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<String>,
    /// The request resource's entity uid, if any.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resource: Option<String>,
    /// The **logged temporal record**: the durable history future timepoints
    /// correlate against. A JSON object of groups, for example
    /// `{"input": {"amount": 1000}}`.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub logged: Map<String, Value>,
    /// The **request-only context**, which a policy reads as
    /// `context.<group>.<name>`. Deliberately a separate bag from `logged`: a
    /// field both need is sent in both.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub context: Map<String, Value>,
    /// Entity attributes for this decision, keyed by canonical uid:
    /// `{"HSBC::User::\"alice\"": {"dept": "eng"}}`, so a policy can read
    /// `principal.dept`.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub entities: Map<String, Value>,
    /// Direct parent uids per entity, for `principal in Group::"admins"`.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub parents: Map<String, Value>,
}

impl From<EventBody> for WireEvent {
    fn from(body: EventBody) -> Self {
        WireEvent {
            action: body.action,
            kind: body.kind,
            principal: body.principal,
            resource: body.resource,
            logged: body.logged,
            context: body.context,
            entities: body.entities,
            parents: body.parents,
        }
    }
}

// ─── Data-plane responses ────────────────────────────────────────────

/// The answer to a submitted event.
///
/// Which variant you get is the **installed schema's** decision, not the
/// adapter's: a decision-kind event fuses append + monitor step + verdict, a
/// history-kind event is an append acked with no verdict.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum SubmitResponse {
    /// A verdict. Delivered with **200 when allowed** and **403 when denied** —
    /// see the crate docs for why a deny is a 4xx here.
    Decision {
        /// `true` = allow. Named for the question the caller is actually asking,
        /// so an unrecognized spelling cannot be mistaken for permission.
        ///
        /// A caller that only looks at the HTTP status is also safe: a deny is a
        /// 403, never a 200.
        allowed: bool,
        /// The durable tokens of the policies that determined the verdict.
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        reason: Vec<String>,
        /// Evaluation errors accompanying the decision. A Dogwood decision
        /// degrades rather than aborting, so these travel *with* a fail-closed
        /// deny; their presence is never a reason to retry-and-proceed.
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        errors: Vec<String>,
        /// When the store recorded this event, in epoch **nanoseconds**.
        ///
        /// The verdict is only meaningful relative to a moment, and this is that
        /// moment: every window the rules declare is measured against it. The
        /// caller cannot derive it — the store assigns it — so returning it is
        /// the only way a client can reason about when its own history ages out.
        recorded_at_nanos: i64,
    },
    /// A history-kind event was durably recorded; no verdict applies.
    ///
    /// The *arrival* of this response is what tells the caller the event is on
    /// disk — history events are not fire-and-forget.
    Recorded {
        /// When the store recorded this event, in epoch **nanoseconds**.
        recorded_at_nanos: i64,
    },
}

/// The data plane's liveness reply. Carries no policy information.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct PongResponse {
    /// The `dogwood-server` version, for diagnosing a mismatch between the
    /// adapter and the engine it is pointed at.
    pub version: String,
}

/// A readiness probe's body.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct HealthResponse {
    pub status: String,
    /// The engine's version, when it answered.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
}

// ─── Control-plane requests ──────────────────────────────────────────

/// Install a complete policy set plus its action schema, replacing whatever is
/// installed — the declarative path (`[DeleteAll; Add …]`).
///
/// This is also the store-configuration entry point: it sets the event schema,
/// which is fixed thereafter. A later install that changes it is rejected.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct InstallBody {
    /// The `.dw` policy source.
    pub policy: String,
    /// The Cedar action schema (`.cedarschema`).
    pub action_schema: String,
    /// The event-schema DSL (`.dwschema`). Omit for the built-in default.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_schema: Option<String>,
}

/// Apply a batch of policy verbs atomically over the current set — the
/// incremental authoring path. All verbs take effect together, or the whole batch
/// is rejected and the running set is untouched.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct BatchBody {
    pub verbs: Vec<VerbBody>,
}

/// One policy-management verb. The transport form of the engine's `Verb`.
///
/// `Add` carries no id — the engine mints one and returns it, because an opaque
/// engine-assigned handle is what makes "address this policy" a capability rather
/// than a guess.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(tag = "verb", rename_all = "snake_case")]
pub enum VerbBody {
    /// Install a new policy — minted a fresh id, born with no history.
    Add { policy: String },
    /// Replace a policy's content by id. Same id, but its history resets:
    /// update means reset.
    Update { id: String, policy: String },
    /// Remove a policy by id.
    Delete { id: String },
    /// Clear one policy's accumulated history; content unchanged.
    Reset { id: String },
    /// Remove every policy. The schema is kept and the empty set stays installed,
    /// so decisions then fail closed.
    DeleteAll,
    /// Clear every policy's history; all policies are kept.
    ResetAll,
    /// Revalidate and re-lower the whole set under a new action schema. All
    /// history carries if it validates; the batch is rejected if it does not.
    SetActionSchema { action_schema: String },
    /// Append a fragment onto the current action schema, atomically — so a caller
    /// can add a new entity or action without a fetch-then-set round trip that a
    /// concurrent change could invalidate.
    AppendActionSchema { fragment: String },
}

impl From<VerbBody> for WireVerb {
    fn from(body: VerbBody) -> Self {
        match body {
            VerbBody::Add { policy } => WireVerb::Add { policy },
            VerbBody::Update { id, policy } => WireVerb::Update { id, policy },
            VerbBody::Delete { id } => WireVerb::Delete { id },
            VerbBody::Reset { id } => WireVerb::Reset { id },
            VerbBody::DeleteAll => WireVerb::DeleteAll,
            VerbBody::ResetAll => WireVerb::ResetAll,
            VerbBody::SetActionSchema { action_schema } => {
                WireVerb::SetActionSchema { action_schema }
            }
            VerbBody::AppendActionSchema { fragment } => WireVerb::AppendActionSchema { fragment },
        }
    }
}

// ─── Control-plane responses ─────────────────────────────────────────

/// A policy's metadata, without its source.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct PolicySummaryBody {
    /// The opaque engine-minted handle. Non-sequential by design — to order
    /// policies, sort by `created`, not by this.
    pub id: String,
    /// Epoch-nanosecond timestamps the policy was created / last updated at.
    pub created: i64,
    pub updated: i64,
}

/// An install landed, with the prospective-install consequences so the operator
/// sees which rules start cold.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct AppliedResponse {
    /// When the change took effect, in epoch **nanoseconds** — the moment the
    /// change record was assigned, on the same clock and sequence as every
    /// event's `recorded_at_nanos`. So an operator can say exactly which events
    /// this set governed: those stamped at or after it.
    pub applied_at_nanos: i64,
    /// Rules in the newly installed set.
    pub rule_count: usize,
    /// Temporal leaves in the new set.
    pub leaf_count: usize,
    /// Leaves that kept their accumulated window (unchanged formulas).
    pub leaves_retained: usize,
    /// Leaves starting empty — new or edited formulas, subject to the warm-up
    /// window. A new temporal rule does not fully bite until its window has
    /// elapsed, which is the surprising half of a prospective install.
    pub leaves_prospective: usize,
}

/// A batch landed.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct BatchedResponse {
    /// The ids assigned to the batch's `Add` verbs, in listed order.
    pub minted: Vec<String>,
    /// When the batch landed, in epoch **nanoseconds**.
    pub applied_at_nanos: i64,
}

/// A page of policy metadata.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct PolicyListResponse {
    pub policies: Vec<PolicySummaryBody>,
    /// Present when more remain: pass it back as `after` to resume.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next_token: Option<String>,
}

/// A policy's canonical statement.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct PolicyResponse {
    pub source: String,
}

/// The store's schemas: the original action schema the operator authored (not
/// the augmented one) and the configured event schema.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct SchemaResponse {
    pub action_schema: String,
    /// Absent when the built-in default event schema is in use.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_schema: Option<String>,
}

/// What is installed and running.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct StatusResponse {
    pub rule_count: usize,
    pub leaf_count: usize,
    /// Leaves running on the incremental path (versus the scan fallback).
    pub incremental_leaves: usize,
    /// The event kinds the installed schema treats as decision points. Read this
    /// rather than hardcoding `request`: which kinds decide is schema
    /// configuration.
    pub decision_kinds: Vec<String>,
    /// The dotted field paths the event stream may be sharded on. **Empty means
    /// unshardable** — the schema declares no universal symmetric pin, so
    /// partitioning would change verdicts and the server runs a single instance.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub partition_key: Vec<String>,
    /// Events durably appended since the log was created (the next offset).
    pub log_offset: u64,
    /// The uids permitted on the engine's control socket.
    pub control_uids: Vec<u32>,
}

/// A checkpoint completed.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct CheckpointResponse {
    pub up_to_offset: u64,
}

// ─── Errors ──────────────────────────────────────────────────────────

/// Every non-2xx body on both planes.
///
/// One shape, so a caller that treats "not a 2xx" as a refusal never has to parse
/// a second format to find out why.
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct ErrorBody {
    /// What went wrong, in the engine's or the adapter's own words.
    pub error: String,
    /// Which hop produced it: `"request"` (this adapter rejected the request),
    /// `"engine"` (the engine answered with an error), or `"transport"` (the
    /// engine could not be reached or did not answer in time).
    pub source: &'static str,
}
