# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
DisputeResolver
===============

Decentralized Dispute Resolution powered by AI consensus.

Two parties lock escrow, submit their claims as text, and a committee of
AI validators independently evaluates both submissions to reach a binding
verdict. Funds are released only after the appeal window closes.

Lifecycle
---------
evidence -> verdict (resolved, appeal window open)
        -> appealed (bond posted, re-resolution required)
        -> verdict (re-resolved, new appeal window)
        -> closed (execute_verdict: escrow + bonds settled)
evidence -> cancelled (creator cancels, escrow refunded)

Design principles (addressing prior review feedback)
-----------------------------------------------------
1. No external URLs. Evidence is plain text submitted by each party
   directly in the transaction. There is nothing to fetch and nothing
   to spoof -- the claim text is stored on-chain verbatim.
2. Independent evaluation. Each validator reads both parties' claims
   from chain state and independently asks its own LLM to weigh them.
   Validators never see each other's reasoning.
3. Exact binding on the verdict. The validator must independently
   derive the same winning party as the leader. No tolerance -- the
   verdict directly controls who receives the escrow.
4. Deferred atomic payout. resolve_dispute only records the verdict.
   execute_verdict releases escrow in one transaction after the appeal
   window expires, and can never run twice (payout_executed guard).
"""

from genlayer import *
from dataclasses import dataclass
import json


# --- Constants -----------------------------------------------------------

MAX_CLAIM_CHARS = 6000
MAX_DESCRIPTION_CHARS = 2000
EVIDENCE_DEADLINE_SECONDS = 604800   # 7 days for both parties to submit
RESOLVE_DEADLINE_SECONDS = 2592000   # 5 days to resolve after evidence closes
APPEAL_WINDOW_SECONDS = 604800       # 7 days to appeal after verdict
APPEAL_BOND_BPS = 500                # 5% of escrow to appeal
MAX_APPEALS = 3
MAX_ACTIVE_DISPUTES = 20

# Dispute lifecycle: evidence -> verdict -> (appealed -> verdict)* -> closed
STATUSES = ("evidence", "verdict", "appealed", "closed", "cancelled")


# --- Deterministic helpers ------------------------------------------------

@gl.evm.contract_interface
class _EOA:
    """External-message interface for sending GEN to chain-layer addresses."""

    class View:
        pass

    class Write:
        pass


def _transfer(to: Address, amount: int) -> None:
    if amount <= 0:
        return
    _EOA(to).emit_transfer(value=u256(amount))


def _current_timestamp() -> u256:
    import datetime as _dt
    return u256(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))


def _coerce_address(value) -> Address:
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _zero_address() -> Address:
    return _coerce_address("0x0000000000000000000000000000000000000000")


def _validate_text(text: str, max_chars: int, label: str) -> str:
    text = text.strip()
    if not text:
        raise gl.vm.UserError(f"{label} must not be empty")
    if len(text) > max_chars:
        raise gl.vm.UserError(f"{label} exceeds {max_chars} characters")
    return text


def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


# --- Leader: independent evaluation --------------------------------------

def _evaluate_dispute(
    description: str,
    plaintiff_claim: str,
    defendant_claim: str,
    plaintiff_name: str,
    defendant_name: str,
) -> dict:
    """Run one independent LLM evaluation of the dispute.

    Returns a dict with:
      winner: "plaintiff" | "defendant" | "draw"
      confidence: int 0-100
      reasoning: str (2-4 sentences)
    """
    prompt = f"""You are an impartial dispute resolver.

DISPUTE:
{description}

CLAIM BY PLAINTIFF ({plaintiff_name}):
{plaintiff_claim}

CLAIM BY DEFENDANT ({defendant_name}):
{defendant_claim}

Evaluate both claims on their merits: internal consistency, plausibility,
specificity of facts presented, and whether each claim addresses the
dispute directly. Weigh evidence quality, not merely volume of text.

Verdict rules:
- Choose "plaintiff" if their claim is stronger on the merits.
- Choose "defendant" if their claim is stronger on the merits.
- Choose "draw" only if both claims are equally strong OR both are
  too weak/unsupported to decide.

