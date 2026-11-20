use crate::agent_generator::timestamp_now;
use crate::events::{create_signed_event, Event};
use crate::ghost::{create_ghost_bundle, shred_key, zeroize_json_value, GhostError, GhostKey};
use crate::ghost_store::{GhostStore, GhostStoreError};
use crate::identity::Identity;
use crate::store::{EventStore, FileEventStore, StoreError};
use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use thiserror::Error;
use zeroize::{Zeroize, Zeroizing};

#[derive(Debug, Error)]
pub enum GhostVaultError {
    #[error("{0}")]
    Invalid(String),
    #[error("record has not been released yet")]
    NotYetReleased,
    #[error("vault signing identity is missing while the event store is non-empty")]
    MissingIdentity,
    #[error("event verification failed: {0}")]
    EventVerification(String),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    GhostStore(#[from] GhostStoreError),
    #[error(transparent)]
    Ghost(#[from] GhostError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SealResult {
    pub event_id: String,
    pub commitment: String,
    pub release_logical: Option<u64>,
    pub expires_logical: Option<u64>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct VaultListEntry {
    pub event_id: String,
    pub title: String,
    pub commitment: String,
    pub release_logical: Option<u64>,
    pub expires_logical: Option<u64>,
    pub status: String,
}

pub struct GhostVault {
    event_store: FileEventStore,
    ghost_store: GhostStore,
    identity: Identity,
}

struct ZeroizingValue(Value);

impl Drop for ZeroizingValue {
    fn drop(&mut self) {
        zeroize_json_value(&mut self.0);
    }
}

impl GhostVault {
    pub fn open(
        store_path: impl AsRef<Path>,
        vault_path: impl AsRef<Path>,
        identity_path: impl AsRef<Path>,
    ) -> Result<Self, GhostVaultError> {
        create_parent(store_path.as_ref())?;
        create_parent(identity_path.as_ref())?;
        let event_store = FileEventStore::open(store_path)?;
        let identity = load_or_create_identity(identity_path, &event_store)?;
        let ghost_store = GhostStore::new(vault_path)?;
        Ok(Self {
            event_store,
            ghost_store,
            identity,
        })
    }

    pub fn seal(
        &mut self,
        input_path: impl AsRef<Path>,
        scope: &str,
        key_path: impl AsRef<Path>,
        release_logical: Option<u64>,
        expires_logical: Option<u64>,
        title: Option<&str>,
    ) -> Result<SealResult, GhostVaultError> {
        let input_path = input_path.as_ref();
        let bytes = Zeroizing::new(fs::read(input_path)?);
        let title = title
            .map(str::to_owned)
            .or_else(|| {
                input_path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .map(str::to_owned)
            })
            .unwrap_or_else(|| "Untitled".to_owned());
        let (data, data_encoding) = match std::str::from_utf8(&bytes) {
            Ok(text) => (Zeroizing::new(text.to_owned()), "utf8"),
            Err(_) => (Zeroizing::new(base64_encode(&bytes)), "base64"),
        };
        let record = ZeroizingValue(json!({
            "title": title,
            "kind": "record",
            "data": data.as_str(),
            "data_encoding": data_encoding,
        }));
        let key_bytes = read_key(key_path)?;
        let key = GhostKey::new(scope, *key_bytes)?;
        let mut dek = Zeroizing::new([0_u8; 32]);
        OsRng
            .try_fill_bytes(&mut *dek)
            .map_err(|error| GhostVaultError::Invalid(error.to_string()))?;
        let bundle_result = create_ghost_bundle(&record.0, &dek, &key, expires_logical);
        shred_key(&mut dek);
        let bundle = bundle_result?;

        let commitment = bundle.commitment.clone();
        let event = self.new_event(
            "VAULT_SEALED",
            json!({
                "title": title,
                "commitment": commitment,
                "release_logical": release_logical,
                "expires_logical": expires_logical,
                "scope": scope,
            }),
        )?;
        self.ghost_store.save(&event.event_id, &bundle)?;
        if let Err(error) = self.event_store.append(event.clone()) {
            let _ = self.ghost_store.destroy(&event.event_id);
            return Err(error.into());
        }
        Ok(SealResult {
            event_id: event.event_id,
            commitment,
            release_logical,
            expires_logical,
        })
    }

    pub fn open_record(
        &mut self,
        event_id: &str,
        key_path: impl AsRef<Path>,
        now: u64,
        output_path: Option<&Path>,
    ) -> Result<Value, GhostVaultError> {
        let event = self.seal_event(event_id)?;
        self.verify_event(&event)?;
        let expires_logical = optional_u64(&event.payload, "expires_logical")?;
        if expires_logical.is_some_and(|expires| now >= expires) {
            return Err(GhostStoreError::Expired.into());
        }
        let release_logical = optional_u64(&event.payload, "release_logical")?;
        if release_logical.is_some_and(|release| now < release) {
            return Err(GhostVaultError::NotYetReleased);
        }
        let scope = required_string(&event.payload, "scope")?;
        let commitment = required_string(&event.payload, "commitment")?;
        let key_bytes = read_key(key_path)?;
        let key = GhostKey::new(scope, *key_bytes)?;
        let plaintext = self.ghost_store.load(event_id, commitment, &key, now)?;
        let record = ZeroizingValue(serde_json::from_slice(&plaintext)?);
        if required_string(&record.0, "kind")? != "record" {
            return Err(GhostVaultError::Invalid(
                "sealed payload is not a record".into(),
            ));
        }
        let encoded = required_string(&record.0, "data")?;
        let encoding = required_string(&record.0, "data_encoding")?;
        let data = match encoding {
            "utf8" => Zeroizing::new(encoded.as_bytes().to_vec()),
            "base64" => Zeroizing::new(base64_decode(encoded)?),
            _ => {
                return Err(GhostVaultError::Invalid(
                    "unsupported record encoding".into(),
                ))
            }
        };
        let response = if let Some(output_path) = output_path {
            create_parent(output_path)?;
            fs::write(output_path, &data)?;
            json!({ "event_id": event_id, "written_to": output_path })
        } else {
            json!({ "event_id": event_id, "record": record.0.clone() })
        };
        let opened = self.new_event(
            "VAULT_OPENED",
            json!({
                "commitment": commitment,
                "opener_id": self.identity.agent_id,
            }),
        )?;
        self.event_store.append(opened)?;
        Ok(response)
    }

    pub fn shred(&mut self, event_id: &str) -> Result<(), GhostVaultError> {
        let event = self.seal_event(event_id)?;
        self.verify_event(&event)?;
        let commitment = required_string(&event.payload, "commitment")?;
        self.ghost_store.destroy(event_id)?;
        let shredded = self.new_event(
            "VAULT_SHREDDED",
            json!({
                "sealed_event_id": event_id,
                "commitment": commitment,
            }),
        )?;
        self.event_store.append(shredded)?;
        Ok(())
    }

    pub fn list(&self, now: Option<u64>) -> Result<Vec<VaultListEntry>, GhostVaultError> {
        let events = self.event_store.all()?;
        let effective_now = now.unwrap_or_else(|| {
            events
                .iter()
                .map(|event| event.logical_clock)
                .max()
                .unwrap_or(0)
        });
        let shredded: std::collections::HashSet<&str> = events
            .iter()
            .filter(|event| event.event_type == "VAULT_SHREDDED")
            .filter_map(|event| event.payload["sealed_event_id"].as_str())
            .collect();
        let mut records = Vec::new();
        for event in events
            .iter()
            .filter(|event| event.event_type == "VAULT_SEALED")
        {
            let title = required_string(&event.payload, "title")?.to_owned();
            let commitment = required_string(&event.payload, "commitment")?.to_owned();
            let release_logical = optional_u64(&event.payload, "release_logical")?;
            let expires_logical = optional_u64(&event.payload, "expires_logical")?;
            let status = if shredded.contains(event.event_id.as_str()) {
                "shredded"
            } else if expires_logical.is_some_and(|expires| effective_now >= expires) {
                "expired"
            } else if release_logical.is_none_or(|release| effective_now >= release) {
                "released"
            } else {
                "sealed"
            };
            records.push(VaultListEntry {
                event_id: event.event_id.clone(),
                title,
                commitment,
                release_logical,
                expires_logical,
                status: status.to_owned(),
            });
        }
        records.sort_by(|left, right| left.event_id.cmp(&right.event_id));
        Ok(records)
    }

    pub fn verify(
        &self,
        event_id: &str,
        key_path: impl AsRef<Path>,
    ) -> Result<(), GhostVaultError> {
        let event = self.seal_event(event_id)?;
        self.verify_event(&event)?;
        let scope = required_string(&event.payload, "scope")?;
        let key_bytes = read_key(key_path)?;
        let key = GhostKey::new(scope, *key_bytes)?;
        let expected_commitment = required_string(&event.payload, "commitment")?;
        let bundle = self.ghost_store.inspect(event_id)?;
        if bundle.kek_id != key.key_id() {
            return Err(GhostVaultError::Invalid(
                "stored bundle scope does not match the seal event".into(),
            ));
        }
        if bundle.commitment != expected_commitment {
            return Err(GhostVaultError::Invalid(
                "stored bundle commitment does not match the seal event".into(),
            ));
        }
        Ok(())
    }

    fn seal_event(&self, event_id: &str) -> Result<Event, GhostVaultError> {
        let event = self
            .event_store
            .get(&event_id.to_owned())?
            .ok_or_else(|| GhostVaultError::Invalid(format!("seal event not found: {event_id}")))?;
        if event.event_type != "VAULT_SEALED" {
            return Err(GhostVaultError::Invalid(format!(
                "event is not a VAULT_SEALED event: {event_id}"
            )));
        }
        Ok(event)
    }

    fn verify_event(&self, event: &Event) -> Result<(), GhostVaultError> {
        if event.actor_id != self.identity.agent_id {
            return Err(GhostVaultError::EventVerification(
                "seal event actor does not match the vault identity".into(),
            ));
        }
        event
            .verify(&self.identity.public_key_hex())
            .map_err(|error| GhostVaultError::EventVerification(error.to_string()))
    }

    fn new_event(&self, event_type: &str, payload: Value) -> Result<Event, GhostVaultError> {
        let events = self.event_store.all()?;
        let last = events.iter().max_by_key(|event| event.sequence_number);
        let (sequence_number, logical_clock, parents) = match last {
            Some(last) => (
                last.sequence_number.checked_add(1).ok_or_else(|| {
                    GhostVaultError::Invalid("event sequence number overflow".into())
                })?,
                last.logical_clock.checked_add(1).ok_or_else(|| {
                    GhostVaultError::Invalid("event logical clock overflow".into())
                })?,
                vec![last.event_id.clone()],
            ),
            None => (0, 0, Vec::new()),
        };
        let timestamp = timestamp_now().map_err(GhostVaultError::Invalid)?;
        Ok(create_signed_event(
            &self.identity,
            event_type,
            "MOLT/0.1",
            logical_clock,
            sequence_number,
            timestamp,
            parents,
            payload,
            Vec::new(),
            Vec::new(),
        ))
    }
}

fn load_or_create_identity(
    path: impl AsRef<Path>,
    event_store: &FileEventStore,
) -> Result<Identity, GhostVaultError> {
    let path = path.as_ref();
    if path.exists() {
        return load_identity(path);
    }
    if !event_store.all()?.is_empty() {
        return Err(GhostVaultError::MissingIdentity);
    }
    let identity = Identity::generate("ghost-vault");
    let mut seed = identity.seed_bytes();
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = match options.open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                return load_identity(path);
            }
            Err(error) => return Err(error.into()),
        };
        let encoded = Zeroizing::new(hex::encode(seed));
        file.write_all(encoded.as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        Ok(identity)
    })();
    seed.zeroize();
    result
}

