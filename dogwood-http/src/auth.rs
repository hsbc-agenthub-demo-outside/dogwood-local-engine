//! Bearer-token authentication — what replaces the engine's kernel-attested uid.
//!
//! # What this is standing in for
//!
//! The engine's control socket authorizes by **peer credential**: the kernel
//! reports the connecting process's uid, the client never supplies it, and it
//! cannot be forged. That is the entire authorization scheme for policy authoring,
//! and its virtue is that there is nothing to implement — "who may author policy"
//! is "which OS user are you", a question the OS already answers and can already
//! revoke.
//!
//! An HTTP listener has none of that. There is no kernel-attested identity on a
//! TCP connection, so a shared secret is the smallest thing that can stand in. It
//! is strictly weaker: it is a bearer token, so it leaks through logs, shell
//! history, and `ps`; it has no expiry unless something else revokes it; and it
//! authenticates a *deployment*, not a person. That is the price of the HTTP
//! surface, and it is paid on the control plane only — the data plane defaults to
//! no token at all, because the engine's data socket is deliberately reachable by
//! anyone who can traverse its directory, and what protects it there is the verb
//! set, not its mode.
//!
//! # Why the comparison is a dependency
//!
//! `==` on `str` short-circuits at the first differing byte, which turns a token
//! check into a byte-at-a-time oracle. `subtle::ConstantTimeEq` is the vetted
//! primitive; five lines of hand-rolled XOR is the classic way to reintroduce the
//! oracle while looking careful.

use std::sync::Arc;

use axum::extract::{Request, State};
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::{HeaderMap, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use subtle::ConstantTimeEq;

use crate::dto::ErrorBody;

/// The control plane's token, if one is configured.
///
/// `None` means "no token required", which is a legitimate configuration for the
/// data plane (loopback binding is its protection, exactly as the socket's mode is
/// not the data socket's protection) and a mistake on the control plane — the
/// binary therefore refuses to start a control listener without one.
#[derive(Debug, Clone)]
pub struct BearerAuth {
    token: Option<Arc<str>>,
}

impl BearerAuth {
    pub fn new(token: Option<Arc<str>>) -> Self {
        BearerAuth { token }
    }

    /// No token configured: every request passes.
    pub fn open() -> Self {
        BearerAuth { token: None }
    }

    pub fn is_enabled(&self) -> bool {
        self.token.is_some()
    }
}

/// Reject a request that does not carry the configured bearer token.
///
/// Applied with `from_fn_with_state`, so the layer carries its own state
/// independently of the router's — which is what makes it possible to mount the
/// same middleware over a private router and then merge public routes (`/docs`,
/// `/healthz`) on top of it, *after* the layer, where they are deliberately
/// uncovered.
pub async fn require_bearer(
    State(auth): State<BearerAuth>,
    request: Request,
    next: Next,
) -> Response {
    let Some(expected) = auth.token else {
        return next.run(request).await;
    };

    match presented_token(request.headers()) {
        Some(presented) if constant_time_eq(&presented, &expected) => next.run(request).await,
        // Missing, malformed, and wrong are one answer on purpose: telling them
        // apart tells an unauthenticated caller which half of the problem to work
        // on.
        _ => {
            tracing::warn!(
                path = %request.uri().path(),
                "control request rejected: missing or invalid bearer token"
            );
            unauthorized()
        }
    }
}

/// The token out of `Authorization: Bearer <token>`, with surrounding whitespace
/// trimmed (HTTP header values may carry it).
fn presented_token(headers: &HeaderMap) -> Option<String> {
    let value = headers.get(AUTHORIZATION)?.to_str().ok()?;
    let (scheme, token) = value.split_once(' ')?;
    if !scheme.eq_ignore_ascii_case("bearer") {
        return None;
    }
    Some(token.trim().to_string())
}

/// Constant-time comparison. Length is allowed to leak (it is a property of the
/// configuration, not of a guess), the bytes are not.
fn constant_time_eq(presented: &str, expected: &str) -> bool {
    presented.as_bytes().ct_eq(expected.as_bytes()).into()
}

fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        [(WWW_AUTHENTICATE, "Bearer")],
        axum::Json(ErrorBody {
            error: "missing or invalid bearer token".to_string(),
            source: "request",
        }),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bearer_token_is_extracted_case_insensitively() {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, "bearer s3cret".parse().unwrap());
        assert_eq!(presented_token(&headers).as_deref(), Some("s3cret"));
        headers.insert(AUTHORIZATION, "Bearer  s3cret ".parse().unwrap());
        assert_eq!(presented_token(&headers).as_deref(), Some("s3cret"));
    }

    /// A token scheme the adapter does not implement is not silently treated as a
    /// bearer token.
    #[test]
    fn a_non_bearer_scheme_is_not_a_token() {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, "Basic czNjcmV0".parse().unwrap());
        assert_eq!(presented_token(&headers), None);
        headers.insert(AUTHORIZATION, "s3cret".parse().unwrap());
        assert_eq!(presented_token(&headers), None);
    }

    #[test]
    fn comparison_rejects_a_prefix_and_a_different_length() {
        assert!(constant_time_eq("abc", "abc"));
        assert!(!constant_time_eq("abc", "abcd"));
        assert!(!constant_time_eq("abc", "abd"));
        assert!(!constant_time_eq("", "abc"));
    }
}
