# Effect and Recovery Contract

## Rule zero

Never turn uncertainty into a retry decision by assumption.

For a mutating tool call, these are separate facts:

```text
REQUEST
AUTHORITY
ATTEMPT
EFFECT
VERIFIED_EFFECT
```

No item silently proves the next.

## Mutation admission

Before invoking a mutating handler, Pro-Run persists:

- `request_id`;
- tool name;
- canonical request JSON;
- request SHA-256;
- state `EXECUTING`.

That write happens before handler dispatch. If the ledger cannot admit the request, the mutation is not invoked.

## Idempotent replay

If the same `request_id` reappears:

- same tool + same canonical input + `COMMITTED` -> return stored result, do not invoke handler;
- different tool or input -> `IdempotencyConflict`;
- unresolved state -> `AmbiguousEffect`;
- `RECONCILED_NO_EFFECT` -> the exact persisted request may be retried.

A request ID is therefore an effect identity, not a casual correlation ID.

## Ambiguous outcome

A handler exception is conservatively treated as an unknown outcome because the handler may have dispatched an external effect before the local failure became visible.

The ledger records `ATTEMPTED_UNKNOWN`. The engine moves the run to `BLOCKED_EFFECT` and removes automatic queue pressure for that run.

This prevents this unsafe sequence:

```text
mutation dispatched
-> response lost
-> event retried
-> model emits a fresh request ID
-> mutation happens twice
```

## Reconciliation

A host calls `ToolRegistry.reconcile` with a SHA-256 digest of external reconciliation evidence and one of two claims.

### Effect confirmed

`effect_occurred=True` requires a result object. Pro-Run stores that result and moves the request to `COMMITTED`. Resuming the run replays the committed result into the durable transcript without repeating the handler.

### No effect confirmed

`effect_occurred=False` forbids a result object. Pro-Run moves the request to `RECONCILED_NO_EFFECT`. Resuming the run invokes the **exact stored request ID, tool name, and arguments**. It does not ask the model to formulate a replacement call first.

## Evidence ceiling

The reconciliation evidence digest proves only that the host supplied a binding to some external evidence. Pro-Run V1 does not independently validate the truth of that evidence. A production host should bind reconciliation to a trusted readback/verifier appropriate to the target system.

## Host responsibilities

A tool adapter that mutates an external system should prefer:

- target-native idempotency keys when available;
- compare-and-swap or generation checks;
- transaction boundaries;
- exact target identifiers;
- bounded timeouts;
- readback after write;
- reconciliation probes that are independent from the original response path.

Do not use the Pro-Run request ledger as a substitute for target-native transactional guarantees when those exist.