fn load_identity(path: &Path) -> Result<Identity, GhostVaultError> {
    let text = Zeroizing::new(fs::read_to_string(path)?);
    let mut bytes = Zeroizing::new(hex::decode(text.trim()).map_err(|error| {
        GhostVaultError::Invalid(format!("invalid vault identity file: {error}"))
    })?);
    let mut seed: [u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| GhostVaultError::Invalid("vault identity must contain 32 bytes".into()))?;
    bytes.zeroize();
    let identity = Identity::from_seed("ghost-vault", &seed);
    seed.zeroize();
    restrict_file(path)?;
    Ok(identity)
}

fn read_key(path: impl AsRef<Path>) -> Result<Zeroizing<[u8; 32]>, GhostVaultError> {
    let text = Zeroizing::new(fs::read_to_string(path)?);
    let mut bytes =
        Zeroizing::new(hex::decode(text.trim()).map_err(|error| {
            GhostVaultError::Invalid(format!("invalid scope key file: {error}"))
        })?);
    if bytes.len() != 32 {
        return Err(GhostVaultError::Invalid(
            "scope key must contain 32 bytes".into(),
        ));
    }
    let mut key = [0_u8; 32];
    key.copy_from_slice(&bytes);
    bytes.zeroize();
    Ok(Zeroizing::new(key))
}

