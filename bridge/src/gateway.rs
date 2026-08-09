use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

use agent_client_protocol::{
    Agent, ByteStreams, Channel, Client, ConnectTo, RawJsonRpcMessage, RawJsonRpcParams,
    TransportBatchEntry, TransportFrame,
};
use agent_client_protocol_http::{AcpHttpServer, CorsOptions, ServerOptions};
use axum::{
    body::Body,
    extract::{Request, State},
    http::{StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use futures::StreamExt;
use subtle::ConstantTimeEq;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;
use tokio_util::compat::{TokioAsyncReadCompatExt, TokioAsyncWriteCompatExt};

use crate::{HANDSHAKE_VERSION, Result, load_endpoint};

#[derive(Clone)]
struct AuthState {
    token: Arc<str>,
}

#[derive(Clone)]
struct DaemonTransport {
    endpoint_path: Arc<PathBuf>,
    workspace: Arc<str>,
}

impl ConnectTo<Client> for DaemonTransport {
    async fn connect_to(self, client: impl ConnectTo<Agent>) -> agent_client_protocol::Result<()> {
        let stream = connect_daemon(&self.endpoint_path)
            .await
            .map_err(internal_error)?;
        let (reader, writer) = stream.into_split();
        let daemon_stream = ByteStreams::new(writer.compat_write(), reader.compat());
        let (daemon, daemon_future) =
            <ByteStreams<_, _> as ConnectTo<Client>>::into_channel_and_future(daemon_stream);
        let (external, external_future) = client.into_channel_and_future();

        let bridge = bridge_with_workspace(external, daemon, self.workspace);
        let ((), (), ()) = futures::try_join!(bridge, daemon_future, external_future)?;
        Ok(())
    }
}

fn internal_error(error: impl std::fmt::Display) -> agent_client_protocol::Error {
    agent_client_protocol::Error::internal_error().data(error.to_string())
}

async fn connect_daemon(path: &Path) -> Result<TcpStream> {
    let endpoint = load_endpoint(path)?;
    let address = format!("{}:{}", endpoint.host, endpoint.port);
    let mut stream = TcpStream::connect(address).await?;
    stream.set_nodelay(true)?;
    stream
        .write_all(format!("{HANDSHAKE_VERSION} {} ACP\n", endpoint.token).as_bytes())
        .await?;
    stream.flush().await?;

    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        reader.read_line(&mut response),
    )
    .await
    .map_err(|_| "timed out during the internal ACP daemon handshake")??;
    let response = response.trim();
    if response != "OK" && !response.starts_with("OK ") {
        return Err(match response {
            "BUSY" => "the local ACP daemon already has an active client".into(),
            "UNAUTHORIZED" => "the local ACP daemon rejected its internal token".into(),
            "" => "the local ACP daemon closed during handshake".into(),
            value => format!("the local ACP daemon rejected the connection: {value}").into(),
        });
    }
    Ok(reader.into_inner())
}

async fn bridge_with_workspace(
    external: Channel,
    daemon: Channel,
    workspace: Arc<str>,
) -> agent_client_protocol::Result<()> {
    let Channel {
        rx: mut external_rx,
        tx: external_tx,
    } = external;
    let Channel {
        rx: mut daemon_rx,
        tx: daemon_tx,
    } = daemon;

    let to_daemon = async move {
        while let Some(mut frame) = external_rx.next().await {
            rewrite_workspace(&mut frame, &workspace)?;
            daemon_tx.unbounded_send(frame).map_err(internal_error)?;
        }
        Ok::<(), agent_client_protocol::Error>(())
    };
    let to_external = async move {
        while let Some(frame) = daemon_rx.next().await {
            external_tx.unbounded_send(frame).map_err(internal_error)?;
        }
        Ok::<(), agent_client_protocol::Error>(())
    };

    futures::try_join!(to_daemon, to_external)?;
    Ok(())
}

fn rewrite_workspace(
    frame: &mut TransportFrame,
    workspace: &str,
) -> agent_client_protocol::Result<()> {
    match frame {
        TransportFrame::Single(message) => rewrite_message_workspace(message, workspace),
        TransportFrame::Malformed { .. } => Ok(()),
        TransportFrame::Batch(batch) => {
            for entry in batch.entries_mut() {
                if let TransportBatchEntry::Message(message) = entry {
                    rewrite_message_workspace(message, workspace)?;
                }
            }
            Ok(())
        }
    }
}

fn rewrite_message_workspace(
    message: &mut RawJsonRpcMessage,
    workspace: &str,
) -> agent_client_protocol::Result<()> {
    let RawJsonRpcMessage::Request(request) = message else {
        return Ok(());
    };
    if !matches!(
        request.method.as_ref(),
        "session/new" | "session/load" | "session/list" | "session/fork"
    ) {
        return Ok(());
    }
    match &mut request.params {
        Some(RawJsonRpcParams::Object(params)) => {
            params.insert("cwd".to_string(), workspace.into());
            Ok(())
        }
        None => {
            request.params = Some(RawJsonRpcParams::Object(serde_json::Map::from_iter([(
                "cwd".to_string(),
                workspace.into(),
            )])));
            Ok(())
        }
        Some(RawJsonRpcParams::Array(_)) => Err(agent_client_protocol::Error::invalid_params()
            .data("session workspace rewriting requires object params")),
    }
}

async fn authorize(State(state): State<AuthState>, request: Request<Body>, next: Next) -> Response {
    let authorized = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|candidate| candidate.as_bytes().ct_eq(state.token.as_bytes()).into());
    if !authorized {
        return (
            StatusCode::UNAUTHORIZED,
            [(header::WWW_AUTHENTICATE, "Bearer")],
            "Unauthorized",
        )
            .into_response();
    }
    next.run(request).await
}

