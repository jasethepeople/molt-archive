use crate::events::Event;
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};

pub fn export_to_camp1(events: &mut Vec<Event>, multi_head_root: &str) -> Vec<String> {
    let mut indices = HashMap::with_capacity(events.len());
    for (index, event) in events.iter().enumerate() {
        assert!(
            indices.insert(event.event_id.clone(), index).is_none(),
            "duplicate event ID in export input"
        );
    }

    let mut in_degree = vec![0usize; events.len()];
    let mut children = vec![Vec::new(); events.len()];
    for (child_index, event) in events.iter().enumerate() {
        let mut seen_parents = HashSet::new();
        for parent_id in &event.parents {
            if !seen_parents.insert(parent_id.as_str()) {
                continue;
            }
            if let Some(&parent_index) = indices.get(parent_id) {
                in_degree[child_index] = in_degree[child_index]
                    .checked_add(1)
                    .expect("event has too many parents");
                children[parent_index].push(child_index);
            }
        }
    }

    let mut ready = BTreeSet::new();
    for (index, event) in events.iter().enumerate() {
        if in_degree[index] == 0 {
            ready.insert((
                event.logical_clock,
                event.sequence_number,
                event.event_id.clone(),
                index,
            ));
        }
    }

    let mut order = Vec::with_capacity(events.len());
    while let Some(next) = ready.pop_first() {
        let index = next.3;
        order.push(index);
        for &child_index in &children[index] {
            in_degree[child_index] -= 1;
            if in_degree[child_index] == 0 {
                let event = &events[child_index];
                ready.insert((
                    event.logical_clock,
                    event.sequence_number,
                    event.event_id.clone(),
                    child_index,
                ));
            }
        }
    }

    assert_eq!(order.len(), events.len(), "cycle detected in event DAG");

    let original = std::mem::take(events);
    *events = order
        .into_iter()
        .map(|index| original[index].clone())
        .collect();

    let mut chain = Vec::with_capacity(events.len());
    let mut previous_hash = "0".repeat(64);
    for event in events {
        let mut hasher = Sha256::new();
        hasher.update(previous_hash.as_bytes());
        hasher.update(event.event_id.as_bytes());
        hasher.update(multi_head_root.as_bytes());
        previous_hash = hex::encode(hasher.finalize());
        chain.push(previous_hash.clone());
    }
    chain
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::create_signed_event;
    use crate::identity::Identity;
    use serde_json::json;

    fn signed_event(
        identity: &Identity,
        logical_clock: u64,
        sequence_number: u64,
        parents: Vec<String>,
        label: &str,
    ) -> Event {
        create_signed_event(
            identity,
            "TEST",
            "MOLT/0.1",
            logical_clock,
            sequence_number,
            "2026-11-15T00:00:00Z",
            parents,
            json!({ "label": label }),
            vec![],
            vec![],
        )
    }

    #[test]
    fn export_is_topological_and_deterministic() {
        let identity = Identity::generate("export-test");
        let parent = signed_event(&identity, 9, 0, vec![], "parent");
        let child = signed_event(&identity, 0, 0, vec![parent.event_id.clone()], "child");
        let other_root = signed_event(&identity, 2, 0, vec![], "other-root");
        let parent_id = parent.event_id.clone();
        let child_id = child.event_id.clone();
        let mut first = vec![child.clone(), other_root.clone(), parent.clone()];
        let mut second = vec![parent, child, other_root];

        let first_chain = export_to_camp1(&mut first, "root");
        let second_chain = export_to_camp1(&mut second, "root");

        assert_eq!(first, second);
        assert_eq!(first_chain, second_chain);
        let parent_index = first
            .iter()
            .position(|event| event.event_id == parent_id)
            .unwrap();
        let child_index = first
            .iter()
            .position(|event| event.event_id == child_id)
            .unwrap();
        assert!(parent_index < child_index);
        assert_eq!(first_chain.len(), 3);
        assert!(first_chain.iter().all(|hash| hash.len() == 64));
    }

    #[test]
    fn parents_outside_the_export_range_do_not_block_events() {
        let identity = Identity::generate("export-range-test");
        let event = signed_event(&identity, 0, 0, vec!["external-parent".into()], "scoped");
        let mut events = vec![event];
        assert_eq!(export_to_camp1(&mut events, "root").len(), 1);
    }

    #[test]
    #[should_panic(expected = "cycle detected in event DAG")]
    fn cycles_are_rejected() {
        let identity = Identity::generate("export-cycle-test");
        let first = signed_event(&identity, 0, 0, vec![], "first");
        let second = signed_event(&identity, 0, 1, vec![], "second");
        let mut first = first;
        let mut second = second;
        first.parents.push(second.event_id.clone());
        second.parents.push(first.event_id.clone());
        let mut events = vec![first, second];
        export_to_camp1(&mut events, "root");
    }
}
