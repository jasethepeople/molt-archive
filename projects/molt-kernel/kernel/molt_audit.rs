use crate::chronicle::{self, ChronicleEntry};
use crate::events::{create_signed_event, Event};
use crate::ghost::{create_ghost_bundle, zeroize_json_value, GhostError, GhostKey};
use crate::ghost_store::{GhostStore, GhostStoreError};
use crate::hash::blake3_hex;
use crate::identity::{Identity, IdentityError};
use crate::memory::InMemoryStore;
use crate::observatory::{Observatory, ObservatoryError};
use crate::store::{EventStore, FileEventStore, StoreError};
use crate::witness::WitnessReceipt;
use crate::world::WorldState;
use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;
use zeroize::{Zeroize, Zeroizing};

#[derive(Debug, Error)]
pub enum AuditError {
    #[error("event store: {0}")]
    Store(#[from] StoreError),
    #[error("GHOST store: {0}")]
    GhostStore(#[from] GhostStoreError),
    #[error("identity: {0}")]
    Identity(#[from] IdentityError),
    #[error("Observatory: {0}")]
    Observatory(#[from] ObservatoryError),
    #[error("JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("I/O: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Invalid(String),
    #[error("event {event_id} field {field} is Unavailable: {reason}")]
    Unavailable {
        event_id: String,
        field: String,
        reason: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IngestSummary {
    pub ingested: usize,
    pub sealed: usize,
    pub last_event_id: Option<String>,
    pub actor_pubkey: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VerifySummary {
    pub verdict: String,
    pub events: usize,
    pub first_event: Option<String>,
    pub last_event: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReplaySummary {
    pub at: String,
    pub state_hash: String,
    pub events_replayed: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EvidenceEvent {
    pub event_id: String,
    pub event_type: String,
    pub actor_id: String,
    pub logical_clock: u64,
    pub sequence_number: u64,
    pub commitment_or_hash: String,
    pub signature: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EvidencePackage {
    pub events: Vec<EvidenceEvent>,
    pub chain: Vec<String>,
    pub final_hash: String,
    pub witness_receipt: Option<WitnessReceipt>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuditReport {
    pub events: usize,
    pub by_event_type: BTreeMap<String, usize>,
    pub by_actor: BTreeMap<String, usize>,
    pub first_event: Option<String>,
    pub last_event: Option<String>,
    pub sealed_fields: usize,
    pub final_state_hash: String,
    pub chronicle: Vec<ChronicleEntry>,
}

struct ZeroizingValue(Value);

impl Drop for ZeroizingValue {
    fn drop(&mut self) {
        zeroize_json_value(&mut self.0);
    }
}

pub struct MoltAudit {
    store: FileEventStore,
    ghost_store: GhostStore,
}

impl MoltAudit {
    pub fn open(
        store_path: impl AsRef<Path>,
        ghost_path: impl AsRef<Path>,
    ) -> Result<Self, AuditError> {
        let store_path = store_path.as_ref();
        ensure_parent(store_path)?;
        Ok(Self {
            store: FileEventStore::open(store_path)?,
            ghost_store: GhostStore::new(ghost_path)?,
        })
    }

    pub fn ingest_file(
        &mut self,
        input_path: impl AsRef<Path>,
        identity: &Identity,
        ghost_key: &GhostKey,
        seal_fields: &[String],
    ) -> Result<IngestSummary, AuditError> {
        let input = File::open(input_path)?;
        let mut ingested = 0usize;
        let mut sealed = 0usize;
        let mut last_event_id = None;
        for (line_index, line) in BufReader::new(input).lines().enumerate() {
            let mut line = Zeroizing::new(line?);
            if line.trim().is_empty() {
                continue;
            }
            let mut record: Value = serde_json::from_str(&line).map_err(|error| {
                AuditError::Invalid(format!("input line {}: {error}", line_index + 1))
            })?;
            line.zeroize();
            let record_type = record
                .get("type")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| {
                    AuditError::Invalid(format!("input line {}: missing type", line_index + 1))
                })?
                .to_owned();
            let payload = record
                .get_mut("payload")
                .and_then(Value::as_object_mut)
                .ok_or_else(|| {
                    AuditError::Invalid(format!(
                        "input line {}: payload must be an object",
                        line_index + 1
                    ))
                })?;
            let mut pending_bundles = Vec::new();
            let mut sealed_this_record = 0usize;
            for field in seal_fields {
                let Some(value) = payload.remove(field) else {
                    continue;
                };
                let secret = ZeroizingValue(value);
                let mut dek = Zeroizing::new([0_u8; 32]);
                OsRng
                    .try_fill_bytes(&mut *dek)
                    .map_err(|error| AuditError::Invalid(error.to_string()))?;
                let bundle = create_ghost_bundle(&secret.0, &dek, ghost_key, None)
                    .map_err(|error| AuditError::Invalid(error.to_string()))?;
                let payload_ref = random_bundle_id()?;
                let commitment = bundle.commitment.clone();
                payload.insert(
                    field.clone(),
                    json!({
                        "sealed": true,
                        "commitment": commitment,
                        "payload_ref": payload_ref,
                    }),
                );
                pending_bundles.push((payload_ref, bundle));
                sealed_this_record += 1;
            }
            let mut event_payload = json!({
                "type": record_type,
                "payload": Value::Object(std::mem::take(payload)),
                "actor_pubkey": identity.public_key_hex(),
            });
            let event =
                self.prepare_event(identity, "AUDIT_EVENT", std::mem::take(&mut event_payload))?;
            let mut saved: Vec<String> = Vec::new();
            for (payload_ref, bundle) in &pending_bundles {
                if let Err(error) = self.ghost_store.save(payload_ref, bundle) {
                    for saved_ref in saved {
                        let _ = self.ghost_store.destroy(&saved_ref);
                    }
                    return Err(error.into());
                }
                saved.push(payload_ref.clone());
            }
            if let Err(error) = self.store.append(event.clone()) {
                for saved_ref in saved {
                    let _ = self.ghost_store.destroy(&saved_ref);
                }
                return Err(error.into());
            }
            ingested += 1;
            sealed += sealed_this_record;
            last_event_id = Some(event.event_id);
        }
        Ok(IngestSummary {
            ingested,
            sealed,
            last_event_id,
            actor_pubkey: identity.public_key_hex(),
        })
    }

    pub fn verify(&self) -> Result<VerifySummary, AuditError> {
        let events = self.verified_events()?;
        Ok(VerifySummary {
            verdict: "VERIFIED".into(),
            events: events.len(),
            first_event: events.first().map(|event| event.event_id.clone()),
            last_event: events.last().map(|event| event.event_id.clone()),
        })
    }

    pub fn replay_at(&self, target: &str) -> Result<ReplaySummary, AuditError> {
        let events = self.verified_events()?;
        let selected = if let Ok(logical_clock) = target.parse::<u64>() {
            events
                .iter()
                .filter(|event| event.logical_clock <= logical_clock)
                .max_by_key(|event| event.sequence_number)
        } else {
            events.iter().find(|event| event.event_id == target)
        };
        let Some(selected) = selected else {
            if target.parse::<u64>().is_ok() {
                return Ok(ReplaySummary {
                    at: target.to_owned(),
                    state_hash: WorldState::default().hash(),
                    events_replayed: 0,
                });
            }
            return Err(AuditError::Invalid(format!("event {target} was not found")));
        };
        let prefix = events
            .iter()
            .filter(|event| event.sequence_number <= selected.sequence_number)
            .cloned()
            .collect::<Vec<_>>();
        self.require_sealed_bundles(&prefix)?;
        let memory = InMemoryStore::default();
        let observatory = Observatory::new(&self.store, &memory);
        let state_hash = observatory.state_hash_at(&selected.event_id)?;
        Ok(ReplaySummary {
            at: target.to_owned(),
            state_hash,
            events_replayed: prefix.len(),
        })
    }

    pub fn export(
        &self,
        output_path: impl AsRef<Path>,
        witness: Option<&Identity>,
    ) -> Result<EvidencePackage, AuditError> {
        let mut events = self.verified_events()?;
        if events.is_empty() {
            return Err(AuditError::Invalid("cannot export an empty ledger".into()));
        }
        let chain = crate::exporter::export_to_camp1(&mut events, "molt-audit-v1");
        let final_hash = chain
            .last()
            .cloned()
            .ok_or_else(|| AuditError::Invalid("export chain is empty".into()))?;
        let final_event_id = events
            .last()
            .map(|event| event.event_id.clone())
            .ok_or_else(|| AuditError::Invalid("export has no final event".into()))?;
        let witness_receipt =
            witness.map(|identity| WitnessReceipt::sign(&final_event_id, &final_hash, identity));
        let evidence = EvidencePackage {
            events: events.iter().map(evidence_event).collect(),
            chain,
            final_hash,
            witness_receipt,
        };
        write_json_atomic(output_path.as_ref(), &evidence)?;
        Ok(evidence)
    }

    pub fn sealed_open(
        &self,
        event_id: &str,
        field: &str,
        ghost_key: &GhostKey,
        now: u64,
    ) -> Result<Value, AuditError> {
        let events = self.verified_events()?;
        let event = events
            .iter()
            .find(|event| event.event_id == event_id)
            .ok_or_else(|| AuditError::Invalid(format!("event {event_id} was not found")))?;
        let value = event
            .payload
            .get("payload")
            .and_then(|payload| payload.get(field))
            .ok_or_else(|| AuditError::Invalid(format!("event {event_id} has no field {field}")))?;
        let descriptor = value
            .as_object()
            .filter(|object| object.get("sealed").and_then(Value::as_bool) == Some(true))
            .ok_or_else(|| {
                AuditError::Invalid(format!("event {event_id} field {field} is not sealed"))
            })?;
        let commitment = descriptor
            .get("commitment")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                AuditError::Invalid(format!("event {event_id} field {field} has no commitment"))
            })?;
        let payload_ref = descriptor
            .get("payload_ref")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                AuditError::Invalid(format!("event {event_id} field {field} has no payload_ref"))
            })?;
        let plaintext = self
            .ghost_store
            .load(payload_ref, commitment, ghost_key, now)
            .map_err(|error| map_ghost_open_error(event_id, field, error))?;
        serde_json::from_slice(&plaintext).map_err(AuditError::from)
    }

    pub fn report(&self) -> Result<AuditReport, AuditError> {
        let events = self.verified_events()?;
        let mut by_event_type = BTreeMap::new();
        let mut by_actor = BTreeMap::new();
        let mut sealed_fields = 0usize;
        for event in &events {
            let event_type = event
                .payload
                .get("type")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or(&event.event_type);
            *by_event_type.entry(event_type.to_owned()).or_insert(0) += 1;
            *by_actor.entry(event.actor_id.clone()).or_insert(0) += 1;
            sealed_fields += count_sealed_fields(&event.payload);
        }
        let state_hash = self.store.snapshot()?.state_hash;
        Ok(AuditReport {
            events: events.len(),
            by_event_type,
            by_actor,
            first_event: events.first().map(|event| event.event_id.clone()),
            last_event: events.last().map(|event| event.event_id.clone()),
            sealed_fields,
            final_state_hash: state_hash,
            chronicle: chronicle::from_events(&events),
        })
    }

    fn verified_events(&self) -> Result<Vec<Event>, AuditError> {
        let events = self.store.all()?;
        let memory = InMemoryStore::default();
        let history = Observatory::new(&self.store, &memory).verify_history()?;
        if !history.valid {
            let first = history.errors.first().cloned().unwrap_or_default();
            return Err(AuditError::Invalid(format!(
                "history verification failed: {first}"
            )));
        }
        for event in &events {
            let public_key = event
                .payload
                .get("actor_pubkey")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    AuditError::Invalid(format!(
                        "event {}: signing public key unavailable",
                        event.event_id
                    ))
                })?;
            event.verify(public_key).map_err(|error| {
                AuditError::Invalid(format!("event {}: {error}", event.event_id))
            })?;
        }
        Ok(events)
    }

    fn require_sealed_bundles(&self, events: &[Event]) -> Result<(), AuditError> {
        for event in events {
            let Some(payload) = event.payload.get("payload").and_then(Value::as_object) else {
                continue;
            };
            for (field, value) in payload {
                let Some(descriptor) = value.as_object() else {
                    continue;
                };
                if descriptor.get("sealed").and_then(Value::as_bool) != Some(true) {
                    continue;
                }
                let Some(payload_ref) = descriptor.get("payload_ref").and_then(Value::as_str)
                else {
                    return Err(AuditError::Unavailable {
                        event_id: event.event_id.clone(),
                        field: field.clone(),
                        reason: "payload_ref missing".into(),
                    });
                };
                let Some(commitment) = descriptor.get("commitment").and_then(Value::as_str) else {
                    return Err(AuditError::Unavailable {
                        event_id: event.event_id.clone(),
                        field: field.clone(),
                        reason: "commitment missing".into(),
                    });
                };
                match self.ghost_store.stored_commitment(payload_ref) {
                    Ok(stored) if stored == commitment => {}
                    Ok(_) => {
                        return Err(AuditError::Invalid(format!(
                            "event {} field {field}: GHOST bundle commitment mismatch",
                            event.event_id
                        )))
                    }
                    Err(GhostStoreError::Io(error))
                        if error.kind() == std::io::ErrorKind::NotFound =>
                    {
                        return Err(AuditError::Unavailable {
                            event_id: event.event_id.clone(),
                            field: field.clone(),
                            reason: "GHOST bundle is missing".into(),
                        })
                    }
                    Err(error) => return Err(error.into()),
                }
            }
        }
        Ok(())
    }

    fn prepare_event(
        &self,
        identity: &Identity,
        event_type: &str,
        payload: Value,
    ) -> Result<Event, AuditError> {
        let events = self.store.all()?;
        let previous = events.iter().max_by_key(|event| event.sequence_number);
        let (logical_clock, sequence_number, parents) = match previous {
            Some(event) => (
                event
                    .logical_clock
                    .checked_add(1)
                    .ok_or_else(|| AuditError::Invalid("logical clock overflow".into()))?,
                event
                    .sequence_number
                    .checked_add(1)
                    .ok_or_else(|| AuditError::Invalid("event sequence overflow".into()))?,
                vec![event.event_id.clone()],
            ),
            None => (0, 0, Vec::new()),
        };
        Ok(create_signed_event(
            identity,
            event_type,
            "MOLT/0.1",
            logical_clock,
            sequence_number,
            crate::agent_generator::timestamp_now().map_err(AuditError::Invalid)?,
            parents,
            payload,
            Vec::new(),
            Vec::new(),
        ))
    }
}

pub fn generate_actor_identity(actor: &str) -> Identity {
    Identity::generate(actor)
}

pub fn load_actor_identity(path: impl AsRef<Path>, actor: &str) -> Result<Identity, AuditError> {
    let mut encoded = Zeroizing::new(fs::read_to_string(path)?);
    let mut decoded = hex::decode(encoded.trim())
        .map_err(|error| AuditError::Invalid(format!("invalid actor key file: {error}")))?;
    encoded.zeroize();
    if decoded.len() != 32 {
        decoded.zeroize();
        return Err(AuditError::Invalid(
            "actor key file must contain 32 bytes in hex".into(),
        ));
    }
    let mut seed = Zeroizing::new([0_u8; 32]);
    seed.copy_from_slice(&decoded);
    decoded.zeroize();
    Ok(Identity::from_seed(actor, &seed))
}

pub fn load_witness_identity(path: impl AsRef<Path>) -> Result<Identity, AuditError> {
    load_actor_identity(path, "molt-audit-witness")
}

fn evidence_event(event: &Event) -> EvidenceEvent {
    let commitments = event
        .payload
        .get("payload")
        .and_then(Value::as_object)
        .map(|payload| {
            payload
                .values()
                .filter_map(|value| {
                    let descriptor = value.as_object()?;
                    (descriptor.get("sealed").and_then(Value::as_bool) == Some(true))
                        .then(|| descriptor.get("commitment").and_then(Value::as_str))
                        .flatten()
                        .map(str::to_owned)
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let commitment_or_hash = match commitments.as_slice() {
        [commitment] => commitment.clone(),
        [] => event.content_hash.clone(),
        _ => blake3_hex(&serde_json::to_vec(&commitments).expect("commitments serialize")),
    };
    EvidenceEvent {
        event_id: event.event_id.clone(),
        event_type: event.event_type.clone(),
        actor_id: event.actor_id.clone(),
        logical_clock: event.logical_clock,
        sequence_number: event.sequence_number,
        commitment_or_hash,
        signature: event.signature.clone(),
    }
}

fn count_sealed_fields(payload: &Value) -> usize {
    payload
        .get("payload")
        .and_then(Value::as_object)
        .map(|object| {
            object
                .values()
                .filter(|value| {
                    value
                        .as_object()
                        .and_then(|descriptor| descriptor.get("sealed"))
                        .and_then(Value::as_bool)
                        == Some(true)
                })
                .count()
        })
        .unwrap_or(0)
}

fn random_bundle_id() -> Result<String, AuditError> {
    let mut bytes = Zeroizing::new([0_u8; 32]);
    OsRng
        .try_fill_bytes(&mut *bytes)
        .map_err(|error| AuditError::Invalid(error.to_string()))?;
    Ok(hex::encode(*bytes))
}

fn map_ghost_open_error(event_id: &str, field: &str, error: GhostStoreError) -> AuditError {
    match error {
        GhostStoreError::Io(error) if error.kind() == std::io::ErrorKind::NotFound => {
            AuditError::Unavailable {
                event_id: event_id.to_owned(),
                field: field.to_owned(),
                reason: "GHOST bundle is missing".into(),
            }
        }
        GhostStoreError::Crypto(GhostError::DekUnwrapFailed) => {
            AuditError::Invalid(format!("event {event_id} field {field}: DekUnwrapFailed"))
        }
        GhostStoreError::Crypto(GhostError::PayloadDecryptionFailed) => AuditError::Invalid(
            format!("event {event_id} field {field}: PayloadDecryptionFailed"),
        ),
        error => AuditError::Invalid(format!("event {event_id} field {field}: {error}")),
    }
}

fn write_json_atomic(path: &Path, value: &impl Serialize) -> Result<(), AuditError> {
    ensure_parent(path)?;
    let mut temporary = path.as_os_str().to_os_string();
    temporary.push(format!(".{}.tmp", std::process::id()));
    let temporary = PathBuf::from(temporary);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    Ok(())
}

fn ensure_parent(path: &Path) -> Result<(), AuditError> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn setup() -> (tempfile::TempDir, MoltAudit, Identity, GhostKey) {
        let directory = tempfile::tempdir().unwrap();
        let audit = MoltAudit::open(
            directory.path().join("events.jsonl"),
            directory.path().join("vault"),
        )
        .unwrap();
        let identity = generate_actor_identity("finance-system");
        let key = GhostKey::new("molt-audit", [7_u8; 32]).unwrap();
        (directory, audit, identity, key)
    }

    fn input_file(directory: &Path, content: &str) -> PathBuf {
        let path = directory.join("input.jsonl");
        fs::write(&path, content).unwrap();
        path
    }

    #[test]
    fn ingest_verify_replay_export_and_report_are_deterministic() {
        let (directory, mut audit, identity, key) = setup();
        let input = input_file(
            directory.path(),
            "{\"type\":\"transaction\",\"payload\":{\"amount\":42,\"account\":\"acct-7\",\"memo\":\"private memo\"}}\n{\"type\":\"access\",\"payload\":{\"user\":\"analyst-1\",\"action\":\"read\"}}\n",
        );
        let summary = audit
            .ingest_file(&input, &identity, &key, &["memo".into()])
            .unwrap();
        assert_eq!(summary.ingested, 2);
        assert_eq!(summary.sealed, 1);
        assert_eq!(audit.verify().unwrap().events, 2);
        let replay = audit.replay_at("1").unwrap();
        let events = audit.store.all().unwrap();
        let independent = crate::world::replay(&events).unwrap().hash();
        assert_eq!(replay.state_hash, independent);
        let evidence_path = directory.path().join("evidence.json");
        audit.export(&evidence_path, None).unwrap();
        let evidence = fs::read_to_string(evidence_path).unwrap();
        assert!(!evidence.contains("private memo"));
        assert!(!evidence.contains("acct-7"));
        assert!(!evidence.contains("payload_ref"));
        assert!(!evidence.contains("sealed"));
        let report = audit.report().unwrap();
        assert_eq!(report.sealed_fields, 1);
        assert_eq!(report.events, 2);
        assert_eq!(report.by_event_type.get("transaction"), Some(&1));
    }

    #[test]
    fn sealed_open_requires_key_and_missing_payload_is_unavailable() {
        let (directory, mut audit, identity, key) = setup();
        let input = input_file(
            directory.path(),
            "{\"type\":\"patient.record\",\"payload\":{\"name\":\"Private Patient\",\"result\":\"stable\"}}\n",
        );
        let summary = audit
            .ingest_file(&input, &identity, &key, &["name".into()])
            .unwrap();
        let event_id = summary.last_event_id.unwrap();
        assert_eq!(
            audit.sealed_open(&event_id, "name", &key, 0).unwrap(),
            json!("Private Patient")
        );
        let wrong = GhostKey::new("molt-audit", [8_u8; 32]).unwrap();
        let wrong_key_error = audit
            .sealed_open(&event_id, "name", &wrong, 0)
            .unwrap_err()
            .to_string();
        assert!(wrong_key_error.contains("DekUnwrapFailed"));
        let event = audit.store.get(&event_id).unwrap().unwrap();
        let payload_ref = event.payload["payload"]["name"]["payload_ref"]
            .as_str()
            .unwrap();
        let bundle_path = directory
            .path()
            .join("vault")
            .join(format!("{payload_ref}.ghost"));
        let mut bundle: Value = serde_json::from_slice(&fs::read(&bundle_path).unwrap()).unwrap();
        let first_byte = bundle["ciphertext"][0].as_u64().unwrap();
        bundle["ciphertext"][0] = Value::from(first_byte ^ 1);
        fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
        let tampered_error = audit
            .sealed_open(&event_id, "name", &key, 0)
            .unwrap_err()
            .to_string();
        assert!(tampered_error.contains("PayloadDecryptionFailed"));
        audit.ghost_store.destroy(payload_ref).unwrap();
        let error = audit
            .sealed_open(&event_id, "name", &key, 0)
            .unwrap_err()
            .to_string();
        assert!(error.contains("Unavailable"));
        let _ = directory;
    }

    #[test]
    fn changed_payload_and_signature_fail_verification_at_the_event() {
        let (directory, mut audit, identity, key) = setup();
        let input = input_file(
            directory.path(),
            "{\"type\":\"access\",\"payload\":{\"action\":\"write\"}}\n",
        );
        let event_id = audit
            .ingest_file(&input, &identity, &key, &[])
            .unwrap()
            .last_event_id
            .unwrap();
        let path = directory.path().join("events.jsonl");
        let mut event = audit.store.get(&event_id).unwrap().unwrap();
        event.payload["payload"]["action"] = Value::String("tampered".into());
        fs::write(
            &path,
            format!("{}\n", serde_json::to_string(&event).unwrap()),
        )
        .unwrap();
        let error = MoltAudit::open(&path, directory.path().join("vault"))
            .unwrap()
            .verify()
            .unwrap_err()
            .to_string();
        assert!(error.contains(&event_id));

        event.payload["payload"]["action"] = Value::String("write".into());
        event.signature = "00".repeat(64);
        fs::write(
            &path,
            format!("{}\n", serde_json::to_string(&event).unwrap()),
        )
        .unwrap();
        let error = MoltAudit::open(&path, directory.path().join("vault"))
            .unwrap()
            .verify()
            .unwrap_err()
            .to_string();
        assert!(error.contains(&event_id));
    }

    #[test]
    fn replay_stops_with_unavailable_when_a_sealed_bundle_is_missing() {
        let (directory, mut audit, identity, key) = setup();
        let input = input_file(
            directory.path(),
            "{\"type\":\"compliance\",\"payload\":{\"evidence\":\"sealed-value\"}}\n",
        );
        let event_id = audit
            .ingest_file(&input, &identity, &key, &["evidence".into()])
            .unwrap()
            .last_event_id
            .unwrap();
        let event = audit.store.get(&event_id).unwrap().unwrap();
        let payload_ref = event.payload["payload"]["evidence"]["payload_ref"]
            .as_str()
            .unwrap();
        audit.ghost_store.destroy(payload_ref).unwrap();
        let error = audit.replay_at("0").unwrap_err().to_string();
        assert!(error.contains("Unavailable"));
    }
}
