//! The HTTP DTOs are a second spelling of `dogwood-server`'s wire types, and this
//! file is what stops them drifting apart.
//!
//! `dogwood-http` cannot derive `ToSchema` on the wire types — they live in another
//! crate — so `dto::EventBody` and friends restate them field for field. That
//! duplication is a real cost, and it is only acceptable while something fails
//! loudly when the two definitions disagree. That something is this file: every
//! case compares the *serialized* form of an HTTP DTO against the serialized form of
//! the wire value it converts into.
//!
//! Serialized form rather than field-by-field, because that is the actual contract
//! — `skip_serializing_if`, `deny_unknown_fields`, and the field names are the
//! things a caller observes, and a struct that is equal field-by-field but
//! serializes differently is exactly the drift worth catching.

use dogwood_http::dto::{BatchBody, EventBody, VerbBody};
use dogwood_server::protocol::{WireEvent, WireVerb};
use serde_json::{Value, json};

/// A fully populated event: every optional field present.
fn populated_event() -> EventBody {
    EventBody {
        action: "HSBC::Action::transfer".to_string(),
        kind: "request".to_string(),
        principal: Some("HSBC::User::\"alice\"".to_string()),
        resource: Some("HSBC::Account::\"main\"".to_string()),
        logged: serde_json::from_value(json!({"input": {"amount": 1000}})).unwrap(),
        context: serde_json::from_value(json!({"input": {"amount": 1000}})).unwrap(),
        entities: serde_json::from_value(json!({"HSBC::User::\"alice\"": {"dept": "eng"}}))
            .unwrap(),
        parents: serde_json::from_value(
            json!({"HSBC::User::\"alice\"": ["HSBC::Group::\"admins\""]}),
        )
        .unwrap(),
    }
}

fn as_json<T: serde::Serialize>(value: &T) -> Value {
    serde_json::to_value(value).expect("serializes")
}

/// The whole point: the HTTP body and the wire event are the same JSON.
#[test]
fn a_populated_event_serializes_exactly_as_the_wire_event_it_becomes() {
    let body = populated_event();
    let wire: WireEvent = body.clone().into();
    assert_eq!(
        as_json(&body),
        as_json(&wire),
        "EventBody and WireEvent have drifted: the HTTP contract is no longer the wire contract"
    );
}

/// A minimal event: the absent fields must be absent on *both* sides, not merely
/// empty on one.
#[test]
fn a_minimal_event_omits_the_same_fields_on_both_sides() {
    let body = EventBody {
        action: "A::Action::Read".to_string(),
        kind: "request".to_string(),
        principal: None,
        resource: None,
        logged: Default::default(),
        context: Default::default(),
        entities: Default::default(),
        parents: Default::default(),
    };
    let wire: WireEvent = body.clone().into();
    assert_eq!(as_json(&body), as_json(&wire));
    assert_eq!(
        as_json(&body),
        json!({"action": "A::Action::Read", "kind": "request"})
    );
}

/// Both sides reject an unknown field, so a caller cannot silently send a field
/// that the engine will never read.
#[test]
fn an_unknown_event_field_is_rejected_on_both_sides() {
    let document = json!({"action": "A::Action::Read", "kind": "request", "ts": 123});
    // A timestamp is the field most likely to be sent by an optimistic client, and
    // the one whose silent acceptance would be most dangerous: it would look like
    // the caller set when the event happened.
    let http_error = serde_json::from_value::<EventBody>(document.clone()).unwrap_err();
    let wire_error = serde_json::from_value::<WireEvent>(document).unwrap_err();
    assert!(http_error.to_string().contains("ts"), "{http_error}");
    assert!(wire_error.to_string().contains("ts"), "{wire_error}");
}

/// Every verb variant agrees with its wire counterpart. Exhaustive by hand,
/// because the point is to be *forced* to update this list when a verb is added.
#[test]
fn every_verb_serializes_exactly_as_the_wire_verb_it_becomes() {
    let cases: Vec<(VerbBody, WireVerb)> = vec![
        (
            VerbBody::Add {
                policy: "permit(principal, action, resource);".to_string(),
            },
            WireVerb::Add {
                policy: "permit(principal, action, resource);".to_string(),
            },
        ),
        (
            VerbBody::Update {
                id: "abc".to_string(),
                policy: "forbid(principal, action, resource);".to_string(),
            },
            WireVerb::Update {
                id: "abc".to_string(),
                policy: "forbid(principal, action, resource);".to_string(),
            },
        ),
        (
            VerbBody::Delete {
                id: "abc".to_string(),
            },
            WireVerb::Delete {
                id: "abc".to_string(),
            },
        ),
        (
            VerbBody::Reset {
                id: "abc".to_string(),
            },
            WireVerb::Reset {
                id: "abc".to_string(),
            },
        ),
        (VerbBody::DeleteAll, WireVerb::DeleteAll),
        (VerbBody::ResetAll, WireVerb::ResetAll),
        (
            VerbBody::SetActionSchema {
                action_schema: "entity User;".to_string(),
            },
            WireVerb::SetActionSchema {
                action_schema: "entity User;".to_string(),
            },
        ),
        (
            VerbBody::AppendActionSchema {
                fragment: "entity Account;".to_string(),
            },
            WireVerb::AppendActionSchema {
                fragment: "entity Account;".to_string(),
            },
        ),
    ];

    for (body, wire) in cases {
        assert_eq!(
            as_json(&body),
            as_json(&wire),
            "VerbBody and WireVerb have drifted on {body:?}"
        );
    }
}

/// A batch is its verbs, spelled the same way.
#[test]
fn a_batch_serializes_exactly_as_the_wire_batch() {
    let body: BatchBody = serde_json::from_value(json!({
        "verbs": [
            {"verb": "add", "policy": "permit(principal, action, resource);"},
            {"verb": "delete_all"}
        ]
    }))
    .expect("a batch parses");

    let mut wire_verbs: Vec<Value> = Vec::new();
    for verb in body.verbs.clone() {
        wire_verbs.push(as_json(&WireVerb::from(verb)));
    }
    let expected = json!([
        {"verb": "add", "policy": "permit(principal, action, resource);"},
        {"verb": "delete_all"}
    ]);
    assert_eq!(Value::Array(wire_verbs), expected);
}
