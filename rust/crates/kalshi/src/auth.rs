//! RSA-PSS-SHA256 signing for Kalshi REST + WS authentication.
//!
//! Matches `python/src/eventcontracts/adapters/venues/kalshi/client.py`
//! byte-for-byte:
//!   message = f"{timestamp_ms}{METHOD}{path}".encode()
//!   sig = base64( RSA-PSS-SHA256( privkey, message, mgf1=sha256, salt_len=32 ) )
//!   headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
//!
//! Both PKCS#1 (`-----BEGIN RSA PRIVATE KEY-----`) and PKCS#8 PEM are
//! accepted; the loader picks based on the PEM tag.

use base64::Engine;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::SigningKey;
use rsa::sha2::Sha256;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

pub const REST_PROD: &str = "https://external-api.kalshi.com/trade-api/v2";
pub const REST_DEMO: &str = "https://external-api.demo.kalshi.co/trade-api/v2";
pub const WS_PROD: &str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2";
pub const WS_DEMO: &str = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2";

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum KalshiEnv {
    Prod,
    Demo,
}

impl KalshiEnv {
    pub fn from_env_var() -> Self {
        match std::env::var("KALSHI_ENV").ok().as_deref() {
            Some("prod") | Some("production") => KalshiEnv::Prod,
            _ => KalshiEnv::Demo,
        }
    }

    pub fn rest_base(&self) -> &'static str {
        match self {
            KalshiEnv::Prod => REST_PROD,
            KalshiEnv::Demo => REST_DEMO,
        }
    }

    pub fn ws_url(&self) -> &'static str {
        match self {
            KalshiEnv::Prod => WS_PROD,
            KalshiEnv::Demo => WS_DEMO,
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct KalshiEnvironment {
    pub env: KalshiEnv,
}

impl KalshiEnvironment {
    pub fn from_env() -> Self {
        Self {
            env: KalshiEnv::from_env_var(),
        }
    }
    pub fn rest_base(&self) -> &'static str {
        self.env.rest_base()
    }
    pub fn ws_url(&self) -> &'static str {
        self.env.ws_url()
    }
}

#[derive(Debug, Error)]
pub enum SignError {
    #[error("env var KALSHI_API_KEY_ID is missing")]
    MissingKeyId,
    #[error("env var KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM is missing")]
    MissingKeyPath,
    #[error("could not read private key at `{path}`: {source}")]
    ReadKey {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("private key parse failed (tried PKCS#1 and PKCS#8): {0}")]
    ParseKey(String),
    #[error("system clock before UNIX epoch")]
    Clock,
}

#[derive(Clone)]
pub struct KalshiAuth {
    pub api_key_id: String,
    private_key: RsaPrivateKey,
}

impl std::fmt::Debug for KalshiAuth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KalshiAuth")
            .field("api_key_id", &self.api_key_id)
            .field("private_key", &"<redacted>")
            .finish()
    }
}

impl KalshiAuth {
    pub fn from_env() -> Result<Self, SignError> {
        let api_key_id = std::env::var("KALSHI_API_KEY_ID").map_err(|_| SignError::MissingKeyId)?;
        if let Ok(pem) = std::env::var("KALSHI_PRIVATE_KEY_PEM") {
            return Self::from_pem_str(&api_key_id, &pem);
        }
        let path_raw =
            std::env::var("KALSHI_PRIVATE_KEY_PATH").map_err(|_| SignError::MissingKeyPath)?;
        let path = PathBuf::from(path_raw);
        Self::from_pem_file(&api_key_id, &path)
    }

    pub fn from_pem_file(api_key_id: &str, path: &Path) -> Result<Self, SignError> {
        let pem = std::fs::read_to_string(path).map_err(|e| SignError::ReadKey {
            path: path.to_path_buf(),
            source: e,
        })?;
        Self::from_pem_str(api_key_id, &pem)
    }

    pub fn from_pem_str(api_key_id: &str, pem: &str) -> Result<Self, SignError> {
        // Try PKCS#1 first because that's what `ecmodel.txt`-style keys use
        // (header `-----BEGIN RSA PRIVATE KEY-----`). Fall back to PKCS#8.
        let private_key = RsaPrivateKey::from_pkcs1_pem(pem)
            .or_else(|_| RsaPrivateKey::from_pkcs8_pem(pem))
            .map_err(|e| SignError::ParseKey(e.to_string()))?;
        Ok(KalshiAuth {
            api_key_id: api_key_id.to_string(),
            private_key,
        })
    }

