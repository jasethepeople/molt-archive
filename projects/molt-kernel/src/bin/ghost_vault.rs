#[allow(dead_code)]
mod common;

use common::{CliArgs, Paths};
use molt_kernel::ghost_vault::GhostVault;
use serde::Serialize;
use serde_json::json;
use std::env;
use std::path::PathBuf;

fn print_json<T: Serialize>(value: T) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?
    );
    Ok(())
}

fn usage() {
    eprintln!("usage: ghost-vault <seal|open|shred|list|verify> [options]\ncommon options: --store PATH --vault PATH [--identity PATH]");
}

fn run() -> Result<(), String> {
    let raw_args: Vec<String> = env::args().skip(1).collect();
    let args = CliArgs::parse(&raw_args)?;
    let paths = Paths::from_args(&args);
    let mut vault = GhostVault::open(&paths.store, &paths.vault, &paths.identity)
        .map_err(|error| error.to_string())?;

    match args.command.as_str() {
        "seal" => {
            let result = vault
                .seal(
                    args.required("--in")?,
                    args.required("--scope")?,
                    args.required("--key")?,
                    args.optional_logical("--release-after")?,
                    args.optional_logical("--expires")?,
                    args.get("--title"),
                )
                .map_err(|error| error.to_string())?;
            print_json(result)
        }
        "open" => {
            let event_id = args.required("--id")?;
            let now = args.logical("--now")?;
            let output_path = args.get("--out").map(PathBuf::from);
            let result = vault
                .open_record(
                    event_id,
                    args.required("--key")?,
                    now,
                    output_path.as_deref(),
                )
                .map_err(|error| error.to_string())?;
            print_json(result)
        }
        "shred" => {
            let event_id = args.required("--id")?;
            vault.shred(event_id).map_err(|error| error.to_string())?;
            print_json(json!({ "shredded": event_id }))
        }
        "list" => {
            let records = vault
                .list(args.optional_logical("--now")?)
                .map_err(|error| error.to_string())?;
            print_json(records)
        }
        "verify" => {
            vault
                .verify(args.required("--id")?, args.required("--key")?)
                .map_err(|error| error.to_string())?;
            print_json("VERIFIED")
        }
        _ => {
            usage();
            Err(format!("unknown command: {}", args.command))
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
