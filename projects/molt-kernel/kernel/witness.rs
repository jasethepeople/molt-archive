use crate::hash::canonical_json_bytes;
use crate::identity::{verify, Identity, IdentityError};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WitnessReceipt {
    pub event_id: String,
    pub prev_hash: String,
    pub signature: String,
}

impl WitnessReceipt {
    pub fn sign(
        event_id: impl Into<String>,
        prev_hash: impl Into<String>,
        witness: &Identity,
    ) -> Self {
        let mut receipt = Self {
            event_id: event_id.into(),
            prev_hash: prev_hash.into(),
            signature: String::new(),
        };
        receipt.signature = witness.sign(&receipt.signing_bytes());
        receipt
    }

    pub fn verify(&self, witness_public_key_hex: &str) -> Result<(), IdentityError> {
        verify(
            witness_public_key_hex,
            &self.signing_bytes(),
            &self.signature,
        )
    }

    fn signing_bytes(&self) -> Vec<u8> {
        canonical_json_bytes(&json!({
            "event_id": self.event_id,
            "prev_hash": self.prev_hash,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn receipt_round_trips_and_verifies() {
        let witness = Identity::generate("witness-1");
        let receipt = WitnessReceipt::sign("event-1", "0".repeat(64), &witness);
        let encoded = serde_json::to_vec(&receipt).unwrap();
        let decoded: WitnessReceipt = serde_json::from_slice(&encoded).unwrap();
        assert_eq!(decoded, receipt);
        assert!(decoded.verify(&witness.public_key_hex()).is_ok());
        assert!(decoded
            .verify(&Identity::generate("other").public_key_hex())
            .is_err());
    }

    #[test]
    fn receipt_rejects_tampering() {
        let witness = Identity::generate("witness-1");
        let mut receipt = WitnessReceipt::sign("event-1", "previous", &witness);
        receipt.prev_hash = "tampered".into();
        assert!(receipt.verify(&witness.public_key_hex()).is_err());
    }
}
