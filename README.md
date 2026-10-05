# DisputeResolver

Decentralized Dispute Resolution powered by AI consensus.

## Deployed Contract

- **Address:** `0x9Bffb49ACF7727a14d247F220dab9251aDf7D3cA`
- **Explorer:** https://explorer-studio.genlayer.com/address/0x9Bffb49ACF7727a14d247F220dab9251aDf7D3cA

## Overview

DisputeResolver lets two parties lock escrow and resolve a dispute through
AI consensus. Each party submits their claim as plain text stored verbatim
on-chain. A committee of AI validators independently reads both claims from
chain state and evaluates them -- no external URLs, no fetchable pages, no
claims that cannot be verified. The escrow is released only after the appeal
window closes.

## Lifecycle

```
evidence  ->  verdict  ->  appealed  ->  verdict  ->  closed
   |            |                          |
   +-- cancel   +-- execute (window over)  +-- re-resolve after bond
```

1. **create_dispute** - Creator locks escrow and names a counterparty
2. **submit_claim** - Each party submits claim text (once each)
3. **close_evidence** - Evidence closes once both claims are in
4. **resolve_dispute** - AI consensus records a binding verdict
5. **appeal_verdict** - Either party may appeal with a 5% bond (max 3)
6. **execute_verdict** - After the appeal window, escrow is released

## Design Principles

1. **No external URLs.** Evidence is plain text submitted directly in the
   transaction and stored on-chain verbatim. Nothing to fetch, nothing to
   spoof.
2. **Independent evaluation.** Each validator reads both parties' claims
   from chain state and runs its own LLM evaluation. Validators never see
   each other's reasoning.
3. **Exact binding on the verdict.** The validator must independently
   derive the same winning party as the leader. No tolerance -- the verdict
   directly controls who receives the escrow.
4. **Deferred atomic payout.** `resolve_dispute` only records the verdict.
   `execute_verdict` releases escrow in one transaction after the appeal
   window expires, guarded by `payout_executed` so it can never run twice.

## Contract Methods

### Write Methods

| Method | Description |
|--------|-------------|
| `create_dispute(counterparty, description)` | Lock escrow, open dispute (payable) |
| `submit_claim(dispute_id, claim)` | Submit claim text (once per party) |
| `close_evidence(dispute_id)` | Close evidence phase |
| `resolve_dispute(dispute_id)` | Run AI consensus to record verdict |
| `appeal_verdict(dispute_id)` | Appeal with 5% bond (payable) |
| `execute_verdict(dispute_id)` | Release escrow after appeal window |
| `cancel_dispute(dispute_id)` | Cancel during evidence, escrow refunded |

### View Methods

| Method | Description |
|--------|-------------|
| `get_dispute(dispute_id)` | Full dispute state |
| `get_dispute_count()` | Total disputes created |
| `get_statuses()` | Valid lifecycle statuses |
| `get_constants()` | Configured deadlines and limits |
| `get_active_disputes(address)` | Active disputes involving an address |

## Running Tests

### Integration Tests (studionet)

```bash
cd dispute-resolver
python -m pytest tests/integration/ -v
```

### Final Deployment Test (deploy + test all methods)

```bash
cd dispute-resolver
python -m pytest tests/integration/test_final_deploy.py -v -s
```

## Architecture

```
dispute-resolver/
├── contracts/
│   └── dispute_resolver.py       # Main intelligent contract
├── tests/
│   └── integration/
│       ├── conftest.py           # RPC rate-limit throttle
│       ├── test_final_deploy.py  # Deploy + test all methods
│       └── test_appeal_flow.py   # Appeal lifecycle
├── deployment.toml
├── gltest.config.yaml
├── requirements.txt
└── README.md
```

## Trust Model

- Claims are stored on-chain verbatim; validators evaluate them directly
- Verdict must match exactly across leader and every validator
- Escrow releases only after the appeal window expires (no premature payout)
- `payout_executed` guard prevents any double payout
- Appeal bonds discourage frivolous appeals; max 3 appeals per dispute
- Active-dispute counters decrement on close/cancel

## License

MIT
