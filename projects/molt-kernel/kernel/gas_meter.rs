use crate::hash::canonical_json_bytes;
use serde_json::Value;

pub fn calculate_ghost_gas(payload: &Value) -> u64 {
    if payload.get("tier").and_then(Value::as_str) == Some("GHOST") {
        let ciphertext_len = payload
            .get("ciphertext")
            .and_then(Value::as_str)
            .map(str::len)
            .unwrap_or(0);
        let commitment_len = payload
            .get("commitment")
            .and_then(Value::as_str)
            .map(str::len)
            .unwrap_or(0);
        let payload_len = u64::try_from(ciphertext_len)
            .unwrap_or(u64::MAX)
            .saturating_add(u64::try_from(commitment_len).unwrap_or(u64::MAX));
        payload_len.saturating_mul(2).saturating_add(50)
    } else {
        u64::try_from(canonical_json_bytes(payload).len()).unwrap_or(u64::MAX)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ghost_payload_uses_ciphertext_and_commitment_lengths() {
        let payload = json!({
            "tier": "GHOST",
            "ciphertext": "abcd",
            "commitment": "12345678",
            "plaintext": "ignored"
        });
        assert_eq!(calculate_ghost_gas(&payload), 74);
    }

    #[test]
    fn non_ghost_payload_uses_canonical_json_size() {
        let payload = json!({ "b": 2, "a": 1 });
        assert_eq!(
            calculate_ghost_gas(&payload),
            canonical_json_bytes(&payload).len() as u64
        );
    }

    #[test]
    fn missing_ghost_lengths_count_as_zero() {
        let payload = json!({ "tier": "GHOST" });
        assert_eq!(calculate_ghost_gas(&payload), 50);
    }
}
