use crate::events::{create_signed_event, Event};
use crate::exporter::export_to_camp1;
use crate::ghost::{create_ghost_bundle, shred_key, zeroize_json_value, GhostError, GhostKey};
use crate::ghost_store::{GhostStore, GhostStoreError};
use crate::identity::Identity;
use crate::invariants::validate_history;
use crate::store::{EventStore, FileEventStore, StoreError};
use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use thiserror::Error;
use zeroize::Zeroizing;

#[derive(Debug, Error)]
pub enum GhostBidError {
    #[error("{0}")]
    Invalid(String),
    #[error("auction is closed")]
    AuctionClosed,
    #[error("auction is still open")]
    AuctionStillOpen,
    #[error("reveal opens at close")]
    RevealNotOpen,
    #[error("auction has already been closed")]
    AlreadyClosed,
    #[error("bidder already has a committed bid in this auction")]
    DuplicateBid,
    #[error("auction not found: {0}")]
    AuctionNotFound(String),
    #[error("bid not found for bidder {bidder} in auction {auction}")]
    BidNotFound { auction: String, bidder: String },
    #[error("bid has already been revealed")]
    AlreadyRevealed,
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
pub struct AuctionResult {
    pub event_id: String,
    pub auction: String,
    pub close_logical: u64,
    pub reveal_window: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct BidResult {
    pub event_id: String,
    pub commitment: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CommittedBid {
    #[serde(skip_serializing)]
    pub event_id: String,
    pub bidder: String,
    pub commitment: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CloseResult {
    pub event_id: String,
    pub bids: Vec<CommittedBid>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Winner {
    pub winner: String,
    pub amount: u64,
    pub commitment: String,
    pub reveal_event_id: String,
    #[serde(skip_serializing)]
    pub commit_event_id: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ExcludedReveal {
    pub reveal_event_id: String,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct WinnerSelection {
    pub winner: Option<Winner>,
    pub excluded_reveals: Vec<ExcludedReveal>,
}

pub struct GhostBid {
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

impl GhostBid {
    pub fn open(
        store_path: impl AsRef<Path>,
        vault_path: impl AsRef<Path>,
        identity: Identity,
    ) -> Result<Self, GhostBidError> {
        create_parent(store_path.as_ref())?;
        let event_store = FileEventStore::open(store_path)?;
        let ghost_store = GhostStore::new(vault_path)?;
        Ok(Self {
            event_store,
            ghost_store,
            identity,
        })
    }

    pub fn create(
        &mut self,
        auction_id: &str,
        close_logical: u64,
        reveal_window: Option<u64>,
    ) -> Result<AuctionResult, GhostBidError> {
        if auction_id.is_empty() {
            return Err(GhostBidError::Invalid(
                "auction ID must not be empty".into(),
            ));
        }
        match self.find_auction(auction_id) {
            Ok(_) => {
                return Err(GhostBidError::Invalid(format!(
                    "auction already exists: {auction_id}"
                )))
            }
            Err(GhostBidError::AuctionNotFound(_)) => {}
            Err(error) => return Err(error),
        }
        let reveal_window = reveal_window.unwrap_or(100);
        let event = self.new_event(
            "AUCTION_CREATED",
            json!({
                "auction": auction_id,
                "close_logical": close_logical,
                "reveal_window": reveal_window,
            }),
        )?;
        if event.logical_clock >= close_logical {
            return Err(GhostBidError::Invalid(
                "close logical clock must follow auction creation".into(),
            ));
        }
        self.append(event.clone())?;
        Ok(AuctionResult {
            event_id: event.event_id,
            auction: auction_id.to_owned(),
            close_logical,
            reveal_window,
        })
    }

    pub fn bid(
        &mut self,
        auction_id: &str,
        amount: u64,
        bidder: &str,
        ghost_key: &GhostKey,
        now: u64,
    ) -> Result<BidResult, GhostBidError> {
        let auction = self.find_auction(auction_id)?;
        let close_logical = required_u64(&auction.payload, "close_logical")?;
        if now > close_logical || self.is_closed(auction_id)? {
            return Err(GhostBidError::AuctionClosed);
        }
        if self.next_logical_clock()? > close_logical {
            return Err(GhostBidError::AuctionClosed);
        }
        if self
            .committed_bids(auction_id)?
            .iter()
            .any(|committed| committed.bidder == bidder)
        {
            return Err(GhostBidError::DuplicateBid);
        }
        let reveal_window = required_u64(&auction.payload, "reveal_window")?;
        let expires_logical = close_logical
            .checked_add(reveal_window)
            .ok_or_else(|| GhostBidError::Invalid("reveal expiry logical clock overflow".into()))?;
        let payload = ZeroizingValue(json!({
            "auction": auction_id,
            "bidder": bidder,
            "amount": amount,
        }));
        let mut dek = Zeroizing::new([0_u8; 32]);
        OsRng
            .try_fill_bytes(&mut *dek)
            .map_err(|error| GhostBidError::Invalid(error.to_string()))?;
        let bundle_result = create_ghost_bundle(&payload.0, &dek, ghost_key, Some(expires_logical));
        shred_key(&mut dek);
        let bundle = bundle_result?;
        let commitment = bundle.commitment.clone();
        let event = self.new_event(
            "BID_COMMITTED",
            json!({
                "auction": auction_id,
                "bidder": bidder,
                "commitment": commitment,
            }),
        )?;
        if event.logical_clock > close_logical {
            return Err(GhostBidError::AuctionClosed);
        }
        self.ghost_store.save(&event.event_id, &bundle)?;
        if let Err(error) = self.append(event.clone()) {
            let _ = self.ghost_store.destroy(&event.event_id);
            return Err(error);
        }
        Ok(BidResult {
            event_id: event.event_id,
            commitment,
        })
    }

    pub fn close(&mut self, auction_id: &str, now: u64) -> Result<CloseResult, GhostBidError> {
        let auction = self.find_auction(auction_id)?;
        let close_logical = required_u64(&auction.payload, "close_logical")?;
        if now < close_logical {
            return Err(GhostBidError::AuctionStillOpen);
        }
        if self.is_closed(auction_id)? {
            return Err(GhostBidError::AlreadyClosed);
        }
        let event = self.new_event(
            "AUCTION_CLOSED",
            json!({
                "auction": auction_id,
                "close_logical": close_logical,
            }),
        )?;
        self.append(event.clone())?;
        Ok(CloseResult {
            event_id: event.event_id,
            bids: self.committed_bids(auction_id)?,
        })
    }

    pub fn reveal(
        &mut self,
        auction_id: &str,
        bidder: &str,
        ghost_key: &GhostKey,
        now: u64,
    ) -> Result<u64, GhostBidError> {
        let auction = self.find_auction(auction_id)?;
        let close_logical = required_u64(&auction.payload, "close_logical")?;
        if now < close_logical || !self.is_closed(auction_id)? {
            return Err(GhostBidError::RevealNotOpen);
        }
        let commit = self
            .committed_bids(auction_id)?
            .into_iter()
            .find(|candidate| candidate.bidder == bidder)
            .ok_or_else(|| GhostBidError::BidNotFound {
                auction: auction_id.to_owned(),
                bidder: bidder.to_owned(),
            })?;
        if self.has_reveal(auction_id, bidder)? {
            return Err(GhostBidError::AlreadyRevealed);
        }
        let plaintext =
            self.ghost_store
                .load(&commit.event_id, &commit.commitment, ghost_key, now)?;
        let payload = ZeroizingValue(serde_json::from_slice(&plaintext)?);
        if payload.0.get("auction").and_then(Value::as_str) != Some(auction_id)
            || payload.0.get("bidder").and_then(Value::as_str) != Some(bidder)
        {
            return Err(GhostBidError::Invalid(
                "decrypted bid identity does not match its commit event".into(),
            ));
        }
        let amount = required_u64(&payload.0, "amount")?;
        let event = self.new_event(
            "BID_REVEALED",
            json!({
                "auction": auction_id,
                "bidder": bidder,
                "amount": amount,
                "commitment": commit.commitment,
            }),
        )?;
        self.append(event)?;
        Ok(amount)
    }

    pub fn winner(&self, auction_id: &str) -> Result<WinnerSelection, GhostBidError> {
        self.select_winner(auction_id)
    }

    pub fn shred_losing(
        &mut self,
        auction_id: &str,
        include_winner: bool,
    ) -> Result<Vec<String>, GhostBidError> {
        if !self.is_closed(auction_id)? {
            return Err(GhostBidError::AuctionStillOpen);
        }
        let selection = self.select_winner(auction_id)?;
        if let Some(excluded) = selection.excluded_reveals.first() {
            return Err(GhostBidError::Invalid(format!(
                "invalid reveal {}: {}",
                excluded.reveal_event_id, excluded.reason
            )));
        }
        let winner_commit = selection
            .winner
            .as_ref()
            .map(|winner| winner.commit_event_id.as_str());
        let already_shredded = self.shredded_commit_ids(auction_id)?;
        let mut shredded_ids = Vec::new();
        for commit in self.committed_bids(auction_id)? {
            if already_shredded.contains(&commit.event_id) {
                continue;
            }
            let is_winner = winner_commit == Some(commit.event_id.as_str());
            if is_winner && !include_winner {
                continue;
            }
            self.ghost_store.destroy(&commit.event_id)?;
            let event = self.new_event(
                "BID_SHREDDED",
                json!({
                    "auction": auction_id,
                    "bidder": commit.bidder,
                    "commitment": commit.commitment,
                    "commit_event_id": commit.event_id,
                    "included_winner": is_winner,
                }),
            )?;
            self.append(event.clone())?;
            shredded_ids.push(event.event_id);
        }
        Ok(shredded_ids)
    }

    pub fn verify(&self, auction_id: &str) -> Result<(), GhostBidError> {
        let auction = self.find_auction(auction_id)?;
        let close_logical = required_u64(&auction.payload, "close_logical")?;
        let reveal_window = required_u64(&auction.payload, "reveal_window")?;
        let events = self.auction_events(auction_id)?;
        for event in &events {
            self.verify_event(event)?;
            if event.event_type == "BID_COMMITTED" && event.logical_clock > close_logical {
                return Err(GhostBidError::Invalid(format!(
                    "late bid commit {} has logical clock {} after close {}",
                    event.event_id, event.logical_clock, close_logical
                )));
            }
        }
        let commits = self.committed_bids(auction_id)?;
        let mut bidders = HashSet::new();
        for commit in &commits {
            if !bidders.insert(commit.bidder.as_str()) {
                return Err(GhostBidError::Invalid(format!(
                    "multiple committed bids from {}",
                    commit.bidder
                )));
            }
        }
        let shredded = self.shredded_commit_ids(auction_id)?;
        let shredded_events = self.shredded_events(auction_id)?;
        let expiry = close_logical
            .checked_add(reveal_window)
            .ok_or_else(|| GhostBidError::Invalid("reveal expiry logical clock overflow".into()))?;
        for commit in &commits {
            if shredded.contains(&commit.event_id) {
                match self.ghost_store.inspect(&commit.event_id) {
                    Err(GhostStoreError::Io(error))
                        if error.kind() == std::io::ErrorKind::NotFound => {}
                    Ok(_) => {
                        return Err(GhostBidError::Invalid(format!(
                            "shredded bundle still exists: {}",
                            commit.event_id
                        )))
                    }
                    Err(error) => return Err(error.into()),
                }
            } else {
                let bundle = self.ghost_store.inspect(&commit.event_id)?;
                if bundle.commitment != commit.commitment {
                    return Err(GhostBidError::Invalid(format!(
                        "stored commitment mismatch for {}",
                        commit.event_id
                    )));
                }
                if bundle.kek_id != commit.bidder {
                    return Err(GhostBidError::Invalid(format!(
                        "bid bundle is not wrapped under bidder scope for {}",
                        commit.event_id
                    )));
                }
                if bundle.expires_logical != Some(expiry) {
                    return Err(GhostBidError::Invalid(format!(
                        "wrong reveal expiry for {}",
                        commit.event_id
                    )));
                }
            }
        }
        for event in &shredded_events {
            let commit_event_id = required_string(&event.payload, "commit_event_id")?;
            let commit = commits
                .iter()
                .find(|commit| commit.event_id == commit_event_id)
                .ok_or_else(|| GhostBidError::Invalid("shred event has no committed bid".into()))?;
            if required_string(&event.payload, "commitment")? != commit.commitment {
                return Err(GhostBidError::Invalid(
                    "shred event commitment mismatch".into(),
                ));
            }
            let is_winner = self
                .select_winner(auction_id)?
                .winner
                .is_some_and(|winner| winner.commit_event_id == commit.event_id);
            if event.payload["included_winner"].as_bool() != Some(is_winner) {
                return Err(GhostBidError::Invalid(format!(
                    "incorrect winner-shred flag for {}",
                    event.event_id
                )));
            }
        }
        let selection = self.select_winner(auction_id)?;
        if let Some(excluded) = selection.excluded_reveals.first() {
            return Err(GhostBidError::Invalid(format!(
                "invalid reveal {}: {}",
                excluded.reveal_event_id, excluded.reason
            )));
        }
        Ok(())
    }

    fn select_winner(&self, auction_id: &str) -> Result<WinnerSelection, GhostBidError> {
        self.find_auction(auction_id)?;
        let mut ordered = self.auction_events(auction_id)?;
        export_to_camp1(&mut ordered, &format!("auction:{auction_id}"));
        let commits: Vec<Event> = ordered
            .iter()
            .filter(|event| event.event_type == "BID_COMMITTED")
            .cloned()
            .collect();
        let commit_order: HashMap<&str, usize> = commits
            .iter()
            .enumerate()
            .map(|(index, event)| (event.event_id.as_str(), index))
            .collect();
        let by_bidder: HashMap<String, &Event> = commits
            .iter()
            .filter_map(|event| {
                event
                    .payload
                    .get("bidder")
                    .and_then(Value::as_str)
                    .map(|bidder| (bidder.to_owned(), event))
            })
            .collect();
        let mut excluded_reveals = Vec::new();
        let mut winner: Option<(usize, Winner)> = None;
        for reveal in ordered
            .iter()
            .filter(|event| event.event_type == "BID_REVEALED")
        {
            let reject = |reason: String| ExcludedReveal {
                reveal_event_id: reveal.event_id.clone(),
                reason,
            };
            if let Err(error) = self.verify_event(reveal) {
                excluded_reveals.push(reject(error.to_string()));
                continue;
            }
            let Some(bidder) = reveal.payload.get("bidder").and_then(Value::as_str) else {
                excluded_reveals.push(reject("missing bidder".into()));
                continue;
            };
            let Some(commit) = by_bidder.get(bidder).copied() else {
                excluded_reveals.push(reject("no matching committed bid".into()));
                continue;
            };
            let Some(amount) = reveal.payload.get("amount").and_then(Value::as_u64) else {
                excluded_reveals.push(reject("invalid amount".into()));
                continue;
            };
            let commitment = required_string(&commit.payload, "commitment")?;
            if reveal.payload.get("commitment").and_then(Value::as_str) != Some(commitment) {
                excluded_reveals.push(reject("reveal commitment does not match commit".into()));
                continue;
            }
            if reveal.payload.get("auction").and_then(Value::as_str) != Some(auction_id) {
                excluded_reveals.push(reject("reveal auction does not match".into()));
                continue;
            }
            match self.verify_reveal_payload(auction_id, bidder, amount, commit) {
                Ok(()) => {}
                Err(error) => {
                    excluded_reveals.push(reject(error.to_string()));
                    continue;
                }
            }
            let commit_index = commit_order[commit.event_id.as_str()];
            let candidate = Winner {
                winner: bidder.to_owned(),
                amount,
                commitment: commitment.to_owned(),
                reveal_event_id: reveal.event_id.clone(),
                commit_event_id: commit.event_id.clone(),
            };
            let replace = winner.as_ref().is_none_or(|(index, best)| {
                amount > best.amount || (amount == best.amount && commit_index < *index)
            });
            if replace {
                winner = Some((commit_index, candidate));
            }
        }
        Ok(WinnerSelection {
            winner: winner.map(|(_, winner)| winner),
            excluded_reveals,
        })
    }

    fn verify_reveal_payload(
        &self,
        auction_id: &str,
        bidder: &str,
        amount: u64,
        commit: &Event,
    ) -> Result<(), GhostBidError> {
        let shredded = self.shredded_commit_ids(auction_id)?;
        let bundle = match self.ghost_store.inspect(&commit.event_id) {
            Ok(bundle) => bundle,
            Err(GhostStoreError::Io(error))
                if error.kind() == std::io::ErrorKind::NotFound
                    && shredded.contains(&commit.event_id) =>
            {
                return Ok(())
            }
            Err(error) => return Err(error.into()),
        };
        if bundle.commitment != required_string(&commit.payload, "commitment")? {
            return Err(GhostBidError::Invalid(
                "stored bundle commitment does not match the commit event".into(),
            ));
        }
        let payload = ZeroizingValue(json!({
            "auction": auction_id,
            "bidder": bidder,
            "amount": amount,
        }));
        if bundle.commitment != required_string(&commit.payload, "commitment")?
            || !bundle.commitment_matches(&payload.0)
        {
            return Err(GhostBidError::Invalid(
                "revealed amount does not match the committed payload".into(),
            ));
        }
        Ok(())
    }

    fn find_auction(&self, auction_id: &str) -> Result<Event, GhostBidError> {
        let mut matching = self.event_store.all()?.into_iter().filter(|event| {
            event.event_type == "AUCTION_CREATED"
                && event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
        });
        let event = matching
            .next()
            .ok_or_else(|| GhostBidError::AuctionNotFound(auction_id.to_owned()))?;
        if matching.next().is_some() {
            return Err(GhostBidError::Invalid(format!(
                "multiple AUCTION_CREATED events for {auction_id}"
            )));
        }
        Ok(event)
    }

    fn is_closed(&self, auction_id: &str) -> Result<bool, GhostBidError> {
        Ok(self.event_store.all()?.iter().any(|event| {
            event.event_type == "AUCTION_CLOSED"
                && event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
        }))
    }

    fn has_reveal(&self, auction_id: &str, bidder: &str) -> Result<bool, GhostBidError> {
        Ok(self.event_store.all()?.iter().any(|event| {
            event.event_type == "BID_REVEALED"
                && event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
                && event.payload.get("bidder").and_then(Value::as_str) == Some(bidder)
        }))
    }

    fn committed_bids(&self, auction_id: &str) -> Result<Vec<CommittedBid>, GhostBidError> {
        Ok(self
            .event_store
            .all()?
            .into_iter()
            .filter(|event| {
                event.event_type == "BID_COMMITTED"
                    && event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
            })
            .map(|event| CommittedBid {
                event_id: event.event_id,
                bidder: event.payload["bidder"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                commitment: event.payload["commitment"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
            })
            .collect())
    }

    fn auction_events(&self, auction_id: &str) -> Result<Vec<Event>, GhostBidError> {
        Ok(self
            .event_store
            .all()?
            .into_iter()
            .filter(|event| {
                event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
            })
            .collect())
    }

    fn shredded_commit_ids(&self, auction_id: &str) -> Result<HashSet<String>, GhostBidError> {
        let commits = self.committed_bids(auction_id)?;
        let by_event_id: HashMap<&str, &CommittedBid> = commits
            .iter()
            .map(|commit| (commit.event_id.as_str(), commit))
            .collect();
        let mut shredded = HashSet::new();
        for event in self.shredded_events(auction_id)? {
            let event_id = required_string(&event.payload, "commit_event_id")?;
            if !by_event_id.contains_key(event_id) {
                return Err(GhostBidError::Invalid(
                    "shred event has no matching committed bid".into(),
                ));
            }
            shredded.insert(event_id.to_owned());
        }
        Ok(shredded)
    }

    fn shredded_events(&self, auction_id: &str) -> Result<Vec<Event>, GhostBidError> {
        Ok(self
            .event_store
            .all()?
            .into_iter()
            .filter(|event| {
                event.event_type == "BID_SHREDDED"
                    && event.payload.get("auction").and_then(Value::as_str) == Some(auction_id)
            })
            .collect())
    }

    fn verify_event(&self, event: &Event) -> Result<(), GhostBidError> {
        if event.actor_id != self.identity.agent_id {
            return Err(GhostBidError::Invalid(format!(
                "event signer does not match this CLI identity: {}",
                event.event_id
            )));
        }
        event
            .verify(&self.identity.public_key_hex())
            .map_err(|error| GhostBidError::Invalid(error.to_string()))
    }

    fn append(&mut self, event: Event) -> Result<(), GhostBidError> {
        let events = self.event_store.all()?;
        validate_history(&event, &events)
            .map_err(|error| GhostBidError::Invalid(error.to_string()))?;
        self.event_store.append(event)?;
        Ok(())
    }

    fn new_event(&self, event_type: &str, payload: Value) -> Result<Event, GhostBidError> {
        let events = self.event_store.all()?;
        let last = events.iter().max_by_key(|event| event.sequence_number);
        let (sequence_number, logical_clock, parents) = match last {
            Some(last) => (
                last.sequence_number.checked_add(1).ok_or_else(|| {
                    GhostBidError::Invalid("event sequence number overflow".into())
                })?,
                last.logical_clock
                    .checked_add(1)
                    .ok_or_else(|| GhostBidError::Invalid("event logical clock overflow".into()))?,
                vec![last.event_id.clone()],
            ),
            None => (0, 0, Vec::new()),
        };
        Ok(create_signed_event(
            &self.identity,
            event_type,
            "MOLT/0.1",
            logical_clock,
            sequence_number,
            crate::agent_generator::timestamp_now().map_err(GhostBidError::Invalid)?,
            parents,
            payload,
            Vec::new(),
            Vec::new(),
        ))
    }

    fn next_logical_clock(&self) -> Result<u64, GhostBidError> {
        Ok(self
            .event_store
            .all()?
            .iter()
            .max_by_key(|event| event.sequence_number)
            .map(|event| event.logical_clock.saturating_add(1))
            .unwrap_or(0))
    }
}

fn required_u64(value: &Value, field: &str) -> Result<u64, GhostBidError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| GhostBidError::Invalid(format!("missing or invalid {field}")))
}

fn required_string<'a>(value: &'a Value, field: &str) -> Result<&'a str, GhostBidError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| GhostBidError::Invalid(format!("missing or invalid {field}")))
}

fn create_parent(path: &Path) -> Result<(), GhostBidError> {
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
    use std::path::PathBuf;
    use tempfile::{tempdir, TempDir};

    fn setup() -> (TempDir, GhostBid, Identity, PathBuf) {
        let directory = tempdir().unwrap();
        let identity = Identity::generate("ghost-bid");
        let auction = GhostBid::open(
            directory.path().join("events.jsonl"),
            directory.path().join("vault"),
            identity.clone(),
        )
        .unwrap();
        let vault_path = directory.path().join("vault");
        (directory, auction, identity, vault_path)
    }

    fn key(bidder: &str, byte: u8) -> GhostKey {
        GhostKey::new(bidder, [byte; 32]).unwrap()
    }

    #[test]
    fn sealed_bid_hides_amount_and_enforces_deadlines() {
        let (directory, mut auction, _, vault_path) = setup();
        let alice_key = key("alice", 3);
        auction.create("auction-1", 10, None).unwrap();
        let bid = auction
            .bid("auction-1", 456789, "alice", &alice_key, 1)
            .unwrap();
        let serialized_events = serde_json::to_string(&auction.event_store.all().unwrap()).unwrap();
        let bundle_path = vault_path.join(format!("{}.ghost", bid.event_id));
        let bundle = fs::read_to_string(bundle_path).unwrap();
        assert!(!serialized_events.contains("456789"));
        assert!(!bundle.contains("456789"));
        assert!(matches!(
            auction.reveal("auction-1", "alice", &alice_key, 9),
            Err(GhostBidError::RevealNotOpen)
        ));
        assert!(matches!(
            auction.reveal("auction-1", "alice", &alice_key, 10),
            Err(GhostBidError::RevealNotOpen)
        ));
        assert!(matches!(
            auction.bid("auction-1", 10, "late", &key("late", 4), 11),
            Err(GhostBidError::AuctionClosed)
        ));
        auction.close("auction-1", 10).unwrap();
        assert_eq!(
            auction
                .reveal("auction-1", "alice", &alice_key, 10)
                .unwrap(),
            456789
        );
        let winner = auction.winner("auction-1").unwrap();
        assert_eq!(winner.winner.unwrap().winner, "alice");
        assert!(winner.excluded_reveals.is_empty());
        auction.verify("auction-1").unwrap();
        assert!(directory.path().join("events.jsonl").exists());
    }

    #[test]
    fn wrong_key_tampered_bundle_and_expired_reveal_are_rejected() {
        let (_directory, mut auction, _, vault_path) = setup();
        let alice_key = key("alice", 5);
        auction.create("auction-2", 10, Some(3)).unwrap();
        let bid = auction
            .bid("auction-2", 55, "alice", &alice_key, 1)
            .unwrap();
        auction.close("auction-2", 10).unwrap();
        assert!(matches!(
            auction.reveal("auction-2", "alice", &key("alice", 6), 10),
            Err(GhostBidError::GhostStore(GhostStoreError::Crypto(
                GhostError::DekUnwrapFailed
            )))
        ));
        let bundle_path = vault_path.join(format!("{}.ghost", bid.event_id));
        let mut bundle: Value = serde_json::from_slice(&fs::read(&bundle_path).unwrap()).unwrap();
        let ciphertext = bundle["ciphertext"].as_array_mut().unwrap();
        ciphertext[0] = json!(ciphertext[0].as_u64().unwrap() ^ 1);
        fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
        assert!(matches!(
            auction.reveal("auction-2", "alice", &alice_key, 11),
            Err(GhostBidError::GhostStore(GhostStoreError::Crypto(
                GhostError::PayloadDecryptionFailed
            )))
        ));

        let (directory, mut auction, _, _) = setup();
        let alice_key = key("alice", 7);
        auction.create("auction-3", 10, Some(3)).unwrap();
        auction
            .bid("auction-3", 55, "alice", &alice_key, 1)
            .unwrap();
        auction.close("auction-3", 10).unwrap();
        assert!(matches!(
            auction.reveal("auction-3", "alice", &alice_key, 13),
            Err(GhostBidError::GhostStore(GhostStoreError::Expired))
        ));
        drop(directory);
    }

    #[test]
    fn winner_ties_use_earliest_commit_and_shred_removes_losing_bundles() {
        let (_directory, mut auction, _, vault_path) = setup();
        let alice_key = key("alice", 8);
        let bob_key = key("bob", 9);
        auction.create("auction-4", 10, None).unwrap();
        let alice = auction
            .bid("auction-4", 100, "alice", &alice_key, 1)
            .unwrap();
        let bob = auction.bid("auction-4", 100, "bob", &bob_key, 2).unwrap();
        auction.close("auction-4", 10).unwrap();
        auction.reveal("auction-4", "bob", &bob_key, 10).unwrap();
        auction
            .reveal("auction-4", "alice", &alice_key, 10)
            .unwrap();
        let winner = auction.winner("auction-4").unwrap().winner.unwrap();
        assert_eq!(winner.winner, "alice");
        assert_eq!(winner.commit_event_id, alice.event_id);
        let shredded = auction.shred_losing("auction-4", false).unwrap();
        assert_eq!(shredded.len(), 1);
        assert!(!vault_path.join(format!("{}.ghost", bob.event_id)).exists());
        assert!(vault_path
            .join(format!("{}.ghost", alice.event_id))
            .exists());
        auction.verify("auction-4").unwrap();
        auction.shred_losing("auction-4", true).unwrap();
        assert!(!vault_path
            .join(format!("{}.ghost", alice.event_id))
            .exists());
        auction.verify("auction-4").unwrap();
    }

    #[test]
    fn verify_rejects_a_late_planted_commit() {
        let (_directory, mut auction, identity, _) = setup();
        let created = auction.create("auction-5", 1, None).unwrap();
        let event = create_signed_event(
            &identity,
            "BID_COMMITTED",
            "MOLT/0.1",
            2,
            1,
            "2026-11-16T00:00:00Z",
            vec![created.event_id],
            json!({ "auction": "auction-5", "bidder": "late", "commitment": "bad" }),
            Vec::new(),
            Vec::new(),
        );
        auction.event_store.append(event).unwrap();
        assert!(auction
            .verify("auction-5")
            .unwrap_err()
            .to_string()
            .contains("late bid commit"));
    }
}
