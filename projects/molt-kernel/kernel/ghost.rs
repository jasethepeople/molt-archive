use crate::hash::{blake3_hex, canonical_json_bytes};
use chacha20poly1305::{
    aead::{Aead, KeyInit, Payload},
    XChaCha20Poly1305, XNonce,
};
use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use thiserror::Error;
use zeroize::{Zeroize, Zeroizing};

const GHOST_BUNDLE_FORMAT_VERSION: u8 = 2;
const DEK_LEN: usize = 32;
const SALT_LEN: usize = 32;

#[derive(Debug, Error)]
pub enum GhostError {
    #[error("ghost key identifier must contain 1 to 128 printable ASCII characters")]
    InvalidKeyId,
    #[error("unsupported ghost bundle format version {0}")]
    UnsupportedFormat(u8),
    #[error("ghost bundle key ID does not match the provided key")]
    KeyIdMismatch,
    #[error("wrapped DEK is missing or has an invalid length")]
    InvalidWrappedDek,
    #[error("wrapped DEK could not be authenticated")]
    DekUnwrapFailed,
    #[error("DEK could not be wrapped")]
    DekWrapFailed,
    #[error("GHOST payload could not be authenticated")]
    PayloadDecryptionFailed,
    #[error("GHOST payload could not be encrypted")]
    PayloadEncryptionFailed,
    #[error("GHOST bundle has expired")]
    Expired,
    #[error("GHOST payload is missing its salt prefix")]
    MissingSalt,
    #[error("GHOST payload salt does not match bundle metadata")]
    SaltMismatch,
    #[error("GHOST payload is not canonical JSON")]
    NonCanonicalPayload,
    #[error("GHOST payload commitment does not match")]
    CommitmentMismatch,
    #[error("GHOST payload JSON is invalid: {0}")]
    PayloadJson(#[from] serde_json::Error),
    #[error("secure random generator failed: {0}")]
    Random(String),
}

pub struct GhostKey {
    key_id: String,
    key: Zeroizing<[u8; DEK_LEN]>,
}

impl GhostKey {
    pub fn new(key_id: impl Into<String>, key: [u8; DEK_LEN]) -> Result<Self, GhostError> {
        let key_id = key_id.into();
        if key_id.is_empty()
            || key_id.len() > 128
            || !key_id.bytes().all(|byte| byte.is_ascii_graphic())
        {
            return Err(GhostError::InvalidKeyId);
        }
        Ok(Self {
            key_id,
            key: Zeroizing::new(key),
        })
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }
}

fn legacy_bundle_version() -> u8 {
    1
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GhostBundle {
    #[serde(default = "legacy_bundle_version")]
    pub format_version: u8,
    pub commitment: String,
    pub salt: [u8; SALT_LEN],
    pub nonce: [u8; 24],
    pub ciphertext: Vec<u8>,
    #[serde(default)]
    pub kek_id: String,
    #[serde(default)]
    pub dek_wrap_nonce: [u8; 24],
    #[serde(default)]
    pub wrapped_dek: Vec<u8>,
    #[serde(default)]
    pub expires_logical: Option<u64>,
}

pub fn create_ghost_bundle(
    plaintext: &Value,
    dek: &[u8; DEK_LEN],
    ghost_key: &GhostKey,
    expires_logical: Option<u64>,
) -> Result<GhostBundle, GhostError> {
    let mut salt = [0_u8; SALT_LEN];
    let mut nonce = [0_u8; 24];
    let mut dek_wrap_nonce = [0_u8; 24];
    fill_random(&mut salt)?;
    fill_random(&mut nonce)?;
    fill_random(&mut dek_wrap_nonce)?;

    let canonical_payload = Zeroizing::new(canonical_json_bytes(plaintext));
    let commitment = compute_commitment(&salt, &nonce, &canonical_payload);
    let mut encrypted_plaintext =
        Zeroizing::new(Vec::with_capacity(SALT_LEN + canonical_payload.len()));
    encrypted_plaintext.extend_from_slice(&salt);
    encrypted_plaintext.extend_from_slice(&canonical_payload);

    let payload_cipher = XChaCha20Poly1305::new(dek.into());
    let payload_aad = associated_data("molt.ghost.payload.v2", ghost_key.key_id(), &commitment);
    let ciphertext = payload_cipher
        .encrypt(
            &XNonce::from(nonce),
            Payload {
                msg: &encrypted_plaintext,
                aad: &payload_aad,
            },
        )
        .map_err(|_| GhostError::PayloadEncryptionFailed)?;

    let wrapping_cipher = XChaCha20Poly1305::new((&*ghost_key.key).into());
    let wrapping_aad = associated_data("molt.ghost.dek.v2", ghost_key.key_id(), &commitment);
    let wrapped_dek = wrapping_cipher
        .encrypt(
            &XNonce::from(dek_wrap_nonce),
            Payload {
                msg: dek,
                aad: &wrapping_aad,
            },
        )
        .map_err(|_| GhostError::DekWrapFailed)?;

    Ok(GhostBundle {
        format_version: GHOST_BUNDLE_FORMAT_VERSION,
        commitment,
        salt,
        nonce,
        ciphertext,
        kek_id: ghost_key.key_id().to_owned(),
        dek_wrap_nonce,
        wrapped_dek,
        expires_logical,
    })
}

impl GhostBundle {
    pub fn is_expired(&self, now: u64) -> bool {
        self.expires_logical.is_some_and(|expiry| now >= expiry)
    }

    pub fn commitment_matches(&self, plaintext: &Value) -> bool {
        let canonical_payload = Zeroizing::new(canonical_json_bytes(plaintext));
        compute_commitment(&self.salt, &self.nonce, &canonical_payload) == self.commitment
    }

    pub fn decrypt(
        &self,
        ghost_key: &GhostKey,
        now: u64,
    ) -> Result<Zeroizing<Vec<u8>>, GhostError> {
        if self.is_expired(now) {
            return Err(GhostError::Expired);
        }
        if self.format_version != GHOST_BUNDLE_FORMAT_VERSION {
            return Err(GhostError::UnsupportedFormat(self.format_version));
        }
        if self.kek_id != ghost_key.key_id() {
            return Err(GhostError::KeyIdMismatch);
        }
        if self.wrapped_dek.len() != DEK_LEN + 16 {
            return Err(GhostError::InvalidWrappedDek);
        }

        let wrapping_cipher = XChaCha20Poly1305::new((&*ghost_key.key).into());
        let wrapping_aad =
            associated_data("molt.ghost.dek.v2", ghost_key.key_id(), &self.commitment);
        let wrapped_dek = Zeroizing::new(
            wrapping_cipher
                .decrypt(
                    &XNonce::from(self.dek_wrap_nonce),
                    Payload {
                        msg: &self.wrapped_dek,
                        aad: &wrapping_aad,
                    },
                )
                .map_err(|_| GhostError::DekUnwrapFailed)?,
        );
        if wrapped_dek.len() != DEK_LEN {
            return Err(GhostError::InvalidWrappedDek);
        }
        let mut dek = Zeroizing::new([0_u8; DEK_LEN]);
        dek.copy_from_slice(&wrapped_dek);

        let payload_cipher = XChaCha20Poly1305::new((&*dek).into());
        let payload_aad = associated_data(
            "molt.ghost.payload.v2",
            ghost_key.key_id(),
            &self.commitment,
        );
        let encrypted_plaintext = Zeroizing::new(
            payload_cipher
                .decrypt(
                    &XNonce::from(self.nonce),
                    Payload {
                        msg: &self.ciphertext,
                        aad: &payload_aad,
                    },
                )
                .map_err(|_| GhostError::PayloadDecryptionFailed)?,
        );
        if encrypted_plaintext.len() < SALT_LEN {
            return Err(GhostError::MissingSalt);
        }
        if encrypted_plaintext[..SALT_LEN] != self.salt {
            return Err(GhostError::SaltMismatch);
        }

        let payload = &encrypted_plaintext[SALT_LEN..];
        let mut value: Value = serde_json::from_slice(payload)?;
        let canonical_payload = Zeroizing::new(canonical_json_bytes(&value));
        if canonical_payload.as_slice() != payload {
            zeroize_json_value(&mut value);
            return Err(GhostError::NonCanonicalPayload);
        }
        let commitment = compute_commitment(&self.salt, &self.nonce, &canonical_payload);
        zeroize_json_value(&mut value);
        if commitment != self.commitment {
            return Err(GhostError::CommitmentMismatch);
        }

        Ok(Zeroizing::new(payload.to_vec()))
    }
}

pub fn shred_key(dek: &mut [u8; DEK_LEN]) {
    dek.zeroize();
}

fn fill_random(bytes: &mut [u8]) -> Result<(), GhostError> {
    OsRng
        .try_fill_bytes(bytes)
        .map_err(|error| GhostError::Random(error.to_string()))
}

fn associated_data(purpose: &str, key_id: &str, commitment: &str) -> Vec<u8> {
    canonical_json_bytes(&json!({
        "commitment": commitment,
        "key_id": key_id,
        "purpose": purpose,
    }))
}

fn compute_commitment(salt: &[u8; SALT_LEN], nonce: &[u8; 24], canonical_payload: &[u8]) -> String {
    let salt_hex = hex::encode(salt);
    let nonce_hex = hex::encode(nonce);
    let mut commitment_data = Zeroizing::new(Vec::with_capacity(canonical_payload.len() + 160));
    commitment_data.extend_from_slice(b"{\"data\":");
    commitment_data.extend_from_slice(canonical_payload);
    commitment_data.extend_from_slice(b",\"nonce\":\"");
    commitment_data.extend_from_slice(nonce_hex.as_bytes());
    commitment_data.extend_from_slice(b"\",\"salt\":\"");
    commitment_data.extend_from_slice(salt_hex.as_bytes());
    commitment_data.extend_from_slice(b"\"}");
    blake3_hex(&commitment_data)
}

pub(crate) fn zeroize_json_value(value: &mut Value) {
    match value {
        Value::String(string) => string.zeroize(),
        Value::Array(values) => values.iter_mut().for_each(zeroize_json_value),
        Value::Object(values) => values.values_mut().for_each(zeroize_json_value),
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_key() -> GhostKey {
        GhostKey::new("scope-key-v1", [7_u8; DEK_LEN]).unwrap()
    }

    #[test]
    fn ghost_bundle_round_trips_with_wrapped_dek() {
        let key = test_key();
        let plaintext = json!({ "system_prompt": "Keep this prompt confidential." });
        let mut dek = Zeroizing::new([0_u8; DEK_LEN]);
        fill_random(&mut *dek).unwrap();

        let bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();
        assert_eq!(bundle.format_version, GHOST_BUNDLE_FORMAT_VERSION);
        assert_eq!(bundle.kek_id, key.key_id());
        assert_eq!(bundle.wrapped_dek.len(), DEK_LEN + 16);
        assert!(!bundle.ciphertext.is_empty());

        let recovered = bundle.decrypt(&key, 0).unwrap();
        let recovered: Value = serde_json::from_slice(&recovered).unwrap();
        assert_eq!(recovered, plaintext);

        shred_key(&mut dek);
        assert_eq!(*dek, [0_u8; DEK_LEN]);
    }

    #[test]
    fn expiry_is_checked_against_the_caller_logical_clock() {
        let key = test_key();
        let plaintext = json!({ "record": "still live before expiry" });
        let dek = [3_u8; DEK_LEN];
        let bundle = create_ghost_bundle(&plaintext, &dek, &key, Some(10)).unwrap();

        assert!(bundle.decrypt(&key, 9).is_ok());
        assert!(matches!(bundle.decrypt(&key, 10), Err(GhostError::Expired)));
    }

    #[test]
    fn legacy_v2_bundle_without_expiry_remains_unexpired() {
        let key = test_key();
        let plaintext = json!({ "record": "legacy v2" });
        let dek = [3_u8; DEK_LEN];
        let bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();
        let mut value = serde_json::to_value(bundle).unwrap();
        value.as_object_mut().unwrap().remove("expires_logical");
        let bundle: GhostBundle = serde_json::from_value(value).unwrap();

        assert_eq!(bundle.format_version, GHOST_BUNDLE_FORMAT_VERSION);
        assert_eq!(bundle.expires_logical, None);
        assert!(bundle.decrypt(&key, u64::MAX).is_ok());
    }

    #[test]
    fn wrong_ghost_key_cannot_unwrap_dek() {
        let key = test_key();
        let wrong_key = GhostKey::new("scope-key-v1", [8_u8; DEK_LEN]).unwrap();
        let plaintext = json!({ "system_prompt": "secret" });
        let dek = [3_u8; DEK_LEN];
        let bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();

        assert!(matches!(
            bundle.decrypt(&wrong_key, 0),
            Err(GhostError::DekUnwrapFailed)
        ));
    }

    #[test]
    fn key_id_mismatch_is_rejected() {
        let key = test_key();
        let other_key = GhostKey::new("scope-key-v2", [7_u8; DEK_LEN]).unwrap();
        let plaintext = json!({ "system_prompt": "secret" });
        let dek = [3_u8; DEK_LEN];
        let bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();

        assert!(matches!(
            bundle.decrypt(&other_key, 0),
            Err(GhostError::KeyIdMismatch)
        ));
    }

    #[test]
    fn altered_ciphertext_is_rejected() {
        let key = test_key();
        let plaintext = json!({ "system_prompt": "secret" });
        let dek = [3_u8; DEK_LEN];
        let mut bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();
        bundle.ciphertext[0] ^= 1;

        assert!(matches!(
            bundle.decrypt(&key, 0),
            Err(GhostError::PayloadDecryptionFailed)
        ));
    }

    #[test]
    fn legacy_bundle_cannot_be_decrypted_without_wrapped_dek() {
        let legacy: GhostBundle = serde_json::from_value(json!({
            "commitment": "old-commitment",
            "salt": vec![0_u8; SALT_LEN],
            "nonce": vec![0_u8; 24],
            "ciphertext": []
        }))
        .unwrap();

        assert_eq!(legacy.format_version, 1);
        assert!(matches!(
            legacy.decrypt(&test_key(), 0),
            Err(GhostError::UnsupportedFormat(1))
        ));
    }
}
