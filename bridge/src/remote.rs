use agent_client_protocol::{ConnectTo, Stdio};
use agent_client_protocol_http::HttpClient;
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue};

use crate::{Result, gateway_token};

pub(crate) async fn run(endpoint: &str, token_env: &str) -> Result<()> {
    let token = gateway_token(token_env)?;
    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| "gateway token contains characters that are invalid in an HTTP header")?,
    );
    let http = reqwest::Client::builder()
        .default_headers(headers)
        .build()?;
    let transport = HttpClient::with_endpoint_and_client(endpoint, http)?;

    // The parent DeerFlow process remains the logical ACP client on stdio. This
    // process only converts that line transport into the SDK's HTTP + SSE
    // transport and deliberately does not interpret protocol messages.
    transport.connect_to(Stdio::new()).await?;
    Ok(())
}
