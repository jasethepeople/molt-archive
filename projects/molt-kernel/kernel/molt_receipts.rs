use crate::agent_generator::{spawn_agent, timestamp_now};
use crate::events::{create_signed_event, Event};
use crate::exporter::export_to_camp1;
use crate::gas_meter::calculate_ghost_gas;
use crate::ghost::GhostKey;
use crate::ghost_store::GhostStore;
use crate::identity::{Identity, IdentityError};
use crate::memory::InMemoryStore;
use crate::observatory::{Observatory, ObservatoryError};
use crate::store::{EventStore, FileEventStore, StoreError};
use crate::witness::WitnessReceipt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;
use zeroize::Zeroize;

#[derive(Debug, Error)]
pub enum ReceiptError {
    #[error("event store: {0}")]
    Store(#[from] StoreError),
    #[error("GHOST store: {0}")]
    GhostStore(#[from] crate::ghost_store::GhostStoreError),
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
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobStart {
    pub job: String,
    pub agent: String,
    pub agent_pubkey: String,
    pub genesis_event: Event,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkStepResult {
    pub event_id: String,
    pub sequence_number: u64,
    pub gas: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobTotals {
    pub job: String,
    pub steps: u64,
    pub total_gas: u64,
    pub event_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReceiptFile {
    pub job: String,
    pub agent: String,
    pub agent_pubkey: String,
    pub witness_pubkey: String,
    pub events: Vec<Event>,
    pub chain: Vec<String>,
    pub final_hash: String,
    pub witness_receipt: WitnessReceipt,
    pub steps: u64,
    pub total_gas: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VerifiedReceipt {
    pub steps: u64,
    pub total_gas: u64,
    pub final_hash: String,
}

pub struct MoltReceipts {
    store: FileEventStore,
    ghost_store: GhostStore,
    agent_key_directory: PathBuf,
    runner: Identity,
}

impl MoltReceipts {
    pub fn open(
        store_path: impl AsRef<Path>,
        ghost_path: impl AsRef<Path>,
        agent_key_directory: impl AsRef<Path>,
        runner: Identity,
    ) -> Result<Self, ReceiptError> {
        let store_path = store_path.as_ref();
        ensure_parent(store_path)?;
        let agent_key_directory = agent_key_directory.as_ref().to_path_buf();
        fs::create_dir_all(&agent_key_directory)?;
        restrict_directory(&agent_key_directory)?;
        Ok(Self {
            store: FileEventStore::open(store_path)?,
            ghost_store: GhostStore::new(ghost_path)?,
            agent_key_directory,
            runner,
        })
    }

    pub fn start(
        &mut self,
        job: &str,
        agent_name: &str,
        client: &str,
        scope_key: &GhostKey,
        system_prompt: Option<&str>,
    ) -> Result<JobStart, ReceiptError> {
        validate_identifier(job, "job")?;
        if self.find_start(job).is_ok() {
            return Err(ReceiptError::Invalid(format!("job {job} already exists")));
        }
        let prompt = system_prompt.map(str::to_owned).unwrap_or_else(|| {
            format!("You are the agent for job {job}. Follow the task and report your work steps accurately.")
        });
        let spawned = spawn_agent(
            &self.runner,
            &self.ghost_store,
            scope_key,
            agent_name,
            &prompt,
            None,
        )
        .map_err(ReceiptError::Invalid)?;
        let agent_pubkey = spawned.identity.public_key_hex();
        let genesis_event = spawned.genesis_event;
        let key_path = self.agent_key_path(job);
        if let Err(error) = write_identity_seed(&key_path, &spawned.identity) {
            let _ = self.ghost_store.destroy(&genesis_event.event_id);
            return Err(error);
        }
        let payload = json!({
            "job": job,
            "agent": spawned.agent_id,
            "client": client,
            "agent_pubkey": agent_pubkey,
            "genesis_event": genesis_event,
        });
        let event = self.new_event(&spawned.identity, "JOB_STARTED", payload)?;
        if let Err(error) = self.store.append(event) {
            let _ = fs::remove_file(key_path);
            let _ = self.ghost_store.destroy(&genesis_event.event_id);
            return Err(error.into());
        }
        Ok(JobStart {
            job: job.to_owned(),
            agent: spawned.agent_id,
            agent_pubkey,
            genesis_event,
        })
    }

    pub fn log_step(
        &mut self,
        job: &str,
        description: &str,
        input_hash: Option<&str>,
        output_hash: Option<&str>,
        tier: Option<&str>,
    ) -> Result<WorkStepResult, ReceiptError> {
        let start = self.find_start(job)?;
        self.ensure_not_finished(job)?;
        let agent = field_string(&start.payload, "agent", &start.event_id)?;
        let identity = self.load_agent_identity(job, agent)?;
        let mut payload = json!({
            "job": job,
            "step": description,
            "input_hash": input_hash,
            "output_hash": output_hash,
        });
        if let Some(tier) = tier {
            if tier != "GHOST" && tier != "PUBLIC" {
                return Err(ReceiptError::Invalid("tier must be GHOST or PUBLIC".into()));
            }
            payload["tier"] = Value::String(tier.to_owned());
        }
        let gas = calculate_ghost_gas(&payload);
        payload["gas"] = Value::from(gas);
        let event = self.new_event(&identity, "WORK_STEP", payload)?;
        let result = WorkStepResult {
            event_id: event.event_id.clone(),
            sequence_number: event.sequence_number,
            gas,
        };
        self.store.append(event)?;
        Ok(result)
    }

    pub fn finish(&mut self, job: &str) -> Result<JobTotals, ReceiptError> {
        let start = self.find_start(job)?;
        self.ensure_not_finished(job)?;
        let agent = field_string(&start.payload, "agent", &start.event_id)?;
        let agent_pubkey = field_string(&start.payload, "agent_pubkey", &start.event_id)?;
        let identity = self.load_agent_identity(job, agent)?;
        let steps = self.job_events(job)?;
        let (steps_count, total_gas) = verify_step_totals(&steps, agent, agent_pubkey)?;
        let event = self.new_event(
            &identity,
            "JOB_DONE",
            json!({ "job": job, "steps": steps_count, "total_gas": total_gas }),
        )?;
        let result = JobTotals {
            job: job.to_owned(),
            steps: steps_count,
            total_gas,
            event_id: event.event_id.clone(),
        };
        self.store.append(event)?;
        Ok(result)
    }

    pub fn create_receipt(
        &self,
        job: &str,
        witness: &Identity,
        output_path: impl AsRef<Path>,
    ) -> Result<ReceiptFile, ReceiptError> {
        let memory = InMemoryStore::default();
        let history = Observatory::new(&self.store, &memory).verify_history()?;
        if !history.valid {
            return Err(ReceiptError::Invalid(format!(
                "stored history invalid: {}",
                history.errors.first().cloned().unwrap_or_default()
            )));
        }
        let events = self.job_events(job)?;
        let start = self.find_start(job)?;
        let agent = field_string(&start.payload, "agent", &start.event_id)?.to_owned();
        let agent_pubkey =
            field_string(&start.payload, "agent_pubkey", &start.event_id)?.to_owned();
        let (steps, total_gas) = verify_step_totals(&events, &agent, &agent_pubkey)?;
        let done_count = events
            .iter()
            .filter(|event| event.event_type == "JOB_DONE")
            .count();
        if done_count != 1 {
            return Err(ReceiptError::Invalid(format!(
                "job {job} must have exactly one JOB_DONE event"
            )));
        }
        let mut ordered_events = events;
        let chain = export_to_camp1(&mut ordered_events, &format!("job:{job}"));
        let final_hash = chain
            .last()
            .cloned()
            .ok_or_else(|| ReceiptError::Invalid("job receipt has no events".into()))?;
        let final_event_id = ordered_events
            .last()
            .map(|event| event.event_id.clone())
            .ok_or_else(|| ReceiptError::Invalid("job receipt has no events".into()))?;
        let witness_receipt = WitnessReceipt::sign(&final_event_id, &final_hash, witness);
        let receipt = ReceiptFile {
            job: job.to_owned(),
            agent,
            agent_pubkey,
            witness_pubkey: witness.public_key_hex(),
            events: ordered_events,
            chain,
            final_hash,
            witness_receipt,
            steps,
            total_gas,
        };
        write_json_atomic(output_path.as_ref(), &receipt)?;
        Ok(receipt)
    }

    fn find_start(&self, job: &str) -> Result<Event, ReceiptError> {
        self.store
            .all()?
            .into_iter()
            .find(|event| {
                event.event_type == "JOB_STARTED"
                    && event.payload.get("job").and_then(Value::as_str) == Some(job)
            })
            .ok_or_else(|| ReceiptError::Invalid(format!("job {job} was not started")))
    }

    fn job_events(&self, job: &str) -> Result<Vec<Event>, ReceiptError> {
        let events = self
            .store
            .all()?
            .into_iter()
            .filter(|event| event.payload.get("job").and_then(Value::as_str) == Some(job))
            .collect::<Vec<_>>();
        if events.is_empty() {
            return Err(ReceiptError::Invalid(format!("job {job} has no events")));
        }
        Ok(events)
    }

    fn ensure_not_finished(&self, job: &str) -> Result<(), ReceiptError> {
        if self
            .job_events(job)?
            .iter()
            .any(|event| event.event_type == "JOB_DONE")
        {
            return Err(ReceiptError::Invalid(format!(
                "job {job} is already finished"
            )));
        }
        Ok(())
    }

    fn load_agent_identity(&self, job: &str, agent: &str) -> Result<Identity, ReceiptError> {
        read_identity_seed(self.agent_key_path(job), agent)
    }

    fn agent_key_path(&self, job: &str) -> PathBuf {
        let digest = blake3::hash(job.as_bytes()).to_hex().to_string();
        self.agent_key_directory.join(format!("{digest}.seed"))
    }

    fn new_event(
        &self,
        identity: &Identity,
        event_type: &str,
        payload: Value,
    ) -> Result<Event, ReceiptError> {
        let events = self.store.all()?;
        let previous = events.iter().max_by_key(|event| event.sequence_number);
        let (logical_clock, sequence_number, parents) = match previous {
            Some(event) => (
                event
                    .logical_clock
                    .checked_add(1)
                    .ok_or_else(|| ReceiptError::Invalid("logical clock overflow".into()))?,
                event
                    .sequence_number
                    .checked_add(1)
                    .ok_or_else(|| ReceiptError::Invalid("event sequence overflow".into()))?,
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
            timestamp_now().map_err(ReceiptError::Invalid)?,
            parents,
            payload,
            Vec::new(),
            Vec::new(),
        ))
    }
}

pub fn verify_receipt(
    receipt: &ReceiptFile,
    agent_public_key_override: Option<&str>,
    witness_public_key_override: Option<&str>,
) -> Result<VerifiedReceipt, ReceiptError> {
    if receipt.events.is_empty() {
        return Err(ReceiptError::Invalid("receipt contains no events".into()));
    }
    let agent_public_key = agent_public_key_override.unwrap_or(&receipt.agent_pubkey);
    let witness_public_key = witness_public_key_override.unwrap_or(&receipt.witness_pubkey);
    let mut unique = HashSet::new();
    for event in &receipt.events {
        if !unique.insert(event.event_id.as_str()) {
            return Err(event_failure(event, "duplicate event ID"));
        }
        event
            .verify(agent_public_key)
            .map_err(|error| event_failure(event, &error.to_string()))?;
        if event.actor_id != receipt.agent {
            return Err(event_failure(event, "actor does not match receipt agent"));
        }
        if field_string(&event.payload, "job", &event.event_id)? != receipt.job {
            return Err(event_failure(event, "job ID does not match receipt"));
        }
    }
    validate_acyclic(&receipt.events)?;
    let mut ordered_events = receipt.events.clone();
    let chain = export_to_camp1(&mut ordered_events, &format!("job:{}", receipt.job));
    if chain != receipt.chain {
        return Err(ReceiptError::Invalid(format!(
            "event {}: receipt chain does not match deterministic export",
            ordered_events
                .iter()
                .zip(&chain)
                .zip(&receipt.chain)
                .find(|((_, actual), recorded)| actual != recorded)
                .map(|((event, _), _)| event.event_id.as_str())
                .or_else(|| ordered_events.first().map(|event| event.event_id.as_str()))
                .unwrap_or("unknown")
        )));
    }
    let final_hash = chain
        .last()
        .ok_or_else(|| ReceiptError::Invalid("receipt chain is empty".into()))?;
    let final_event = ordered_events
        .last()
        .ok_or_else(|| ReceiptError::Invalid("receipt has no final event".into()))?;
    if &receipt.final_hash != final_hash {
        return Err(event_failure(
            final_event,
            "final hash does not match chain",
        ));
    }
    if receipt.witness_receipt.event_id != final_event.event_id
        || receipt.witness_receipt.prev_hash != receipt.final_hash
    {
        return Err(event_failure(
            final_event,
            "witness receipt target does not match final chain",
        ));
    }
    receipt
        .witness_receipt
        .verify(witness_public_key)
        .map_err(|error| {
            event_failure(final_event, &format!("witness receipt invalid: {error}"))
        })?;
    let (steps, total_gas) = verify_step_totals(&ordered_events, &receipt.agent, agent_public_key)?;
    if ordered_events
        .iter()
        .filter(|event| event.event_type == "JOB_DONE")
        .count()
        != 1
    {
        return Err(event_failure(
            final_event,
            "receipt must contain exactly one JOB_DONE event",
        ));
    }
    let start_event = ordered_events
        .iter()
        .find(|event| event.event_type == "JOB_STARTED")
        .ok_or_else(|| event_failure(final_event, "JOB_STARTED event is missing"))?;
    let done_event = ordered_events
        .iter()
        .find(|event| event.event_type == "JOB_DONE")
        .ok_or_else(|| event_failure(final_event, "JOB_DONE event is missing"))?;
    if final_event.event_id != done_event.event_id {
        return Err(event_failure(
            done_event,
            "JOB_DONE must be the final job event",
        ));
    }
    for event in ordered_events
        .iter()
        .filter(|event| event.event_type == "WORK_STEP")
    {
        if event.sequence_number <= start_event.sequence_number
            || event.sequence_number >= done_event.sequence_number
        {
            return Err(event_failure(
                event,
                "WORK_STEP must occur after JOB_STARTED and before JOB_DONE",
            ));
        }
    }
    if receipt.steps != steps {
        return Err(event_failure(
            final_event,
            "receipt step count does not match events",
        ));
    }
    if receipt.total_gas != total_gas {
        return Err(event_failure(
            final_event,
            "receipt gas total does not match events",
        ));
    }
    Ok(VerifiedReceipt {
        steps,
        total_gas,
        final_hash: final_hash.clone(),
    })
}

pub fn verify_receipt_file(
    path: impl AsRef<Path>,
    agent_public_key_override: Option<&str>,
    witness_public_key_override: Option<&str>,
) -> Result<VerifiedReceipt, ReceiptError> {
    let receipt: ReceiptFile = serde_json::from_slice(&fs::read(path)?)?;
    verify_receipt(
        &receipt,
        agent_public_key_override,
        witness_public_key_override,
    )
}

fn verify_step_totals(
    events: &[Event],
    agent: &str,
    agent_public_key: &str,
) -> Result<(u64, u64), ReceiptError> {
    let starts = events
        .iter()
        .filter(|event| event.event_type == "JOB_STARTED")
        .collect::<Vec<_>>();
    if starts.len() != 1 {
        return Err(ReceiptError::Invalid(
            "job must have exactly one JOB_STARTED event".into(),
        ));
    }
    for event in events {
        if !matches!(
            event.event_type.as_str(),
            "JOB_STARTED" | "WORK_STEP" | "JOB_DONE"
        ) {
            return Err(event_failure(event, "unexpected event type in job receipt"));
        }
        if event.actor_id != agent {
            return Err(event_failure(event, "actor does not match job agent"));
        }
        event
            .verify(agent_public_key)
            .map_err(|error| event_failure(event, &error.to_string()))?;
    }
    let start = starts[0];
    if field_string(&start.payload, "agent", &start.event_id)? != agent {
        return Err(event_failure(start, "JOB_STARTED agent mismatch"));
    }
    if field_string(&start.payload, "agent_pubkey", &start.event_id)? != agent_public_key {
        return Err(event_failure(start, "JOB_STARTED public key mismatch"));
    }
    let done_events = events
        .iter()
        .filter(|event| event.event_type == "JOB_DONE")
        .collect::<Vec<_>>();
    if done_events.len() > 1 {
        return Err(event_failure(done_events[1], "duplicate JOB_DONE event"));
    }
    let mut steps = 0_u64;
    let mut total_gas = 0_u64;
    for event in events
        .iter()
        .filter(|event| event.event_type == "WORK_STEP")
    {
        let recorded = event
            .payload
            .get("gas")
            .and_then(Value::as_u64)
            .ok_or_else(|| event_failure(event, "WORK_STEP gas is missing or invalid"))?;
        let mut cost_payload = event.payload.clone();
        let object = cost_payload
            .as_object_mut()
            .ok_or_else(|| event_failure(event, "WORK_STEP payload is not an object"))?;
        object.remove("gas");
        let computed = calculate_ghost_gas(&cost_payload);
        if recorded != computed {
            return Err(event_failure(event, "WORK_STEP gas does not match payload"));
        }
        steps = steps
            .checked_add(1)
            .ok_or_else(|| event_failure(event, "step count overflow"))?;
        total_gas = total_gas
            .checked_add(recorded)
            .ok_or_else(|| event_failure(event, "gas total overflow"))?;
    }
    if let Some(done) = done_events.first() {
        let recorded_steps = done.payload.get("steps").and_then(Value::as_u64);
        let recorded_gas = done.payload.get("total_gas").and_then(Value::as_u64);
        if recorded_steps != Some(steps) || recorded_gas != Some(total_gas) {
            return Err(event_failure(
                done,
                "JOB_DONE totals do not match work steps",
            ));
        }
    }
    Ok((steps, total_gas))
}

fn validate_acyclic(events: &[Event]) -> Result<(), ReceiptError> {
    let index_by_id = events
        .iter()
        .enumerate()
        .map(|(index, event)| (event.event_id.as_str(), index))
        .collect::<HashMap<_, _>>();
    let mut in_degree = vec![0usize; events.len()];
    let mut children = vec![Vec::new(); events.len()];
    for (child, event) in events.iter().enumerate() {
        for parent in &event.parents {
            if let Some(parent_index) = index_by_id.get(parent.as_str()) {
                in_degree[child] += 1;
                children[*parent_index].push(child);
            }
        }
    }
    let mut ready = in_degree
        .iter()
        .enumerate()
        .filter_map(|(index, degree)| (*degree == 0).then_some(index))
        .collect::<Vec<_>>();
    let mut visited = 0usize;
    while let Some(index) = ready.pop() {
        visited += 1;
        for child in &children[index] {
            in_degree[*child] -= 1;
            if in_degree[*child] == 0 {
                ready.push(*child);
            }
        }
    }
    if visited != events.len() {
        let event = events
            .iter()
            .find(|event| in_degree[index_by_id[event.event_id.as_str()]] > 0)
            .ok_or_else(|| ReceiptError::Invalid("receipt contains a cycle".into()))?;
        return Err(event_failure(event, "event graph contains a cycle"));
    }
    Ok(())
}

fn event_failure(event: &Event, reason: &str) -> ReceiptError {
    ReceiptError::Invalid(format!("event {}: {reason}", event.event_id))
}

fn field_string<'a>(
    payload: &'a Value,
    field: &str,
    event_id: &str,
) -> Result<&'a str, ReceiptError> {
    payload.get(field).and_then(Value::as_str).ok_or_else(|| {
        ReceiptError::Invalid(format!("event {event_id}: missing or invalid {field}"))
    })
}

fn validate_identifier(value: &str, label: &str) -> Result<(), ReceiptError> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(ReceiptError::Invalid(format!("invalid {label} identifier")));
    }
    Ok(())
}

fn write_identity_seed(path: &Path, identity: &Identity) -> Result<(), ReceiptError> {
    ensure_parent(path)?;
    let mut seed = identity.seed_bytes();
    let mut encoded = hex::encode(seed);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = options.open(path).and_then(|mut file| {
        file.write_all(encoded.as_bytes())?;
        file.sync_all()
    });
    seed.zeroize();
    encoded.zeroize();
    result?;
    Ok(())
}

fn read_identity_seed(path: impl AsRef<Path>, agent: &str) -> Result<Identity, ReceiptError> {
    let mut encoded = String::new();
    File::open(path)?.read_to_string(&mut encoded)?;
    let mut decoded = hex::decode(encoded.trim())
        .map_err(|error| ReceiptError::Invalid(format!("invalid agent identity seed: {error}")))?;
    encoded.zeroize();
    if decoded.len() != 32 {
        decoded.zeroize();
        return Err(ReceiptError::Invalid(
            "agent identity seed must be exactly 32 bytes".into(),
        ));
    }
    let mut seed = [0_u8; 32];
    seed.copy_from_slice(&decoded);
    decoded.zeroize();
    let identity = Identity::from_seed(agent, &seed);
    seed.zeroize();
    Ok(identity)
}

fn write_json_atomic(path: &Path, value: &impl Serialize) -> Result<(), ReceiptError> {
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

fn ensure_parent(path: &Path) -> Result<(), ReceiptError> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

#[cfg(unix)]
fn restrict_directory(path: &Path) -> Result<(), ReceiptError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(not(unix))]
fn restrict_directory(_path: &Path) -> Result<(), ReceiptError> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::Event;
    use serde_json::Value;

    fn setup() -> (tempfile::TempDir, MoltReceipts, GhostKey, Identity) {
        let directory = tempfile::tempdir().unwrap();
        let store = directory.path().join("events.jsonl");
        let vault = directory.path().join("vault");
        let keys = directory.path().join("agent-keys");
        let runner = Identity::generate("job-runner");
        let receipts = MoltReceipts::open(store, vault, keys, runner.clone()).unwrap();
        let ghost_key = GhostKey::new("job-scope", [8_u8; 32]).unwrap();
        (directory, receipts, ghost_key, runner)
    }

    fn full_job() -> (
        tempfile::TempDir,
        MoltReceipts,
        JobStart,
        GhostKey,
        Identity,
        Identity,
    ) {
        let (directory, mut receipts, ghost_key, runner) = setup();
        let start = receipts
            .start(
                "job-1",
                "agent-1",
                "client-1",
                &ghost_key,
                Some("Do the work"),
            )
            .unwrap();
        receipts
            .log_step(
                "job-1",
                "inspect inputs",
                Some("in-1"),
                Some("out-1"),
                Some("PUBLIC"),
            )
            .unwrap();
        receipts
            .log_step(
                "job-1",
                "compute result",
                Some("in-2"),
                Some("out-2"),
                Some("GHOST"),
            )
            .unwrap();
        receipts
            .log_step("job-1", "write report", None, Some("out-3"), None)
            .unwrap();
        receipts.finish("job-1").unwrap();
        let witness = Identity::generate("witness-1");
        (directory, receipts, start, ghost_key, runner, witness)
    }

    #[test]
    fn full_session_receipt_verifies_and_recomputes_gas() {
        let (directory, receipts, _start, _key, _runner, witness) = full_job();
        let path = directory.path().join("job-1.receipt.json");
        let receipt = receipts.create_receipt("job-1", &witness, &path).unwrap();
        let verified = verify_receipt(&receipt, None, None).unwrap();
        assert_eq!(verified.steps, 3);
        assert_eq!(verified.total_gas, receipt.total_gas);
        assert_eq!(verified.final_hash, receipt.final_hash);
        assert!(path.exists());
    }

    #[test]
    fn payload_and_chain_tampering_fail_with_event_context() {
        let (directory, receipts, _start, _key, _runner, witness) = full_job();
        let path = directory.path().join("job-1.receipt.json");
        let mut receipt = receipts.create_receipt("job-1", &witness, path).unwrap();
        let step_index = receipt
            .events
            .iter()
            .position(|event| event.event_type == "WORK_STEP")
            .unwrap();
        let step_id = receipt.events[step_index].event_id.clone();
        receipt.events[step_index].payload["step"] = Value::String("tampered".into());
        let error = verify_receipt(&receipt, None, None)
            .unwrap_err()
            .to_string();
        assert!(error.contains(&step_id));
        assert!(error.contains("event hash mismatch"));

        let mut receipt = receipts
            .create_receipt("job-1", &witness, directory.path().join("retry.json"))
            .unwrap();
        receipt.chain[0] = "f".repeat(64);
        let error = verify_receipt(&receipt, None, None)
            .unwrap_err()
            .to_string();
        assert!(error.contains("chain does not match"));
    }

    #[test]
    fn wrong_witness_key_fails_and_out_of_order_input_still_verifies() {
        let (directory, receipts, _start, _key, _runner, witness) = full_job();
        let path = directory.path().join("job-1.receipt.json");
        let mut receipt = receipts.create_receipt("job-1", &witness, &path).unwrap();
        let wrong = Identity::generate("wrong-witness");
        assert!(verify_receipt(&receipt, None, Some(&wrong.public_key_hex())).is_err());
        receipt.events.reverse();
        assert!(verify_receipt(&receipt, None, None).is_ok());
    }

    #[test]
    fn work_steps_and_done_event_are_signed_by_the_spawned_agent() {
        let (directory, receipts, start, _key, _runner, witness) = full_job();
        let store = FileEventStore::open(directory.path().join("events.jsonl")).unwrap();
        let steps = store
            .all()
            .unwrap()
            .into_iter()
            .filter(|event| event.event_type == "WORK_STEP" || event.event_type == "JOB_DONE")
            .collect::<Vec<Event>>();
        assert!(steps.iter().all(|event| event.actor_id == start.agent));
        let receipt = receipts
            .create_receipt("job-1", &witness, directory.path().join("receipt.json"))
            .unwrap();
        assert_eq!(receipt.steps, 3);
    }
}