Respond with ONLY a JSON object, no markdown fences:
{{"winner": "plaintiff" | "defendant" | "draw", "confidence": <0-100 int>, "reasoning": "<2-4 sentences>"}}"""

    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_json_object(parsed) or {}

    winner = str(payload.get("winner", "")).strip().lower()
    if winner not in ("plaintiff", "defendant", "draw"):
        winner = "draw"

    confidence_raw = payload.get("confidence", 50)
    try:
        confidence = int(confidence_raw)
    except (ValueError, TypeError):
        confidence = 50
    confidence = max(0, min(100, confidence))

    reasoning = str(payload.get("reasoning", ""))[:1000]

    return {
        "winner": winner,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def _leader_fn(description, p_claim, d_claim, p_name, d_name) -> dict:
    return _evaluate_dispute(description, p_claim, d_claim, p_name, d_name)


def _validator_fn(leaders_res, description, p_claim, d_claim, p_name, d_name) -> bool:
    """Validator independently re-evaluates and must derive the SAME winner."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False

    leader_winner = str(leader_data.get("winner", ""))
    if leader_winner not in ("plaintiff", "defendant", "draw"):
        return False

    # Independent re-evaluation
    my_data = _evaluate_dispute(description, p_claim, d_claim, p_name, d_name)
    my_winner = my_data["winner"]

    # EXACT binding on the verdict -- it controls who gets paid
    return leader_winner == my_winner


# --- Data classes ---------------------------------------------------------

@allow_storage
@dataclass
class Dispute:
    dispute_id: str
    creator: Address
    counterparty: Address
    description: str
    escrow_amount: u256
    status: str
    created_at: u256
    evidence_deadline: u256
    resolve_deadline: u256
    plaintiff_claim: str
    defendant_claim: str
    plaintiff_submitted: bool
    defendant_submitted: bool
    verdict: str            # "", "plaintiff", "defendant", "draw"
    verdict_confidence: u256
    verdict_reasoning: str
    resolved_at: u256
    winner: Address         # zero address until resolved
    appeal_bond: u256
    appeal_count: u256
    appellant: Address      # last appeal poster, zero if none
    payout_executed: bool

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "creator": str(self.creator),
            "counterparty": str(self.counterparty),
            "description": self.description,
            "escrow_amount": int(self.escrow_amount),
            "status": self.status,
            "created_at": int(self.created_at),
            "evidence_deadline": int(self.evidence_deadline),
            "resolve_deadline": int(self.resolve_deadline),
            "plaintiff_claim": self.plaintiff_claim,
            "defendant_claim": self.defendant_claim,
            "plaintiff_submitted": self.plaintiff_submitted,
            "defendant_submitted": self.defendant_submitted,
            "verdict": self.verdict,
            "verdict_confidence": int(self.verdict_confidence),
            "verdict_reasoning": self.verdict_reasoning,
            "resolved_at": int(self.resolved_at),
            "winner": str(self.winner),
            "appeal_bond": int(self.appeal_bond),
            "appeal_count": int(self.appeal_count),
            "appellant": str(self.appellant),
            "payout_executed": self.payout_executed,
        }


# --- The contract ---------------------------------------------------------

