use crate::ghost::{GhostBundle, GhostError, GhostKey};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use thiserror::Error;
use zeroize::Zeroizing;

#[derive(Debug, Error)]
pub enum GhostStoreError {
    #[error("event ID must be a 64-character hexadecimal hash")]
    InvalidEventId,
    #[error("stored GHOST commitment does not match the signed event")]
    CommitmentMismatch,
    #[error("GHOST bundle has expired")]
    Expired,
    #[error("ghost store I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("ghost bundle serialization failed: {0}")]
    Serialization(#[from] serde_json::Error),
    #[error(transparent)]
    Crypto(#[from] GhostError),
}

pub struct GhostStore {
    base_dir: PathBuf,
}

impl GhostStore {
    pub fn new<P: AsRef<Path>>(path: P) -> std::io::Result<Self> {
        let base_dir = path.as_ref().to_path_buf();
        fs::create_dir_all(&base_dir)?;
        Ok(Self { base_dir })
    }

    fn bundle_path(&self, event_id: &str) -> Result<PathBuf, GhostStoreError> {
        if event_id.len() != 64 || !event_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(GhostStoreError::InvalidEventId);
        }
        Ok(self.base_dir.join(format!("{event_id}.ghost")))
    }

    pub fn save(&self, event_id: &str, bundle: &GhostBundle) -> Result<(), GhostStoreError> {
        let path = self.bundle_path(event_id)?;
        let data = serde_json::to_vec(bundle)?;
        let mut file = File::create(path)?;
        file.write_all(&data)?;
        file.sync_all()?;
        Ok(())
    }

    pub fn inspect(&self, event_id: &str) -> Result<GhostBundle, GhostStoreError> {
        let path = self.bundle_path(event_id)?;
        let serialized_bundle = fs::read(path)?;
        Ok(serde_json::from_slice(&serialized_bundle)?)
    }

    pub fn load(
        &self,
        event_id: &str,
        expected_commitment: &str,
        ghost_key: &GhostKey,
        now: u64,
    ) -> Result<Zeroizing<Vec<u8>>, GhostStoreError> {
        let path = self.bundle_path(event_id)?;
        let serialized_bundle = fs::read(path)?;
        let bundle: GhostBundle = serde_json::from_slice(&serialized_bundle)?;
        if bundle.commitment != expected_commitment {
            return Err(GhostStoreError::CommitmentMismatch);
        }
        if bundle.is_expired(now) {
            return Err(GhostStoreError::Expired);
        }
        Ok(bundle.decrypt(ghost_key, now)?)
    }

    pub fn stored_commitment(&self, event_id: &str) -> Result<String, GhostStoreError> {
        let path = self.bundle_path(event_id)?;
        let serialized_bundle = fs::read(path)?;
        let bundle: GhostBundle = serde_json::from_slice(&serialized_bundle)?;
        Ok(bundle.commitment)
    }

    pub fn expired_bundles(&self, now: u64) -> Result<Vec<String>, GhostStoreError> {
        let mut expired = Vec::new();
        for entry in fs::read_dir(&self.base_dir)? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|extension| extension.to_str()) != Some("ghost") {
                continue;
            }
            let Some(event_id) = path
                .file_stem()
                .and_then(|stem| stem.to_str())
                .map(str::to_owned)
            else {
                continue;
            };
            let bundle: GhostBundle = serde_json::from_slice(&fs::read(&path)?)?;
            if bundle.is_expired(now) {
                expired.push(event_id);
            }
        }
        expired.sort();
        Ok(expired)
    }

    pub fn destroy(&self, event_id: &str) -> Result<(), GhostStoreError> {
        let path = self.bundle_path(event_id)?;
        if !path.exists() {
            return Ok(());
        }

        let mut file = OpenOptions::new().write(true).open(&path)?;
        let mut remaining = file.metadata()?.len();
        let zeros = [0_u8; 8192];
        while remaining > 0 {
            let count = remaining.min(zeros.len() as u64) as usize;
            file.write_all(&zeros[..count])?;
            remaining -= count as u64;
        }
        file.sync_all()?;
        drop(file);

        fs::remove_file(path)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ghost::{create_ghost_bundle, GhostKey};
    use serde_json::{json, Value};

    #[test]
    fn save_load_and_destroy_uses_wrapped_dek_and_event_commitment() {
        let directory = tempfile::tempdir().unwrap();
        let store = GhostStore::new(directory.path()).unwrap();
        let key = GhostKey::new("scope-key-v1", [9_u8; 32]).unwrap();
        let event_id = "a".repeat(64);
        let plaintext = json!({ "system_prompt": "Confidential prompt." });
        let dek = [4_u8; 32];
        let bundle = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();
        let commitment = bundle.commitment.clone();

        store.save(&event_id, &bundle).unwrap();
        let encrypted_file =
            std::fs::read(directory.path().join(format!("{event_id}.ghost"))).unwrap();
        assert!(!String::from_utf8_lossy(&encrypted_file).contains("Confidential prompt."));

        let recovered = store.load(&event_id, &commitment, &key, 0).unwrap();
        let recovered: Value = serde_json::from_slice(&recovered).unwrap();
        assert_eq!(recovered, plaintext);
        assert!(matches!(
            store.load(&event_id, "different-commitment", &key, 0),
            Err(GhostStoreError::CommitmentMismatch)
        ));

        store.destroy(&event_id).unwrap();
        assert!(!directory.path().join(format!("{event_id}.ghost")).exists());
    }

    #[test]
    fn load_enforces_expiry_and_listing_skips_live_bundles() {
        let directory = tempfile::tempdir().unwrap();
        let store = GhostStore::new(directory.path()).unwrap();
        let key = GhostKey::new("scope-key-v1", [9_u8; 32]).unwrap();
        let plaintext = json!({ "record": "private" });
        let dek = [4_u8; 32];
        let live_id = "a".repeat(64);
        let expired_id = "b".repeat(64);
        let unexpired_id = "c".repeat(64);
        let live = create_ghost_bundle(&plaintext, &dek, &key, Some(10)).unwrap();
        let expired = create_ghost_bundle(&plaintext, &dek, &key, Some(5)).unwrap();
        let unexpired = create_ghost_bundle(&plaintext, &dek, &key, None).unwrap();
        let live_commitment = live.commitment.clone();
        let expired_commitment = expired.commitment.clone();
        store.save(&live_id, &live).unwrap();
        store.save(&expired_id, &expired).unwrap();
        store.save(&unexpired_id, &unexpired).unwrap();

        assert!(store.load(&live_id, &live_commitment, &key, 9).is_ok());
        assert!(matches!(
            store.load(&expired_id, &expired_commitment, &key, 5),
            Err(GhostStoreError::Expired)
        ));
        assert_eq!(store.expired_bundles(5).unwrap(), vec![expired_id.clone()]);
        assert_eq!(
            store.expired_bundles(10).unwrap(),
            vec![live_id, expired_id]
        );
    }

    #[test]
    fn event_id_is_validated_before_building_a_store_path() {
        let directory = tempfile::tempdir().unwrap();
        let store = GhostStore::new(directory.path()).unwrap();

        assert!(matches!(
            store.destroy("../../outside"),
            Err(GhostStoreError::InvalidEventId)
        ));
    }
}
