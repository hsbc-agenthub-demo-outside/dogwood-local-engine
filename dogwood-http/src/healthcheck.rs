//! A minimal HTTP client, for one job: probing `/healthz` from inside a container.
//!
//! # Why this exists instead of a healthcheck that calls `curl`
//!
//! A container that cannot report its own health is one an operator has to guess
//! about, so the healthcheck matters. `curl` would mean a package install in the
//! runtime stage, which means an apt repository the build must reach, a larger
//! image, and a build that fails in exactly the networks where a mirror is
//! needed. The check itself is one `GET` and one status line, so the binary does it
//! and the runtime image stays a base image plus two files.
//!
//! # Deliberately narrow
//!
//! `http://` only, no redirects, no chunked bodies (only the header block is read),
//! no TLS. Every one of those omissions is a reason this must never grow into a
//! general-purpose client: it is a liveness probe, and a probe that can follow a
//! redirect or speak TLS is a probe with a certificate store to keep current.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

/// Probe `url` and return `Ok(())` if it answers 2xx.
pub fn check(url: &str, timeout: Duration) -> Result<(), String> {
    let (authority, path) = split_http_url(url)?;

    // The host is the whole authority including the port, which is also what the
    // `Host` header carries — for a numeric loopback address those are the same
    // string, and for anything else this client is not the right tool anyway.
    let mut addresses = authority
        .to_socket_addrs()
        .map_err(|e| format!("resolve {authority}: {e}"))?;
    let address = addresses
        .next()
        .ok_or_else(|| format!("{authority} resolved to no address"))?;

    let mut stream = TcpStream::connect_timeout(&address, timeout)
        .map_err(|e| format!("connect {address}: {e}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|e| format!("set read timeout: {e}"))?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|e| format!("set write timeout: {e}"))?;

    // HTTP/1.0 so the response cannot be chunked, which removes the last reason to
    // parse a body.
    let request = format!("GET {path} HTTP/1.0\r\nHost: {authority}\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("send request: {e}"))?;

    let head = read_header_block(&mut stream)?;
    let status = parse_status(&head)?;
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!("{url} answered {status}"))
    }
}

/// Split `http://host:port/path` into the authority and the request target.
fn split_http_url(url: &str) -> Result<(String, String), String> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| format!("{url} is not an http:// URL (this probe speaks nothing else)"))?;
    let (authority, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        // No path: the root, which is not the same as an empty request target.
        None => (rest, "/"),
    };
    if authority.is_empty() {
        return Err(format!("{url} has no host"));
    }
    Ok((authority.to_string(), path.to_string()))
}

/// Read until the end of the header block. Bounded, because the peer is a service
/// we are asking about, not one we trust to be well-behaved.
fn read_header_block(stream: &mut TcpStream) -> Result<String, String> {
    const LIMIT: usize = 8 * 1024;
    let mut buffer = Vec::with_capacity(256);
    let mut chunk = [0u8; 256];
    loop {
        let read = stream
            .read(&mut chunk)
            .map_err(|e| format!("read response: {e}"))?;
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if buffer.windows(4).any(|w| w == b"\r\n\r\n") || buffer.len() >= LIMIT {
            break;
        }
    }
    if buffer.is_empty() {
        return Err("the server closed the connection without answering".to_string());
    }
    Ok(String::from_utf8_lossy(&buffer).to_string())
}

/// The status code out of a response's first line.
fn parse_status(head: &str) -> Result<u16, String> {
    head.lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| {
            let first = head.lines().next().unwrap_or("");
            format!("could not read a status code from {first:?}")
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_url_with_a_path_splits_at_the_first_slash() {
        assert_eq!(
            split_http_url("http://127.0.0.1:8080/healthz").unwrap(),
            ("127.0.0.1:8080".to_string(), "/healthz".to_string())
        );
        assert_eq!(
            split_http_url("http://[::1]:8080/v1/ping?x=1").unwrap(),
            ("[::1]:8080".to_string(), "/v1/ping?x=1".to_string())
        );
    }

    /// No path is the root, not an empty request target — the latter is a
    /// malformed request line, and it would fail in a way that looks like the
    /// service being down.
    #[test]
    fn a_url_without_a_path_requests_the_root() {
        assert_eq!(
            split_http_url("http://127.0.0.1:8080").unwrap(),
            ("127.0.0.1:8080".to_string(), "/".to_string())
        );
    }

    #[test]
    fn a_scheme_this_client_cannot_speak_is_refused_rather_than_guessed_at() {
        assert!(split_http_url("https://127.0.0.1:8080/healthz").is_err());
        assert!(split_http_url("127.0.0.1:8080/healthz").is_err());
        assert!(split_http_url("http://").is_err());
    }

    #[test]
    fn a_status_line_is_parsed() {
        assert_eq!(parse_status("HTTP/1.1 200 OK\r\n\r\n").unwrap(), 200);
        assert_eq!(parse_status("HTTP/1.1 502 Bad Gateway\r\n").unwrap(), 502);
        assert!(parse_status("nonsense").is_err());
        assert!(parse_status("").is_err());
    }
}
