#[allow(dead_code)]
mod common;

use common::CliArgs;
use molt_kernel::identity::Identity;
use molt_kernel::molt_audit::{
    generate_actor_identity, load_actor_identity, load_witness_identity, MoltAudit,
};
use serde_json::json;
use std::env;
use std::path::{Path, PathBuf};

const AUDIT_SCOPE: &str = "molt-audit";

fn print_json(value: impl serde::Serialize) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?
    );
    Ok(())
}

fn audit_paths(args: &CliArgs) -> (PathBuf, PathBuf) {
    (
        PathBuf::from(args.get("--store").unwrap_or("events.jsonl")),
        PathBuf::from(args.get("--vault").unwrap_or("ghost-audit-vault")),
    )
}

fn open_audit(args: &CliArgs) -> Result<MoltAudit, String> {
    let (store, vault) = audit_paths(args);
    MoltAudit::open(store, vault).map_err(|error| error.to_string())
}

fn actor_identity(args: &CliArgs, actor: &str) -> Result<Identity, String> {
    match args.get("--actor-key") {
        Some(path) => {
            if !Path::new(path).exists() {
                return Err(format!("actor identity file does not exist: {path}"));
            }
            load_actor_identity(path, actor).map_err(|error| error.to_string())
        }
        None => Ok(generate_actor_identity(actor)),
    }
}

fn report_error_json(error: &str) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string(&json!({ "verdict": "FAILED", "error": error }))
            .map_err(|serialization_error| serialization_error.to_string())?
    );
    Err("audit command failed".into())
}

fn run() -> Result<(), String> {
    let raw_args = env::args().skip(1).collect::<Vec<_>>();
    let args = CliArgs::parse(&raw_args)?;
    match args.command.as_str() {
        "ingest" => {
            let actor = args.required("--actor")?;
            let identity = actor_identity(&args, actor)?;
            let key = common::read_ghost_key(Path::new(args.required("--key")?), AUDIT_SCOPE)?;
            let mut audit = open_audit(&args)?;
            let summary = audit
                .ingest_file(
                    args.required("--in")?,
                    &identity,
                    &key,
                    args.values("--seal"),
                )
                .map_err(|error| error.to_string())?;
            print_json(summary)
        }
        "verify" => {
            let audit = open_audit(&args)?;
            match audit.verify() {
                Ok(summary) => print_json(summary),
                Err(error) => report_error_json(&error.to_string()),
            }
        }
        "replay" => {
            let audit = open_audit(&args)?;
            match audit.replay_at(args.required("--at")?) {
                Ok(summary) => print_json(summary),
                Err(error) => report_error_json(&error.to_string()),
            }
        }
        "export" => {
            let audit = open_audit(&args)?;
            let witness = args
                .get("--witness-key")
                .map(load_witness_identity)
                .transpose()
                .map_err(|error| error.to_string())?;
            match audit.export(args.required("--out")?, witness.as_ref()) {
                Ok(package) => print_json(json!({
                    "path": args.required("--out")?,
                    "final_hash": package.final_hash,
                })),
                Err(error) => report_error_json(&error.to_string()),
            }
        }
        "sealed-open" => {
            let audit = open_audit(&args)?;
            let key = common::read_ghost_key(Path::new(args.required("--key")?), AUDIT_SCOPE)?;
            let now = args.logical("--now")?;
            match audit.sealed_open(args.required("--id")?, args.required("--field")?, &key, now) {
                Ok(value) => print_json(json!({
                    "event_id": args.required("--id")?,
                    "field": args.required("--field")?,
                    "value": value,
                })),
                Err(error) => report_error_json(&error.to_string()),
            }
        }
        "report" => {
            let audit = open_audit(&args)?;
            match audit.report() {
                Ok(report) => print_json(report),
                Err(error) => report_error_json(&error.to_string()),
            }
        }
        _ => Err(format!("unknown command: {}", args.command)),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