fn required_string<'a>(value: &'a Value, field: &str) -> Result<&'a str, GhostVaultError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| GhostVaultError::Invalid(format!("missing or invalid {field}")))
}

fn optional_u64(value: &Value, field: &str) -> Result<Option<u64>, GhostVaultError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(number)) => number
            .as_u64()
            .map(Some)
            .ok_or_else(|| GhostVaultError::Invalid(format!("invalid {field}"))),
        _ => Err(GhostVaultError::Invalid(format!("invalid {field}"))),
    }
}

fn create_parent(path: &Path) -> Result<(), GhostVaultError> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

fn restrict_file(path: &Path) -> Result<(), GhostVaultError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0];
        let second = *chunk.get(1).unwrap_or(&0);
        let third = *chunk.get(2).unwrap_or(&0);
        output.push(TABLE[(first >> 2) as usize] as char);
        output.push(TABLE[(((first & 0x03) << 4) | (second >> 4)) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[(((second & 0x0f) << 2) | (third >> 6)) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(third & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    output
}

fn base64_decode(encoded: &str) -> Result<Vec<u8>, GhostVaultError> {
    fn value(byte: u8) -> Option<u8> {
        match byte {
            b'A'..=b'Z' => Some(byte - b'A'),
            b'a'..=b'z' => Some(byte - b'a' + 26),
            b'0'..=b'9' => Some(byte - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let bytes = encoded.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err(GhostVaultError::Invalid(
            "invalid base64 record data".into(),
        ));
    }
    let mut output = Vec::with_capacity(bytes.len() / 4 * 3);
    for index in 0..bytes.len() / 4 {
        let chunk = &bytes[index * 4..index * 4 + 4];
        let last = index + 1 == bytes.len() / 4;
        let a = value(chunk[0])
            .ok_or_else(|| GhostVaultError::Invalid("invalid base64 record data".into()))?;
        let b = value(chunk[1])
            .ok_or_else(|| GhostVaultError::Invalid("invalid base64 record data".into()))?;
        output.push((a << 2) | (b >> 4));
        match (chunk[2], chunk[3]) {
            (b'=', b'=') if last && b & 0x0f == 0 => {}
            (c, b'=') if last => {
                let c = value(c)
                    .filter(|value| value & 0x03 == 0)
                    .ok_or_else(|| GhostVaultError::Invalid("invalid base64 record data".into()))?;
                output.push((b << 4) | (c >> 2));
            }
            (c, d) => {
                let c = value(c)
                    .ok_or_else(|| GhostVaultError::Invalid("invalid base64 record data".into()))?;
                let d = value(d)
                    .ok_or_else(|| GhostVaultError::Invalid("invalid base64 record data".into()))?;
                output.push((b << 4) | (c >> 2));
                output.push((c << 6) | d);
            }
        }
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ghost::GhostError;
    use std::path::Path;
    use std::path::PathBuf;
    use tempfile::tempdir;

    fn setup() -> (tempfile::TempDir, GhostVault, PathBuf, PathBuf) {
        let directory = tempdir().unwrap();
        let store_path = directory.path().join("events.jsonl");
        let vault_path = directory.path().join("vault");
        let identity_path = directory.path().join("identity.key");
        let key_path = directory.path().join("scope.key");
        fs::write(&key_path, hex::encode([11_u8; 32])).unwrap();
        let vault = GhostVault::open(&store_path, &vault_path, &identity_path).unwrap();
        (directory, vault, key_path, vault_path)
    }

    fn seal(
        vault: &mut GhostVault,
        directory: &Path,
        key: &Path,
        release: Option<u64>,
        expiry: Option<u64>,
    ) -> SealResult {
        let input = directory.join("record.md");
        fs::write(&input, "private record contents").unwrap();
        vault
            .seal(
                &input,
                "personal",
                key,
                release,
                expiry,
                Some("Test record"),
            )
            .unwrap()
    }

    #[test]
    fn seal_open_round_trip_keeps_plaintext_off_disk_and_logs_commitment_only() {
        let (directory, mut vault, key_path, vault_path) = setup();
        let sealed = seal(&mut vault, directory.path(), &key_path, None, None);
        let bundle_path = vault_path.join(format!("{}.ghost", sealed.event_id));
        let bundle = fs::read(bundle_path).unwrap();
        assert!(!String::from_utf8_lossy(&bundle).contains("private record contents"));
        let record = vault
            .open_record(&sealed.event_id, &key_path, 0, None)
            .unwrap();
        assert_eq!(record["record"]["data"], "private record contents");
        assert_eq!(record["record"]["kind"], "record");
        let events = vault.event_store.all().unwrap();
        assert_eq!(events[0].event_type, "VAULT_SEALED");
        assert_eq!(events[0].payload["commitment"], sealed.commitment);
        assert!(events[0].payload.get("data").is_none());
        assert_eq!(events[1].event_type, "VAULT_OPENED");
        assert_eq!(events[1].payload["opener_id"], "ghost-vault");
    }

    #[test]
    fn wrong_key_and_tampered_bundle_cannot_open() {
        let (directory, mut vault, key_path, vault_path) = setup();
        let sealed = seal(&mut vault, directory.path(), &key_path, None, None);
        let wrong_key = directory.path().join("wrong.key");
        fs::write(&wrong_key, hex::encode([12_u8; 32])).unwrap();
        assert!(matches!(
            vault.open_record(&sealed.event_id, &wrong_key, 0, None),
            Err(GhostVaultError::GhostStore(GhostStoreError::Crypto(
                GhostError::DekUnwrapFailed
            )))
        ));

        let bundle_path = vault_path.join(format!("{}.ghost", sealed.event_id));
        let mut bundle: Value = serde_json::from_slice(&fs::read(&bundle_path).unwrap()).unwrap();
        let ciphertext = bundle["ciphertext"].as_array_mut().unwrap();
        ciphertext[0] = json!(ciphertext[0].as_u64().unwrap() ^ 1);
        fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
        assert!(matches!(
            vault.open_record(&sealed.event_id, &key_path, 0, None),
            Err(GhostVaultError::GhostStore(GhostStoreError::Crypto(
                GhostError::PayloadDecryptionFailed
            )))
        ));
    }

    #[test]
    fn release_expiry_shred_and_verification_behave_as_requested() {
        let (directory, mut vault, key_path, vault_path) = setup();
        let delayed = seal(&mut vault, directory.path(), &key_path, Some(5), Some(20));
        assert!(matches!(
            vault.open_record(&delayed.event_id, &key_path, 4, None),
            Err(GhostVaultError::NotYetReleased)
        ));
        assert!(vault
            .open_record(&delayed.event_id, &key_path, 5, None)
            .is_ok());
        assert!(matches!(
            vault.open_record(&delayed.event_id, &key_path, 20, None),
            Err(GhostVaultError::GhostStore(GhostStoreError::Expired))
        ));
        assert!(vault.verify(&delayed.event_id, &key_path).is_ok());

        let release_after_expiry =
            seal(&mut vault, directory.path(), &key_path, Some(30), Some(20));
        assert!(matches!(
            vault.open_record(&release_after_expiry.event_id, &key_path, 20, None),
            Err(GhostVaultError::GhostStore(GhostStoreError::Expired))
        ));

        let shredded = seal(&mut vault, directory.path(), &key_path, None, None);
        let bundle_path = vault_path.join(format!("{}.ghost", shredded.event_id));
        vault.shred(&shredded.event_id).unwrap();
        assert!(!bundle_path.exists());
        assert!(vault
            .open_record(&shredded.event_id, &key_path, 0, None)
            .is_err());
        assert_eq!(
            vault
                .list(Some(0))
                .unwrap()
                .iter()
                .find(|entry| entry.event_id == shredded.event_id)
                .unwrap()
                .status,
            "shredded"
        );
    }

    #[test]
    fn verify_rejects_a_tampered_seal_event() {
        let (directory, mut vault, key_path, _) = setup();
        let sealed = seal(&mut vault, directory.path(), &key_path, None, None);
        let store_path = directory.path().join("events.jsonl");
        let mut events: Vec<Event> = fs::read_to_string(&store_path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        events[0].payload["title"] = json!("changed");
        fs::write(
            &store_path,
            events
                .iter()
                .map(serde_json::to_string)
                .collect::<Result<Vec<_>, _>>()
                .unwrap()
                .join("\n")
                + "\n",
        )
        .unwrap();
        assert!(matches!(
            vault.verify(&sealed.event_id, &key_path),
            Err(GhostVaultError::EventVerification(_))
        ));
    }

    #[test]
    fn binary_records_round_trip_through_base64() {
        let (directory, mut vault, key_path, _) = setup();
        let input = directory.path().join("binary.dat");
        let bytes = [0_u8, 255, 1, 2, 128];
        fs::write(&input, bytes).unwrap();
        let sealed = vault
            .seal(&input, "personal", &key_path, None, None, None)
            .unwrap();
        let output = directory.path().join("opened.dat");
        vault
            .open_record(&sealed.event_id, &key_path, 0, Some(&output))
            .unwrap();
        assert_eq!(fs::read(output).unwrap(), bytes);
    }

    #[test]
    fn base64_handles_empty_and_padded_inputs() {
        for bytes in [b"".as_slice(), b"a", b"ab", b"abc", b"abcd", &[0, 255, 1]] {
            assert_eq!(base64_decode(&base64_encode(bytes)).unwrap(), bytes);
        }
        assert!(base64_decode("YR==").is_err());
        assert!(base64_decode("YQ=A").is_err());
    }
}
