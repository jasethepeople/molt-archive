#[allow(dead_code)]
mod common;

use common::CliArgs;
use molt_kernel::identity::Identity;
use molt_kernel::molt_receipts::{verify_receipt_file, MoltReceipts};
use serde_json::json;
use std::env;
use std::path::{Path, PathBuf};

fn print_json(value: impl serde::Serialize) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?
    );
    Ok(())
}

fn with_receipts<T>(
    args: &CliArgs,
    action: impl FnOnce(&mut MoltReceipts) -> Result<T, String>,
) -> Result<T, String> {
    let store = PathBuf::from(args.get("--store").unwrap_or("events.jsonl"));
    let vault = args
        .get("--vault")
        .map(PathBuf::from)
        .unwrap_or_else(|| store.with_extension("ghost-receipts"));
    let key_dir = args
        .get("--agent-keys")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let mut path = store.as_os_str().to_os_string();
            path.push(".molt-receipts-agent-keys");
            PathBuf::from(path)
        });
    let identity_path = args
        .get("--runner-key")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("{}.molt-receipts-runner.key", store.display())));
    let runner = common::load_or_create_identity(&identity_path, "molt-receipts-runner")?;
    let mut receipts =
        MoltReceipts::open(store, vault, key_dir, runner).map_err(|error| error.to_string())?;
    action(&mut receipts)
}

fn identity_from_seed_file(
    path: &Path,
    actor: &str,
    create_if_missing: bool,
) -> Result<Identity, String> {
    if !path.exists() {
        if !create_if_missing {
            return Err(format!(
                "witness identity file does not exist: {}",
                path.display()
            ));
        }
        return common::load_or_create_identity(path, actor);
    }
    common::load_or_create_identity(path, actor)
}

fn run() -> Result<(), String> {
    let raw_args = env::args().skip(1).collect::<Vec<_>>();
    let args = CliArgs::parse(&raw_args)?;
    match args.command.as_str() {
        "start" => {
            let job = args.required("--job")?;
            let scope = format!("job:{job}");
            let key = common::read_ghost_key(Path::new(args.required("--key")?), &scope)?;
            let result = with_receipts(&args, |receipts| {
                receipts
                    .start(
                        job,
                        args.required("--agent")?,
                        args.required("--client")?,
                        &key,
                        args.get("--prompt"),
                    )
                    .map_err(|error| error.to_string())
            })?;
            print_json(result)
        }
        "log" => {
            let result = with_receipts(&args, |receipts| {
                receipts
                    .log_step(
                        args.required("--job")?,
                        args.required("--step")?,
                        args.get("--input-hash"),
                        args.get("--output-hash"),
                        args.get("--tier"),
                    )
                    .map_err(|error| error.to_string())
            })?;
            print_json(result)
        }
        "finish" => {
            let result = with_receipts(&args, |receipts| {
                receipts
                    .finish(args.required("--job")?)
                    .map_err(|error| error.to_string())
            })?;
            print_json(result)
        }
        "receipt" => {
            let job = args.required("--job")?;
            let witness_key_path = PathBuf::from(args.required("--witness-key")?);
            let witness = match args.get("--witness-id") {
                Some(path) => identity_from_seed_file(Path::new(path), "molt-witness", false)?,
                None => identity_from_seed_file(&witness_key_path, "molt-witness", true)?,
            };
            let output = args
                .get("--out")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(format!("{job}.receipt.json")));
            let receipt = with_receipts(&args, |receipts| {
                receipts
                    .create_receipt(job, &witness, &output)
                    .map_err(|error| error.to_string())
            })?;
            print_json(json!({ "path": output, "final_hash": receipt.final_hash }))
        }
        "verify" => {
            let path = args.required("--receipt")?;
            match verify_receipt_file(
                path,
                args.get("--agent-pubkey"),
                args.get("--witness-pubkey"),
            ) {
                Ok(verified) => print_json(json!({
                    "verdict": "VERIFIED",
                    "steps": verified.steps,
                    "total_gas": verified.total_gas,
                    "final_hash": verified.final_hash,
                })),
                Err(error) => {
                    println!(
                        "{}",
                        serde_json::to_string(&json!({
                            "verdict": "FAILED",
                            "error": error.to_string(),
                        }))
                        .map_err(|serialization_error| serialization_error.to_string())?
                    );
                    Err("receipt verification failed".into())
                }
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
