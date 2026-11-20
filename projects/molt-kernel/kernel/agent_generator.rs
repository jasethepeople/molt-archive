use crate::events::{create_signed_event, Event};
use crate::ghost::{create_ghost_bundle, shred_key, zeroize_json_value, GhostKey};
use crate::ghost_store::GhostStore;
use crate::identity::Identity;
use rand::{rngs::OsRng, RngCore};
use serde_json::{json, Value};
use std::time::{SystemTime, UNIX_EPOCH};
use zeroize::Zeroizing;

pub struct SpawnedAgent {
    pub agent_id: String,
    pub genesis_event: Event,
    pub system_prompt_commitment: String,
    pub identity: Identity,
}

struct ZeroizingJson(Value);

impl Drop for ZeroizingJson {
    fn drop(&mut self) {
        zeroize_json_value(&mut self.0);
    }
}

pub fn spawn_agent(
    forge_identity: &Identity,
    ghost_store: &GhostStore,
    ghost_key: &GhostKey,
    agent_name: &str,
    system_prompt: &str,
    expires_logical: Option<u64>,
) -> Result<SpawnedAgent, String> {
    let identity = Identity::generate(agent_name);
    let agent_id = identity.agent_id.clone();

    let mut dek = Zeroizing::new([0_u8; 32]);
    OsRng
        .try_fill_bytes(&mut *dek)
        .map_err(|error| error.to_string())?;

    let generation_result = (|| {
        let prompt_payload = ZeroizingJson(json!({ "system_prompt": system_prompt }));
        let bundle = create_ghost_bundle(&prompt_payload.0, &dek, ghost_key, expires_logical)
            .map_err(|error| error.to_string())?;
        let commitment = bundle.commitment.clone();
        let timestamp = timestamp_now()?;

        let genesis_event = create_signed_event(
            forge_identity,
            "AGENT_GENESIS",
            "MOLT/0.1",
            0,
            0,
            timestamp,
            Vec::new(),
            json!({
                "agent_name": agent_name,
                "tier": "GHOST",
                "commitment": commitment,
                "capabilities": ["ghost.read", "shell.execute"],
                "payload_ref": Value::Null
            }),
            Vec::new(),
            Vec::new(),
        );

        ghost_store
            .save(&genesis_event.event_id, &bundle)
            .map_err(|error| error.to_string())?;
        Ok::<_, String>((genesis_event, commitment))
    })();

    shred_key(&mut dek);
    let (genesis_event, system_prompt_commitment) = generation_result?;

    Ok(SpawnedAgent {
        agent_id,
        genesis_event,
        system_prompt_commitment,
        identity,
    })
}

pub fn timestamp_now() -> Result<String, String> {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_secs();
    let seconds = i64::try_from(seconds).map_err(|error| error.to_string())?;
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let shifted_days = days + 719_468;
    let era = if shifted_days >= 0 {
        shifted_days
    } else {
        shifted_days - 146_096
    }
    .div_euclid(146_097);
    let day_of_era = shifted_days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    let hour = day_seconds / 3_600;
    let minute = day_seconds % 3_600 / 60;
    let second = day_seconds % 60;

    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn spawn_agent_stores_an_encrypted_prompt_and_signed_genesis_event() {
        let directory = tempfile::tempdir().unwrap();
        let store = GhostStore::new(directory.path()).unwrap();
        let forge_identity = Identity::generate("forge");
        let ghost_key = GhostKey::new("scope-key-v1", [5_u8; 32]).unwrap();
        let system_prompt = "Keep this prompt confidential.";

        let spawned = spawn_agent(
            &forge_identity,
            &store,
            &ghost_key,
            "test-agent",
            system_prompt,
            None,
        )
        .unwrap();

        assert_eq!(spawned.agent_id, "test-agent");
        assert_eq!(spawned.genesis_event.logical_clock, 0);
        assert_eq!(spawned.genesis_event.sequence_number, 0);
        assert!(spawned.genesis_event.parents.is_empty());
        assert!(spawned
            .genesis_event
            .verify(&forge_identity.public_key_hex())
            .is_ok());
        assert_eq!(
            spawned.genesis_event.payload["tier"],
            Value::String("GHOST".into())
        );
        assert_eq!(
            spawned.genesis_event.payload["commitment"],
            spawned.system_prompt_commitment
        );

        let bundle_path = directory
            .path()
            .join(format!("{}.ghost", spawned.genesis_event.event_id));
        let stored_bundle = std::fs::read(bundle_path).unwrap();
        assert!(!String::from_utf8_lossy(&stored_bundle).contains(system_prompt));

        let recovered = store
            .load(
                &spawned.genesis_event.event_id,
                &spawned.system_prompt_commitment,
                &ghost_key,
                0,
            )
            .unwrap();
        let recovered: Value = serde_json::from_slice(&recovered).unwrap();
        assert_eq!(recovered["system_prompt"], system_prompt);
    }

    #[test]
    fn timestamp_is_rfc3339_utc() {
        let timestamp = timestamp_now().unwrap();
        assert!(timestamp.ends_with('Z'));
        assert_eq!(timestamp.len(), 20);
        assert_eq!(timestamp.as_bytes()[4], b'-');
        assert_eq!(timestamp.as_bytes()[7], b'-');
        assert_eq!(timestamp.as_bytes()[10], b'T');
    }
}
