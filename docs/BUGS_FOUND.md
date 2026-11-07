# Bugs found

Provenance matters here, so bugs are split by who found them first:

- **Part A: audit-probe bugs.** Found by reading the code and probing it by hand (see [AUDIT.md](AUDIT.md)), before the simulator existed. The simulator then re-found each one on the unfixed engine.
- **Part B: simulator bugs.** First found by the simulator, with nobody having predicted them.

The simulator's invariants were written after the audit, by the same author. Part A shows the simulator can reach these bugs and recognise them, not that it found them blind.

## Part A: audit-probe bugs

### Rediscovery on the unfixed engine

Run at commit `005e746`, before any fix, with `python -m aegisflow.sim survey --seeds 6000`. It took 404.8 s. "Seeds to find" counts from seed 0 through the first failing seed.

| Audit | Bug | Invariant that caught it | First seed | Seeds to find | Failing seeds of 6,000 |
|---|---|---|---|---|---|
| P5 | Opening a connection takes the write lock, even to read | `read_path_writes` | 2 | 3 | 2,338 |
| P3 | A repeated request refunds an order twice | `refund_cap` | 3 | 4 | 908 |
| P3 | The same request runs as two runs | `request_ran_twice` | 6 | 7 | 318 |
| P6 | `notify_customer` delivers twice after a crash | `notified_twice` | 3 | 4 | 350 |
| P2 | A driver writes after losing its lease | `stale_write` | 13 | 14 | 28 |
| P2 | Same cause: a succeeded step overwritten as failed | `step_regressed` | 13 | 14 | 2 |
| P2 | Same cause: a run with two terminal events | `history_mismatch` | 551 | 552 | 3 |
| P2 | Same cause: two drivers race for a step index, HTTP 500 | `integrity_error` | 2951 | 2,952 | 4 |
| P7 | A run fails after its refund committed, without saying so | `undisclosed_effect_before_failure` | 14 | 15 | 170 |
| P1 | A committed refund, then a fatal error, reports plain `failed` | `undisclosed_effect_on_failed_step` | 256 | 257 | 19 |
| P4 | A retry skips its backoff after a crash or takeover | `backoff_skipped` | 1545 | 1,546 | 3 |

To replay any of these, check out `005e746` and run `python -m aegisflow.sim replay --seed <seed> --trace`.

**A gap in the rediscovery of P2.** The audit probe showed a *finished run* flipping from `succeeded` to `failed`. The `terminal_changed` invariant watches for that and did not fire in these 6,000 seeds. The seeds above show the same root cause through a step regressing and through two terminal events on one run, but not that exact symptom.