pub(crate) async fn run(
    listen: std::net::SocketAddr,
    endpoint_path: PathBuf,
    workspace: PathBuf,
    token: String,
    path: String,
) -> Result<()> {
    let workspace = workspace
        .to_str()
        .ok_or("gateway workspace path is not valid UTF-8")?
        .to_owned();
    let transport = DaemonTransport {
        endpoint_path: Arc::new(endpoint_path),
        workspace: Arc::from(workspace),
    };
    let router = AcpHttpServer::new(move || transport.clone())
        .with_options(ServerOptions {
            path,
            cors: CorsOptions::disabled(),
            health_endpoint: true,
        })
        .into_router()
        .layer(middleware::from_fn_with_state(
            AuthState {
                token: Arc::from(token),
            },
            authorize,
        ));
    let listener = tokio::net::TcpListener::bind(listen).await?;
    eprintln!("deerflow-acp: gateway listening on http://{listen}");
    axum::serve(listener, router).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use agent_client_protocol::schema::v1::RequestId;
    use axum::{Router, body::Body, http::Request, middleware, routing::get};
    use tower::ServiceExt;

    fn session_request(method: &str, cwd: &str) -> TransportFrame {
        TransportFrame::Single(
            RawJsonRpcMessage::request(
                method.to_string(),
                serde_json::json!({"cwd": cwd}),
                RequestId::Number(1),
            )
            .unwrap(),
        )
    }

    fn cwd(frame: &TransportFrame) -> Option<&str> {
        let TransportFrame::Single(RawJsonRpcMessage::Request(request)) = frame else {
            return None;
        };
        let Some(RawJsonRpcParams::Object(params)) = &request.params else {
            return None;
        };
        params.get("cwd").and_then(serde_json::Value::as_str)
    }

    #[test]
    fn rewrites_remote_session_cwd() {
        let mut frame = session_request("session/new", "/remote/workspace");
        rewrite_workspace(&mut frame, r"D:\local\workspace").unwrap();
        assert_eq!(cwd(&frame), Some(r"D:\local\workspace"));
    }

    #[test]
    fn leaves_non_session_requests_unchanged() {
        let mut frame = TransportFrame::Single(
            RawJsonRpcMessage::request(
                "session/prompt".to_string(),
                serde_json::json!({"sessionId": "one", "cwd": "/remote"}),
                RequestId::Number(2),
            )
            .unwrap(),
        );
        rewrite_workspace(&mut frame, "/local").unwrap();
        assert_eq!(cwd(&frame), Some("/remote"));
    }

    #[tokio::test]
    async fn bearer_token_is_required() {
        let app = Router::new()
            .route("/acp", get(|| async { StatusCode::OK }))
            .layer(middleware::from_fn_with_state(
                AuthState {
                    token: Arc::from("correct-token"),
                },
                authorize,
            ));

        let missing = app
            .clone()
            .oneshot(Request::builder().uri("/acp").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(missing.status(), StatusCode::UNAUTHORIZED);

        let wrong = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/acp")
                    .header(header::AUTHORIZATION, "Bearer wrong-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(wrong.status(), StatusCode::UNAUTHORIZED);

        let allowed = app
            .oneshot(
                Request::builder()
                    .uri("/acp")
                    .header(header::AUTHORIZATION, "Bearer correct-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(allowed.status(), StatusCode::OK);
    }
}
