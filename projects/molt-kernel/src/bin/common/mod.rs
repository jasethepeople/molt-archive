use molt_kernel::agent_generator::timestamp_now;
use molt_kernel::events::{create_signed_event, Event};
use molt_kernel::ghost::GhostKey;
use molt_kernel::identity::Identity;
use molt_kernel::store::{EventStore, FileEventStore};
use serde_json::Value;
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use zeroize::{Zeroize, Zeroizing};

pub struct CliArgs {
    pub command: String,
    options: HashMap<String, Vec<String>>,
}

impl CliArgs {
    pub fn parse(args: &[String]) -> Result<Self, String> {
        let command = args
            .first()
            .cloned()
            .ok_or_else(|| "missing command".to_owned())?;
        let mut options = HashMap::new();
        let mut index = 1;
        while index < args.len() {
            let name = &args[index];
            if !name.starts_with("--") {
                return Err(format!("unexpected argument: {name}"));
            }
            let value = args
                .get(index + 1)
                .ok_or_else(|| format!("{name} requires a value"))?;
            if value.starts_with("--") {
                return Err(format!("{name} requires a value"));
            }
            let values = options.entry(name.clone()).or_insert_with(Vec::new);
            if !values.is_empty() && name != "--seal" {
                return Err(format!("duplicate option: {name}"));
            }
            values.push(value.clone());
            index += 2;
        }
        Ok(Self { command, options })
    }

    pub fn get(&self, name: &str) -> Option<&str> {
        self.options
            .get(name)
            .and_then(|values| values.first())
            .map(String::as_str)
    }

    pub fn values(&self, name: &str) -> &[String] {
        self.options
            .get(name)
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    pub fn required(&self, name: &str) -> Result<&str, String> {
        self.get(name)
            .ok_or_else(|| format!("missing required option {name}"))
    }

    pub fn logical(&self, name: &str) -> Result<u64, String> {
        self.required(name)?
            .parse()
            .map_err(|_| format!("{name} must be an unsigned logical clock value"))
    }

    pub fn optional_logical(&self, name: &str) -> Result<Option<u64>, String> {
        self.get(name)
            .map(|value| {
                value
                    .parse()
                    .map_err(|_| format!("{name} must be an unsigned logical clock value"))
            })
            .transpose()
    }
}

pub struct Paths {
    pub store: PathBuf,
    pub vault: PathBuf,
    pub identity: PathBuf,
}

impl Paths {
    pub fn from_args(args: &CliArgs) -> Self {
        let store = PathBuf::from(args.get("--store").unwrap_or("events.jsonl"));
        let vault = PathBuf::from(args.get("--vault").unwrap_or("ghost-vault"));
        let identity = args
            .get("--identity")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let mut path = store.as_os_str().to_os_string();
                path.push(".identity");
                PathBuf::from(path)
            });
        Self {
            store,
            vault,
            identity,
        }
    }
}

pub fn open_store(path: &Path) -> Result<FileEventStore, String> {
    ensure_parent(path)?;
    FileEventStore::open(path).map_err(|error| error.to_string())
}

pub fn load_or_create_identity(path: &Path, actor_id: &str) -> Result<Identity, String> {
    ensure_parent(path)?;
    if path.exists() {
        let encoded = Zeroizing::new(fs::read_to_string(path).map_err(|error| error.to_string())?);
        let mut decoded = hex::decode(encoded.trim()).map_err(|error| error.to_string())?;
        if decoded.len() != 32 {
            decoded.zeroize();
            return Err("identity file must contain a 32-byte Ed25519 seed in hex".into());
        }
        let mut seed = [0_u8; 32];
        seed.copy_from_slice(&decoded);
        decoded.zeroize();
        let identity = Identity::from_seed(actor_id, &seed);
        seed.zeroize();
        restrict_file(path)?;
        return Ok(identity);
    }

    let identity = Identity::generate(actor_id);
    let mut seed = identity.seed_bytes();
    let encoded = Zeroizing::new(hex::encode(seed));
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
    if let Err(error) = result {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            return load_or_create_identity(path, actor_id);
        }
        return Err(error.to_string());
    }
    restrict_file(path)?;
    Ok(identity)
}

pub fn read_ghost_key(path: &Path, scope: &str) -> Result<GhostKey, String> {
    let encoded = Zeroizing::new(fs::read_to_string(path).map_err(|error| error.to_string())?);
    let mut decoded = hex::decode(encoded.trim()).map_err(|error| error.to_string())?;
    if decoded.len() != 32 {
        decoded.zeroize();
        return Err("scope key file must contain exactly 32 bytes in hex".into());
    }
    let mut key = [0_u8; 32];
    key.copy_from_slice(&decoded);
    decoded.zeroize();
    let result = GhostKey::new(scope, key).map_err(|error| error.to_string());
    key.zeroize();
    result
}

pub fn prepare_event(
    store: &FileEventStore,
    identity: &Identity,
    event_type: &str,
    payload: Value,
) -> Result<Event, String> {
    let events = store.all().map_err(|error| error.to_string())?;
    let previous = events.iter().max_by_key(|event| event.sequence_number);
    let (logical_clock, sequence_number, parents) = match previous {
        Some(event) => (
            event
                .logical_clock
                .checked_add(1)
                .ok_or_else(|| "logical clock overflow".to_owned())?,
            event
                .sequence_number
                .checked_add(1)
                .ok_or_else(|| "event sequence overflow".to_owned())?,
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
        timestamp_now()?,
        parents,
        payload,
        Vec::new(),
        Vec::new(),
    ))
}

pub fn append_event(
    store: &mut FileEventStore,
    identity: &Identity,
    event_type: &str,
    payload: Value,
) -> Result<Event, String> {
    let event = prepare_event(store, identity, event_type, payload)?;
    store
        .append(event.clone())
        .map_err(|error| error.to_string())?;
    Ok(event)
}

fn ensure_parent(path: &Path) -> Result<(), String> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn restrict_file(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}