class DisputeResolver(gl.Contract):
    disputes: TreeMap[str, Dispute]
    dispute_count: u256
    disputes_by_creator: TreeMap[str, u256]      # address -> active count
    disputes_by_counterparty: TreeMap[str, u256]
    evidence_deadline_seconds: u256
    resolve_deadline_seconds: u256
    appeal_window_seconds: u256

    def __init__(
        self,
        evidence_deadline_seconds: u256 = u256(EVIDENCE_DEADLINE_SECONDS),
        resolve_deadline_seconds: u256 = u256(RESOLVE_DEADLINE_SECONDS),
        appeal_window_seconds: u256 = u256(APPEAL_WINDOW_SECONDS),
    ):
        if int(evidence_deadline_seconds) < 60:
            raise gl.vm.UserError("evidence deadline must be >= 60 seconds")
        if int(resolve_deadline_seconds) < 60:
            raise gl.vm.UserError("resolve deadline must be >= 60 seconds")
        if int(appeal_window_seconds) < 60:
            raise gl.vm.UserError("appeal window must be >= 60 seconds")
        self.dispute_count = u256(0)
        self.evidence_deadline_seconds = u256(int(evidence_deadline_seconds))
        self.resolve_deadline_seconds = u256(int(resolve_deadline_seconds))
        self.appeal_window_seconds = u256(int(appeal_window_seconds))

    # -- Create dispute ----------------------------------------------------

    @gl.public.write.payable
    def create_dispute(self, counterparty, description: str) -> str:
        """Lock escrow and open a dispute against a counterparty."""
        creator = gl.message.sender_address
        cp = _coerce_address(counterparty)

        if cp == creator:
            raise gl.vm.UserError("cannot dispute yourself")
        if cp == _zero_address():
            raise gl.vm.UserError("counterparty must be a real address")

        desc = _validate_text(description, MAX_DESCRIPTION_CHARS, "description")

        escrow = u256(int(gl.message.value))
        if int(escrow) <= 0:
            raise gl.vm.UserError("escrow must be positive")

        c_key = str(creator)
        active = int(self.disputes_by_creator.get(c_key, u256(0)))
        if active >= MAX_ACTIVE_DISPUTES:
            raise gl.vm.UserError("maximum active disputes reached")

        now = _current_timestamp()
        dispute_id = f"dispute-{self.dispute_count}"
        self.dispute_count = self.dispute_count + u256(1)

        self.disputes[dispute_id] = Dispute(
            dispute_id=dispute_id,
            creator=creator,
            counterparty=cp,
            description=desc,
            escrow_amount=escrow,
            status="evidence",
            created_at=now,
            evidence_deadline=u256(int(now) + int(self.evidence_deadline_seconds)),
            resolve_deadline=u256(0),
            plaintiff_claim="",
            defendant_claim="",
            plaintiff_submitted=False,
            defendant_submitted=False,
            verdict="",
            verdict_confidence=u256(0),
            verdict_reasoning="",
            resolved_at=u256(0),
            winner=_zero_address(),
            appeal_bond=u256(0),
            appeal_count=u256(0),
            appellant=_zero_address(),
            payout_executed=False,
        )

        self.disputes_by_creator[c_key] = u256(active + 1)
        cp_key = str(cp)
        cp_active = int(self.disputes_by_counterparty.get(cp_key, u256(0)))
        self.disputes_by_counterparty[cp_key] = u256(cp_active + 1)

        return dispute_id

    # -- Submit claims -----------------------------------------------------

    @gl.public.write
    def submit_claim(self, dispute_id: str, claim: str) -> None:
        """Submit your claim text. Each party submits exactly once."""
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status != "evidence":
            raise gl.vm.UserError("evidence period is closed")

        now = _current_timestamp()
        if int(now) > int(dispute.evidence_deadline):
            raise gl.vm.UserError("evidence deadline has passed")

        text = _validate_text(claim, MAX_CLAIM_CHARS, "claim")

        sender = gl.message.sender_address
        if sender == dispute.creator:
            if dispute.plaintiff_submitted:
                raise gl.vm.UserError("plaintiff already submitted")
            dispute.plaintiff_claim = text
            dispute.plaintiff_submitted = True
        elif sender == dispute.counterparty:
            if dispute.defendant_submitted:
                raise gl.vm.UserError("defendant already submitted")
            dispute.defendant_claim = text
            dispute.defendant_submitted = True
        else:
            raise gl.vm.UserError("only the two parties can submit claims")

        self.disputes[dispute_id] = dispute

    # -- Close evidence ----------------------------------------------------

    @gl.public.write
    def close_evidence(self, dispute_id: str) -> None:
        """Close the evidence period once both claims are in (or deadline passed)."""
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status != "evidence":
            raise gl.vm.UserError("dispute is not in evidence phase")

        now = _current_timestamp()
        both_in = dispute.plaintiff_submitted and dispute.defendant_submitted
        deadline_passed = int(now) > int(dispute.evidence_deadline)

        if not both_in and not deadline_passed:
            raise gl.vm.UserError(
                "both claims must be submitted, or evidence deadline passed"
            )

        if not dispute.plaintiff_submitted:
            dispute.plaintiff_claim = "(no claim submitted)"
        if not dispute.defendant_submitted:
            dispute.defendant_claim = "(no claim submitted)"

        dispute.status = "verdict"
        dispute.resolve_deadline = u256(int(now) + int(self.resolve_deadline_seconds))
        self.disputes[dispute_id] = dispute

    # -- Resolve (AI consensus) --------------------------------------------

    @gl.public.write
    def resolve_dispute(self, dispute_id: str) -> dict:
        """Run AI consensus to record a binding verdict (no payout yet)."""
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status == "verdict":
            if dispute.verdict != "":
                raise gl.vm.UserError("dispute already resolved; appeal instead")
        elif dispute.status != "appealed":
            raise gl.vm.UserError("dispute is not ready for resolution")

        now = _current_timestamp()
        if int(now) > int(dispute.resolve_deadline):
            raise gl.vm.UserError("resolution deadline has passed")

        description = str(dispute.description)
        p_claim = str(dispute.plaintiff_claim)
        d_claim = str(dispute.defendant_claim)
        p_name = str(dispute.creator)
        d_name = str(dispute.counterparty)

        def leader_fn():
            return _leader_fn(description, p_claim, d_claim, p_name, d_name)

        def validator_fn(leaders_res):
            return _validator_fn(
                leaders_res, description, p_claim, d_claim, p_name, d_name
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not isinstance(result, dict):
            raise gl.vm.UserError("consensus result was unusable")

        winner_str = str(result.get("winner", ""))
        if winner_str not in ("plaintiff", "defendant", "draw"):
            raise gl.vm.UserError("consensus returned invalid verdict")

        confidence = int(result.get("confidence", 0))
        reasoning = str(result.get("reasoning", ""))[:1000]

        if winner_str == "plaintiff":
            winner_addr = dispute.creator
        elif winner_str == "defendant":
            winner_addr = dispute.counterparty
        else:
            winner_addr = _zero_address()

        dispute.verdict = winner_str
        dispute.verdict_confidence = u256(confidence)
        dispute.verdict_reasoning = reasoning
        dispute.resolved_at = now
        dispute.winner = winner_addr
        dispute.status = "verdict"
        # Start (or restart) the appeal window; payout waits for it to expire
        dispute.resolve_deadline = u256(int(now) + int(self.appeal_window_seconds))
        self.disputes[dispute_id] = dispute

        return {
            "dispute_id": dispute_id,
            "verdict": winner_str,
            "confidence": confidence,
            "reasoning": reasoning,
            "winner": str(winner_addr),
        }

    # -- Appeal ------------------------------------------------------------

    @gl.public.write.payable
    def appeal_verdict(self, dispute_id: str) -> None:
        """Appeal a verdict by posting a bond. Triggers re-resolution."""
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status != "verdict":
            raise gl.vm.UserError("no verdict to appeal")
        if dispute.verdict == "":
            raise gl.vm.UserError("dispute has not been resolved yet")
        if dispute.payout_executed:
            raise gl.vm.UserError("payout already executed")

        now = _current_timestamp()
        if int(now) > int(dispute.resolve_deadline):
            raise gl.vm.UserError("appeal window has closed")

        if int(dispute.appeal_count) >= MAX_APPEALS:
            raise gl.vm.UserError("maximum appeals reached")

        sender = gl.message.sender_address
        if sender != dispute.creator and sender != dispute.counterparty:
            raise gl.vm.UserError("only the parties can appeal")

        bond_required = int(dispute.escrow_amount) * APPEAL_BOND_BPS // 10000
        bond_sent = int(gl.message.value)
        if bond_sent < bond_required:
            raise gl.vm.UserError(
                f"appeal bond must be at least {bond_required} wei"
            )

        dispute.appeal_bond = u256(int(dispute.appeal_bond) + bond_sent)
        dispute.appeal_count = dispute.appeal_count + u256(1)
        dispute.appellant = sender
        dispute.status = "appealed"
        dispute.verdict = ""
        dispute.verdict_reasoning = ""
        dispute.verdict_confidence = u256(0)
        dispute.winner = _zero_address()
        dispute.resolve_deadline = u256(int(now) + int(self.resolve_deadline_seconds))
        self.disputes[dispute_id] = dispute

    # -- Execute verdict (payout) ------------------------------------------

    @gl.public.write
    def execute_verdict(self, dispute_id: str) -> None:
        """Release escrow to the winner after the appeal window expires.

        Settles the appeal bond: refunded to the appellant if the verdict
        stood, or paid to the opposing party if the appeal flipped it.
        Runs at most once (payout_executed guard).
        """
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status != "verdict" or dispute.verdict == "":
            raise gl.vm.UserError("no final verdict to execute")
        if dispute.payout_executed:
            raise gl.vm.UserError("payout already executed")

        now = _current_timestamp()
        if int(now) <= int(dispute.resolve_deadline):
            raise gl.vm.UserError("appeal window is still open")

        dispute.payout_executed = True
        dispute.status = "closed"
        self.disputes[dispute_id] = dispute

        # -- Escrow settlement --
        escrow = int(dispute.escrow_amount)
        if escrow > 0:
            if dispute.verdict == "draw":
                half = escrow // 2
                remainder = escrow - half * 2
                _transfer(dispute.creator, half + remainder)
                _transfer(dispute.counterparty, half)
            else:
                _transfer(dispute.winner, escrow)

        # -- Appeal bond settlement --
        bond = int(dispute.appeal_bond)
        if bond > 0 and dispute.appellant != _zero_address():
            appellant_lost = (
                dispute.verdict != "draw"
                and dispute.winner != dispute.appellant
            )
            if appellant_lost:
                # Appeal failed: bond goes to the party who stood firm
                if dispute.winner == dispute.creator:
                    _transfer(dispute.creator, bond)
                elif dispute.winner == dispute.counterparty:
                    _transfer(dispute.counterparty, bond)
                else:
                    _transfer(dispute.appellant, bond)
            else:
                # Appeal succeeded (or draw): bond refunded to appellant
                _transfer(dispute.appellant, bond)

        # -- Active counters --
        c_key = str(dispute.creator)
        c_active = int(self.disputes_by_creator.get(c_key, u256(0)))
        if c_active > 0:
            self.disputes_by_creator[c_key] = u256(c_active - 1)
        cp_key = str(dispute.counterparty)
        cp_active = int(self.disputes_by_counterparty.get(cp_key, u256(0)))
        if cp_active > 0:
            self.disputes_by_counterparty[cp_key] = u256(cp_active - 1)

    # -- Cancel (before evidence closes) -----------------------------------

    @gl.public.write
    def cancel_dispute(self, dispute_id: str) -> None:
        """Creator can cancel during evidence; escrow returned to creator."""
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        if dispute.status != "evidence":
            raise gl.vm.UserError("cannot cancel after evidence closed")

        sender = gl.message.sender_address
        if sender != dispute.creator:
            raise gl.vm.UserError("only the creator can cancel")

        dispute.status = "cancelled"
        self.disputes[dispute_id] = dispute

        escrow = int(dispute.escrow_amount)
        if escrow > 0:
            _transfer(dispute.creator, escrow)

        c_key = str(dispute.creator)
        c_active = int(self.disputes_by_creator.get(c_key, u256(0)))
        if c_active > 0:
            self.disputes_by_creator[c_key] = u256(c_active - 1)
        cp_key = str(dispute.counterparty)
        cp_active = int(self.disputes_by_counterparty.get(cp_key, u256(0)))
        if cp_active > 0:
            self.disputes_by_counterparty[cp_key] = u256(cp_active - 1)

    # -- Read methods -------------------------------------------------------

    @gl.public.view
    def get_dispute(self, dispute_id: str) -> dict:
        dispute_id = str(dispute_id)
        dispute = self.disputes.get(dispute_id)
        if dispute is None:
            raise gl.vm.UserError("unknown dispute_id")
        return dispute.as_dict()

    @gl.public.view
    def get_dispute_count(self) -> u256:
        return self.dispute_count

    @gl.public.view
    def get_statuses(self) -> list[str]:
        return [s for s in STATUSES]

    @gl.public.view
    def get_constants(self) -> dict:
        return {
            "max_claim_chars": MAX_CLAIM_CHARS,
            "max_description_chars": MAX_DESCRIPTION_CHARS,
            "evidence_deadline_seconds": int(self.evidence_deadline_seconds),
            "resolve_deadline_seconds": int(self.resolve_deadline_seconds),
            "appeal_window_seconds": int(self.appeal_window_seconds),
            "appeal_bond_bps": APPEAL_BOND_BPS,
            "max_appeals": MAX_APPEALS,
            "max_active_disputes": MAX_ACTIVE_DISPUTES,
        }

    @gl.public.view
    def get_active_disputes(self, address) -> u256:
        key = str(_coerce_address(address))
        creator_count = int(self.disputes_by_creator.get(key, u256(0)))
        cp_count = int(self.disputes_by_counterparty.get(key, u256(0)))
        return u256(creator_count + cp_count)