    /// Sign the canonical string `{ts_ms}{METHOD}{path}` and return a base64
    /// signature plus the timestamp string that was used.
    pub fn sign(&self, method: &str, path: &str) -> Result<SignedHeaders, SignError> {
        let ts_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| SignError::Clock)?
            .as_millis() as u64;
        self.sign_at(method, path, ts_ms)
    }

    pub fn sign_at(
        &self,
        method: &str,
        path: &str,
        ts_ms: u64,
    ) -> Result<SignedHeaders, SignError> {
        let ts = ts_ms.to_string();
        let message = format!("{ts}{}{path}", method.to_ascii_uppercase());
        let signing_key = SigningKey::<Sha256>::new_with_salt_len(self.private_key.clone(), 32);
        let mut rng = rand_compat::thread_rng();
        let sig = signing_key.sign_with_rng(&mut rng, message.as_bytes());
        let sig_b64 = base64::engine::general_purpose::STANDARD.encode(sig.to_bytes());
        Ok(SignedHeaders {
            api_key_id: self.api_key_id.clone(),
            timestamp_ms: ts,
            signature_b64: sig_b64,
        })
    }
}

#[derive(Clone, Debug)]
pub struct SignedHeaders {
    pub api_key_id: String,
    pub timestamp_ms: String,
    pub signature_b64: String,
}

impl SignedHeaders {
    pub fn as_pairs(&self) -> [(&'static str, String); 3] {
        [
            ("KALSHI-ACCESS-KEY", self.api_key_id.clone()),
            ("KALSHI-ACCESS-TIMESTAMP", self.timestamp_ms.clone()),
            ("KALSHI-ACCESS-SIGNATURE", self.signature_b64.clone()),
        ]
    }
}

// `rsa` 0.9 wants a `CryptoRng + RngCore` from rand_core 0.6. OsRng is the
// simplest CryptoRng available without an extra dep, and is what the `rsa`
// docs recommend for PSS signing.
mod rand_compat {
    pub fn thread_rng() -> rsa::rand_core::OsRng {
        rsa::rand_core::OsRng
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rsa::pkcs1::EncodeRsaPrivateKey;
    use rsa::pss::VerifyingKey;
    use rsa::signature::Verifier;
    use std::sync::{Mutex, OnceLock};

    fn env_lock() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
    }

    fn test_key_pem() -> String {
        let mut rng = rsa::rand_core::OsRng;
        let key = RsaPrivateKey::new(&mut rng, 1024).unwrap();
        key.to_pkcs1_pem(rsa::pkcs1::LineEnding::LF)
            .unwrap()
            .to_string()
    }

    #[test]
    fn env_defaults_to_demo() {
        let _guard = env_lock();
        std::env::remove_var("KALSHI_ENV");
        assert_eq!(KalshiEnv::from_env_var(), KalshiEnv::Demo);
    }

    #[test]
    fn env_picks_prod_when_set() {
        let _guard = env_lock();
        std::env::set_var("KALSHI_ENV", "prod");
        assert_eq!(KalshiEnv::from_env_var(), KalshiEnv::Prod);
        std::env::remove_var("KALSHI_ENV");
    }

    #[test]
    fn loads_pkcs1_pem() {
        let pem = test_key_pem();
        let auth = KalshiAuth::from_pem_str("kid-1", &pem).unwrap();
        assert_eq!(auth.api_key_id, "kid-1");
    }

    #[test]
    fn loads_inline_pem_from_env() {
        let _guard = env_lock();
        let pem = test_key_pem();
        std::env::set_var("KALSHI_API_KEY_ID", "kid-inline");
        std::env::set_var("KALSHI_PRIVATE_KEY_PEM", &pem);
        std::env::remove_var("KALSHI_PRIVATE_KEY_PATH");

        let auth = KalshiAuth::from_env().unwrap();

        assert_eq!(auth.api_key_id, "kid-inline");
        std::env::remove_var("KALSHI_API_KEY_ID");
        std::env::remove_var("KALSHI_PRIVATE_KEY_PEM");
    }

    #[test]
    fn sign_at_produces_pss_signature_verifiable_with_matching_key() {
        let pem = test_key_pem();
        let priv_key = RsaPrivateKey::from_pkcs1_pem(&pem).unwrap();
        let auth = KalshiAuth {
            api_key_id: "kid".into(),
            private_key: priv_key.clone(),
        };
        let signed = auth.sign_at("GET", "/markets", 1_700_000_000_000).unwrap();
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&signed.signature_b64)
            .unwrap();
        let verifying = VerifyingKey::<Sha256>::new_with_salt_len(priv_key.to_public_key(), 32);
        let sig = rsa::pss::Signature::try_from(bytes.as_slice()).unwrap();
        let msg = b"1700000000000GET/markets";
        verifying.verify(msg, &sig).expect("signature must verify");
    }

    #[test]
    fn signed_headers_keys_are_canonical() {
        let pem = test_key_pem();
        let auth = KalshiAuth::from_pem_str("kid", &pem).unwrap();
        let signed = auth.sign_at("get", "/markets", 1).unwrap();
        let pairs = signed.as_pairs();
        let keys: Vec<&str> = pairs.iter().map(|(k, _)| *k).collect();
        assert_eq!(
            keys,
            vec![
                "KALSHI-ACCESS-KEY",
                "KALSHI-ACCESS-TIMESTAMP",
                "KALSHI-ACCESS-SIGNATURE",
            ]
        );
    }
}
