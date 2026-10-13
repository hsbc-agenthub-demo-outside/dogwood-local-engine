//! `dogwood-http` — an HTTP + OpenAPI adapter over `dogwood-server`'s sockets.
//!
//! This binary is deliberately thin: it reads configuration, prints what is worth
//! knowing about it, and hands off to [`dogwood_http::serve`]. The interesting
//! decisions live in the library so the integration tests can drive the real
//! routers rather than a re-implementation of them.
//!
//! `healthcheck` is the second subcommand and exists so a container can report its
//! own health **without `curl` in the image** — see [`dogwood_http::healthcheck`]
//! for why that trade is worth making.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Args, Parser, Subcommand, ValueEnum};
use dogwood_http::engine::Engine;
use dogwood_http::{Config, Plane};

/// The engine's default state directory, matching `dogwood-server`'s own default so
/// the two agree unless told otherwise.
fn default_store_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".dogwood")
}

#[derive(Parser)]
#[command(
    name = "dogwood-http",
    about = "An HTTP + OpenAPI adapter in front of the dogwood-server Unix-socket API",
    long_about = "Publishes dogwood-server's socket API over HTTP, with an OpenAPI \
                  document and a Swagger UI for each of the engine's two planes.\n\n\
                  This adapter issues verdicts; it does not enforce them, and it is \
                  not the engine. The engine must be running separately: run \
                  `dogwood-server --dir <same dir> run` first.\n\n\
                  A deny is an HTTP 403, not a 200 with \"allowed\": false, so a \
                  caller that proceeds on 2xx is fail-closed by construction.",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Serve one or both HTTP planes.
    Serve(ServeArgs),

    /// Probe a running adapter and exit 0 if it is healthy.
    ///
    /// For container healthchecks. The image therefore needs no `curl` and no apt
    /// repository at build time.
    Healthcheck(HealthcheckArgs),
}

#[derive(Args)]
struct ServeArgs {
    /// Which listener(s) to run. `data` is the face the monitored agent sees;
    /// `control` authors policy and requires a bearer token.
    ///
    /// `both` serves both from one process, which means the process taking the
    /// agent's events can also rewrite the rules — the split the engine's two
    /// sockets existed to provide. It says so at startup.
    #[arg(long, value_enum, default_value_t = PlaneArg::Both)]
    plane: PlaneArg,

    /// The engine's state directory — the same `--dir` given to `dogwood-server
    /// run`. Used only to derive the two socket paths below; the store itself is
    /// never opened.
    #[arg(long)]
    store_dir: Option<PathBuf>,

    /// The engine's data socket, overriding the one derived from `--store-dir`.
    ///
    /// For deployments where this adapter must reach the data socket without being
    /// able to traverse the engine's state directory at all — a container that
    /// mounts only the engine's `run/` directory, for instance, which is the only
    /// way a different uid can reach the socket (the state directory is mode 0700,
    /// so anything below it is unreachable to another principal).
    #[arg(long)]
    data_socket: Option<PathBuf>,

    /// The engine's control socket, overriding the one derived from `--store-dir`.
    #[arg(long)]
    control_socket: Option<PathBuf>,

    /// Address for the data plane.
    #[arg(long, default_value = "127.0.0.1:8080")]
    data_bind: std::net::SocketAddr,

    /// Address for the control plane. Loopback unless you mean it.
    #[arg(long, default_value = "127.0.0.1:8081")]
    control_bind: std::net::SocketAddr,

    /// How long to wait for the engine before answering 504.
    ///
    /// The engine's own floor is one fsync per event (~1.2ms), so this is a
    /// liveness bound, not a latency budget. Raising it does not make a stuck engine
    /// fast.
    #[arg(long, default_value_t = 5000)]
    engine_timeout_ms: u64,

    /// Acknowledge binding the control plane to a non-loopback address.
    ///
    /// A container must listen on 0.0.0.0 for a published port to reach it, so the
    /// honest Docker configuration is never loopback-only inside. Passing this says
    /// the decision was made deliberately: it downgrades the startup warning to a
    /// notice, and does not remove the requirement that the port be published
    /// narrowly.
    #[arg(long)]
    allow_non_loopback_control: bool,
}

#[derive(Args)]
struct HealthcheckArgs {
    /// The URL to probe, for example `http://127.0.0.1:8080/healthz`.
    url: String,

    /// How long to wait for an answer.
    #[arg(long, default_value_t = 3000)]
    timeout_ms: u64,
}

