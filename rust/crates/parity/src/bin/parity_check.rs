use eventcontracts_parity::{
    DynStrategyParityRunner, JsonParityCaseLoader, ParityCaseLoader, ParityRunner,
};
use eventcontracts_risk::SleeveState;
use eventcontracts_runner::{default_registry, StrategyContext, StrategySpecArtifact};
use std::env;
use std::process::ExitCode;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::from(1)
        }
    }
}

fn run() -> Result<(), String> {
    let mut strategy_spec = None;
    let mut cases = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--strategy-spec" => strategy_spec = args.next(),
            "--cases" => cases = args.next(),
            "--help" | "-h" => {
                print_help();
                return Ok(());
            }
            other => return Err(format!("unknown argument `{other}`")),
        }
    }

    let strategy_spec = strategy_spec.ok_or("missing --strategy-spec")?;
    let cases = cases.ok_or("missing --cases")?;
    let spec = StrategySpecArtifact::load(&strategy_spec)
        .map_err(|e| format!("load strategy spec `{strategy_spec}`: {e}"))?;
    let registry = default_registry();
    let strategy = registry
        .instantiate(&spec)
        .map_err(|e| format!("instantiate `{}`: {e}", spec.name))?;
    let loader = JsonParityCaseLoader;
    let cases = loader
        .load_cases(&cases)
        .map_err(|e| format!("load parity cases: {e:?}"))?;
    if cases.is_empty() {
        return Err("no parity cases loaded".into());
    }
    let ctx = StrategyContext::from_sleeve_state("2026-05-26T12:00:00Z", &SleeveState::default());
    let mut runner = DynStrategyParityRunner { strategy, ctx };
    let results = runner
        .run_all(&cases)
        .map_err(|e| format!("run parity cases: {e:?}"))?;
    let mut failures = 0;
    for result in &results {
        if result.passed {
            println!("PASS {}", result.case_id);
        } else {
            failures += 1;
            println!("FAIL {} {}", result.case_id, result.differences.join("; "));
        }
    }
    if failures > 0 {
        return Err(format!(
            "parity failed: {failures}/{} case(s) failed",
            results.len()
        ));
    }
    println!("OK parity: {} case(s)", results.len());
    Ok(())
}

fn print_help() {
    println!("eventcontracts-parity-check --strategy-spec <path> --cases <json-file-or-dir>");
}
