#[allow(dead_code)]
mod common;

use common::{CliArgs, Paths};
use molt_kernel::ghost_bid::GhostBid;
use molt_kernel::store::EventStore;
use serde_json::json;
use std::env;

fn usage() {
    eprintln!("usage: ghost-bid <create|bid|close|reveal|winner|shred-losing|verify> [options]\ncommon options: --store PATH --vault PATH [--identity PATH]");
}

fn print_json<T: serde::Serialize>(value: T) -> Result<(), String> {
    println!(
        "{}",
        serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?
    );
    Ok(())
}

fn run() -> Result<(), String> {
    let mut raw_args: Vec<String> = env::args().skip(1).collect();
    let include_winner = raw_args.iter().any(|arg| arg == "--include-winner");
    raw_args.retain(|arg| arg != "--include-winner");
    let args = CliArgs::parse(&raw_args)?;
    if include_winner && args.command != "shred-losing" {
        return Err("--include-winner is only valid with shred-losing".into());
    }
    let paths = Paths::from_args(&args);
    let store = common::open_store(&paths.store)?;
    if !paths.identity.exists() && !store.all().map_err(|error| error.to_string())?.is_empty() {
        return Err("vault signing identity is missing while the event store is non-empty".into());
    }
    drop(store);
    let identity = common::load_or_create_identity(&paths.identity, "ghost-bid")?;
    let mut auction =
        GhostBid::open(&paths.store, &paths.vault, identity).map_err(|error| error.to_string())?;

    match args.command.as_str() {
        "create" => {
            let result = auction
                .create(
                    args.required("--auction")?,
                    args.logical("--close")?,
                    args.optional_logical("--reveal-window")?,
                )
                .map_err(|error| error.to_string())?;
            print_json(result)
        }
        "bid" => {
            let bidder = args.required("--bidder")?;
            let key =
                common::read_ghost_key(std::path::Path::new(args.required("--key")?), bidder)?;
            let result = auction
                .bid(
                    args.required("--auction")?,
                    args.logical("--amount")?,
                    bidder,
                    &key,
                    args.logical("--now")?,
                )
                .map_err(|error| error.to_string())?;
            print_json(result)
        }
        "close" => {
            let result = auction
                .close(args.required("--auction")?, args.logical("--now")?)
                .map_err(|error| error.to_string())?;
            print_json(result)
        }
        "reveal" => {
            let bidder = args.required("--bidder")?;
            let key =
                common::read_ghost_key(std::path::Path::new(args.required("--key")?), bidder)?;
            let amount = auction
                .reveal(
                    args.required("--auction")?,
                    bidder,
                    &key,
                    args.logical("--now")?,
                )
                .map_err(|error| error.to_string())?;
            print_json(json!({ "amount": amount }))
        }
        "winner" => {
            let selection = auction
                .winner(args.required("--auction")?)
                .map_err(|error| error.to_string())?;
            match (selection.winner, selection.excluded_reveals.is_empty()) {
                (Some(winner), true) => print_json(winner),
                (None, true) => print_json("no valid reveals"),
                (winner, false) => print_json(json!({
                    "winner": winner,
                    "excluded_reveals": selection.excluded_reveals,
                })),
            }
        }
        "shred-losing" => {
            let ids = auction
                .shred_losing(args.required("--auction")?, include_winner)
                .map_err(|error| error.to_string())?;
            print_json(ids)
        }
        "verify" => {
            auction
                .verify(args.required("--auction")?)
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