/// `clap` cannot derive on a type from another crate, so the plane is mirrored here
/// and converted. Keeping the wire-facing enum in the library keeps the CLI free to
/// change without touching the routers.
#[derive(Debug, Clone, Copy, ValueEnum)]
enum PlaneArg {
    Data,
    Control,
    Both,
}

impl From<PlaneArg> for Plane {
    fn from(arg: PlaneArg) -> Self {
        match arg {
            PlaneArg::Data => Plane::Data,
            PlaneArg::Control => Plane::Control,
            PlaneArg::Both => Plane::Both,
        }
    }
}

fn main() -> std::process::ExitCode {
    let cli = Cli::parse();

    match &cli.command {
        // The healthcheck runs before the logger is installed on purpose: a probe
        // is plumbing, and logging inside a healthcheck is how a healthy container
        // fills a log with noise.
        Command::Healthcheck(args) => match dogwood_http::healthcheck::check(
            &args.url,
            Duration::from_millis(args.timeout_ms),
        ) {
            Ok(()) => {
                println!("ok: {}", args.url);
                std::process::ExitCode::SUCCESS
            }
            Err(message) => {
                eprintln!("dogwood-http: unhealthy: {message}");
                std::process::ExitCode::FAILURE
            }
        },
        Command::Serve(args) => {
            install_logger();
            match build_config(args) {
                Ok(config) => match config.validate() {
                    Ok(()) => {
                        for (level, message) in config.advisories() {
                            match level {
                                "warn" => tracing::warn!("{message}"),
                                _ => tracing::info!("{message}"),
                            }
                        }
                        match run(config) {
                            Ok(()) => std::process::ExitCode::SUCCESS,
                            Err(message) => fail(&message),
                        }
                    }
                    Err(message) => fail(&message),
                },
                Err(message) => fail(&message),
            }
        }
    }
}

fn install_logger() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("DOGWOOD_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("dogwood_http=info,warn")),
        )
        .with_writer(std::io::stderr)
        .init();
}

fn fail(message: &str) -> std::process::ExitCode {
    eprintln!("dogwood-http: {message}");
    std::process::ExitCode::FAILURE
}

fn build_config(cli: &ServeArgs) -> Result<Config, String> {
    let conventional = Engine::from_store_dir(
        cli.store_dir.clone().unwrap_or_else(default_store_dir),
        Duration::from_millis(cli.engine_timeout_ms),
    );
    Ok(Config {
        plane: cli.plane.into(),
        data_socket: cli
            .data_socket
            .clone()
            .unwrap_or_else(|| conventional.data_socket().to_path_buf()),
        control_socket: cli
            .control_socket
            .clone()
            .unwrap_or_else(|| conventional.control_socket().to_path_buf()),
        data_bind: cli.data_bind,
        control_bind: cli.control_bind,
        data_token: env_token("DOGWOOD_DATA_TOKEN")?,
        control_token: match env_token("DOGWOOD_CONTROL_TOKEN")? {
            Some(token) => Some(token),
            // A file is the deployment-friendly form: it keeps the secret out of
            // the process environment, where `/proc/<pid>/environ` and `ps` can read
            // it, and out of `docker inspect`.
            None => token_file()?,
        },
        engine_timeout: Duration::from_millis(cli.engine_timeout_ms),
        acknowledge_non_loopback_control: cli.allow_non_loopback_control,
    })
}

/// Read a token from the environment, treating empty as absent.
fn env_token(name: &str) -> Result<Option<Arc<str>>, String> {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => Ok(Some(Arc::from(value.as_str()))),
        Ok(_) => Err(format!("{name} is set but empty")),
        Err(_) => Ok(None),
    }
}

/// Read the control token from `DOGWOOD_CONTROL_TOKEN_FILE`, if set.
fn token_file() -> Result<Option<Arc<str>>, String> {
    let Ok(path) = std::env::var("DOGWOOD_CONTROL_TOKEN_FILE") else {
        return Ok(None);
    };
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read DOGWOOD_CONTROL_TOKEN_FILE {path}: {e}"))?;
    // A file written by a human or an editor ends in a newline; a file written by
    // Docker's secret machinery does not. Trimming means both work, and a token
    // never silently carries a `\n` that makes every comparison fail.
    let token = raw.trim_end_matches(['\n', '\r']);
    if token.is_empty() {
        return Err(format!("DOGWOOD_CONTROL_TOKEN_FILE {path} is empty"));
    }
    Ok(Some(Arc::from(token)))
}

/// Run [`dogwood_http::serve`] on a Tokio runtime.
fn run(config: Config) -> Result<(), String> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("build runtime: {e}"))?
        .block_on(dogwood_http::serve(config))
}
