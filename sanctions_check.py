"""
sanctions_check.py — Sanctions Screening Step (Pipeline Step 3)
Heritage National Bank | Compliance Engineering Team

Pipeline position: kyc_check (1) → aml_analysis (2) → sanctions_screening (3) → approval (4)

Implements deterministic stub screening against OFAC-SDN, UN-1267, and EU-CFSP lists,
with PCI DSS hardening, audit logging, and an approval recommendation helper.
"""

# =============================================================================
# CONFLICT NOTES
# =============================================================================
#
# CONFLICT 1 — match_list on screening failure
#   Design doc §3 ("Example Output: Screening Failure") sets match_list: null.
#   Data model declares match_list: list[SanctionsMatch] (never None), which is
#   the Python-typed field on SanctionsCheckResult.
#   DECISION: Use [] (empty list) in ALL cases, including failure.
#   RATIONALE: An empty list is type-safe and allows callers to iterate
#   match_list without a None-guard. The design doc's null is a JSON-schema
#   convenience; the data model's typed contract is authoritative for the
#   Python layer. No downstream consumer should distinguish null from [].
#
# CONFLICT 2 — metadata nesting in to_dict() output
#   Design doc §3 nests api_version and execution_time_ms under a "metadata"
#   object in the JSON output schema.
#   Data model exposes api_version and execution_time_ms as top-level fields
#   on SanctionsCheckResult.
#   DECISION: to_dict() produces the design doc's nested "metadata" structure.
#   RATIONALE: to_dict() is the serialisation boundary for external consumers
#   (approval step, audit log, API responses). The nested schema is the
#   canonical wire format specified in §3. Internal Python code uses the
#   top-level dataclass fields directly; the nesting is an output-only concern.
#
# CONFLICT 3 — match score field name
#   Design doc §3 and data model both use "match_score".
#   Reference implementation (lab_resources/sanctions_check_reference.py) uses
#   "score" on its SanctionsMatch dataclass.
#   DECISION: Use "match_score" (data model + design doc are authoritative).
#   RATIONALE: The reference is structural guidance only, not the schema source
#   of truth. The data model and design doc agree on "match_score"; the
#   reference diverges and must not be followed here.
#
# CONFLICT 4 — aml_context / transaction_id / customer_id boundary
#   These three fields appear in the data model as output fields on
#   SanctionsCheckResult (for pipeline continuity and PCI DSS audit tracing).
#   They are ABSENT from the design doc's output schema in §3 — they appear
#   only in the combined approval payload example in §4.2.
#   DECISION: Include all three on SanctionsCheckResult as per the data model,
#   but exclude aml_context, transaction_id, and customer_id from the top-level
#   to_dict() output (they are pipeline-internal fields, not wire-format fields).
#   to_dict() exposes only the §3 schema fields so the approval step receives
#   a clean, spec-compliant payload. Callers that need pipeline context
#   (e.g. the approval step) access SanctionsCheckResult attributes directly.
#   RATIONALE: The design doc's output schema is the external contract; the data
#   model's extra fields satisfy internal pipeline and audit requirements without
#   polluting the canonical JSON output.
#
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("compliance.sanctions_check")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_VERSION = "v2.1"
BASE_FUZZY_THRESHOLD = 0.85
HIGH_RISK_FUZZY_THRESHOLD = 0.75   # Applied when aml_result.risk_score >= 7.0
HIGH_RISK_AML_SCORE = 7.0
LISTS_CHECKED = ["OFAC-SDN", "UN-1267", "EU-CFSP"]

AUDIT_LOG_DIR = os.path.join(os.path.dirname(__file__), "audit_log")
AUDIT_LOG_PATH = os.path.join(AUDIT_LOG_DIR, "sanctions_screening.jsonl")

# ---------------------------------------------------------------------------
# Deterministic mock sanctions data
# Each tuple: (list_source, list_entry_id, country, program, match_score)
# ---------------------------------------------------------------------------

_MOCK_OFAC_SDN = {
    "acme international trading llc": ("OFAC-SDN", "OFAC-12847", "AE", "IRAN", 0.91)
}
_MOCK_UN_1267 = {
    "acme international trading llc": ("UN-1267",  "UN-8823",   "AE", "DPRK", 0.83)
}
_MOCK_EU_CFSP = {
    "silverline holdings ltd":        ("EU-CFSP",  "EU-4421",   "RU", "RUS",  0.88)
}


# ---------------------------------------------------------------------------
# Data model — inline definitions (source: sanctions_check_model.md)
# ---------------------------------------------------------------------------

@dataclass
class SanctionsMatch:
    """A single hit returned by a sanctions list lookup."""
    list_source: str          # e.g. "OFAC-SDN", "UN-1267", "EU-CFSP"
    matched_name: str         # Name from the sanctions list that triggered the match
    match_score: float        # Fuzzy-match confidence: 0.0-1.0 (reportable at >= 0.75)
    list_entry_id: str        # Unique identifier in the source list
    country: Optional[str] = None    # ISO 3166-1 alpha-2 jurisdiction of the listed entity
    program: Optional[str] = None    # Sanctions program name (e.g. "IRAN", "DPRK")

    def to_dict(self) -> dict[str, Any]:
        return {
            "list_source":    self.list_source,
            "matched_name":   self.matched_name,
            "match_score":    self.match_score,
            "list_entry_id":  self.list_entry_id,
            "country":        self.country,
            "program":        self.program,
        }


@dataclass
class AMLContext:
    """Subset of the AML step (step 2) result passed through for pipeline context."""
    risk_score: float   # 0-10 scale; score >= 7.0 lowers fuzzy-match threshold to 0.75
    status: str         # "clear" | "flagged" | "manual_review"


@dataclass
class SanctionsAuditEntry:
    """
    Immutable audit record written to audit_log/sanctions_screening.jsonl.
    No raw card data is stored here (PCI DSS Req 3).
    """
    timestamp: str              # ISO 8601 UTC, e.g. "2026-10-29T14:32:18Z"
    transaction_id: str         # Reference ID from initiate_transfer (e.g. "TXN-LAB-0001")
    customer_id: str            # From KYC step (step 1)
    counterparty_name: str      # Full legal name screened — no card/PAN data
    counterparty_country: str   # ISO 3166-1 alpha-2
    amount_inr: int             # Transaction amount in INR (numeric, not a card field)
    screened: bool              # Whether screening executed successfully
    match_count: int            # len(match_list); summary only — no raw match detail in log
    confidence: float           # Overall screening confidence at time of call
    lists_checked: list[str]    # Which sanctions lists were queried
    execution_time_ms: int      # Latency of the screening API call
    pipeline_step: int = 3      # Fixed: sanctions_screening occupies step 3

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the design doc §4.4 JSONL audit schema."""
        return {
            "timestamp":       self.timestamp,
            "transaction_id":  self.transaction_id,
            "customer_id":     self.customer_id,
            "input": {
                "counterparty_name":    self.counterparty_name,
                "counterparty_country": self.counterparty_country,
                "amount_inr":           self.amount_inr,
            },
            "output": {
                "screened":    self.screened,
                "matches":     self.match_count,
                "confidence":  self.confidence,
            },
            "lists_checked":    self.lists_checked,
            "execution_time_ms": self.execution_time_ms,
        }


@dataclass
class SanctionsCheckResult:
    """
    Canonical output of the sanctions_screening pipeline step (step 3).

    Pipeline position: kyc_check (1) -> aml_analysis (2) -> sanctions_screening (3) -> approval (4)

    Approval gate rules driven by this result:
      - screened=False                              -> BLOCK, route to manual review
      - any match_list entry with match_score>=0.75 -> BLOCK, compliance officer review
      - match_list empty and confidence>=0.90       -> PASS to approval
      - match_list empty and confidence<0.90        -> BLOCK, enhanced review
    """
    # --- Required core fields (must always be present) ---
    screened: bool                           # True = screening executed; False = API failure -> manual review
    match_list: list[SanctionsMatch]         # Empty list = no hits; never None (see CONFLICT 1)
    confidence: float                        # Overall screening confidence: 0.0-1.0

    # --- Pipeline integration fields ---
    screening_timestamp: str = ""            # ISO 8601 UTC timestamp when screening completed
    lists_checked: list[str] = field(default_factory=list)
    aml_context: Optional[AMLContext] = None # Passed through from step 2 for approval (step 4)

    # --- Audit / PCI DSS fields ---
    transaction_id: str = ""   # Ties result to initiate_transfer reference_id for audit log
    customer_id: str = ""      # Ties result to KYC-verified identity (step 1)
    audit_logged: bool = False # Set True after record is written to audit_log/

    # --- Operational metadata (no sensitive card data — PCI DSS Req 3) ---
    api_version: str = API_VERSION
    execution_time_ms: int = 0
    error_code: Optional[str] = None  # Populated only when screened=False

    def to_dict(self) -> dict[str, Any]:
        """
        Produce the canonical wire-format JSON from design doc §3.

        api_version and execution_time_ms are nested under "metadata" per §3
        (see CONFLICT 2). aml_context, transaction_id, and customer_id are
        pipeline-internal fields and are excluded from the wire output (see CONFLICT 4).
        """
        metadata: dict[str, Any] = {
            "api_version":        self.api_version,
            "execution_time_ms":  self.execution_time_ms,
        }
        if self.error_code:
            metadata["error"] = self.error_code

        return {
            "screened":            self.screened,
            "match_list":          [m.to_dict() for m in self.match_list],
            "confidence":          self.confidence,
            "screening_timestamp": self.screening_timestamp,
            "lists_checked":       self.lists_checked,
            "metadata":            metadata,
        }


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class InvalidScreeningInputError(ValueError):
    """Raised when the input payload fails PCI DSS validation."""


# ---------------------------------------------------------------------------
# PCI DSS helpers
# ---------------------------------------------------------------------------

def _mask_pii(v: str) -> str:
    """
    PCI DSS hardening: never write plaintext identifiers to logs.
    Returns first 2 chars + '***' + last 2 chars, or '***' if len <= 4.
    """
    if not v or len(v) <= 4:
        return "***"
    return v[:2] + "***" + v[-2:]


def _store_result_encrypted(customer_id: str, result: SanctionsCheckResult) -> None:
    """
    Documented no-op placeholder for KMS-backed encrypted result storage.

    PRODUCTION REQUIREMENT: Before writing to the compliance store, this
    function must encrypt 'result' using a KMS-backed envelope encryption
    scheme (e.g. AWS KMS / GCP Cloud KMS / Azure Key Vault). The data key
    must be wrapped with a CMK scoped to the compliance-store IAM role.
    Storing the result in plaintext would violate PCI DSS Requirement 3
    (protect stored cardholder/sensitive data).

    For the lab environment this is intentionally a no-op; the call site
    is preserved so the production implementation slot is unambiguous.
    """
    logger.debug(
        "Encrypted result storage invoked (no-op in lab) for customer=%s",
        _mask_pii(customer_id),
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _validate_input(
    customer_id: str,
    counterparty_details: dict,
    transaction_amount: int,
) -> None:
    """
    Raise InvalidScreeningInputError for any malformed input.
    PCI DSS hardening: validate before any data touches logs or storage.
    """
    if not customer_id or not isinstance(customer_id, str) or not customer_id.strip():
        raise InvalidScreeningInputError("customer_id must be a non-empty string")

    if not isinstance(counterparty_details, dict):
        raise InvalidScreeningInputError(
            "counterparty_details must be a dict"
        )
    if not counterparty_details.get("name") or not str(counterparty_details["name"]).strip():
        raise InvalidScreeningInputError(
            "counterparty_details must contain a non-empty 'name' field"
        )
    if not counterparty_details.get("country") or not str(counterparty_details["country"]).strip():
        raise InvalidScreeningInputError(
            "counterparty_details must contain a non-empty 'country' field"
        )

    if not isinstance(transaction_amount, (int, float)) or transaction_amount < 0:
        raise InvalidScreeningInputError(
            "transaction_amount must be a non-negative number"
        )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _write_audit_log(entry: SanctionsAuditEntry) -> None:
    """
    Append a JSONL record to audit_log/sanctions_screening.jsonl.
    Creates the directory and file if absent.
    No card/PAN data is written (PCI DSS Req 3).
    """
    os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
    record = json.dumps(entry.to_dict(), ensure_ascii=False)
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(record + "\n")


# ---------------------------------------------------------------------------
# Sanctions list lookup (deterministic stub)
# ---------------------------------------------------------------------------

def _query_sanctions_lists(
    counterparty_name: str,
    fuzzy_threshold: float,
) -> list[SanctionsMatch]:
    """
    Deterministic stub standing in for a live OFAC/UN/EU sanctions API call.
    Returns SanctionsMatch objects for any entry whose match_score >= fuzzy_threshold.
    """
    name_key = counterparty_name.strip().lower()
    matches: list[SanctionsMatch] = []

    all_lists: list[dict] = [_MOCK_OFAC_SDN, _MOCK_UN_1267, _MOCK_EU_CFSP]
    for mock_list in all_lists:
        if name_key in mock_list:
            list_source, list_entry_id, country, program, match_score = mock_list[name_key]
            if match_score >= fuzzy_threshold:
                matches.append(SanctionsMatch(
                    list_source=list_source,
                    matched_name=counterparty_name,
                    match_score=match_score,
                    list_entry_id=list_entry_id,
                    country=country,
                    program=program,
                ))

    return matches


# ---------------------------------------------------------------------------
# Approval recommendation helper
# ---------------------------------------------------------------------------

def get_approval_recommendation(result: dict, aml_status: str = "clear") -> str:
    """
    Implements the approval gate table from design doc §4.2 exactly.

    Returns one of:
      "APPROVE"                    — clean screening, high confidence
      "BLOCK_MANUAL_REVIEW"        — screening failed, low confidence, or AML manual_review
      "BLOCK_COMPLIANCE_OFFICER"   — sanctions hit with match_score >= 0.75
    """
    screened = result.get("screened", False)
    match_list = result.get("match_list", [])
    confidence = result.get("confidence", 0.0)

    # Rule 1: screening did not complete — cannot certify compliance
    if not screened:
        return "BLOCK_MANUAL_REVIEW"

    # Rule 2: any match with score >= 0.75 — potential sanctions violation
    for match in match_list:
        score = match.get("match_score", 0.0)
        if score >= 0.75:
            return "BLOCK_COMPLIANCE_OFFICER"

    # Rule 3: AML step flagged for manual review — additive check
    if aml_status == "manual_review":
        return "BLOCK_MANUAL_REVIEW"

    # Rule 4: clean match_list but insufficient confidence
    if confidence < 0.90:
        return "BLOCK_MANUAL_REVIEW"

    # Rule 5: clean match_list AND confidence >= 0.90 — pass to approval
    return "APPROVE"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def screen_transaction(
    customer_id: str,
    counterparty_details: dict,     # must contain "name", "country"; optional "account", "entity_type"
    transaction_amount: int,
    aml_result: dict | None = None,
) -> dict:
    """
    Screen a transaction against OFAC-SDN, UN-1267, and EU-CFSP sanctions lists.

    Args:
        customer_id:          KYC-verified customer identifier (from pipeline step 1).
        counterparty_details: Dict with at least 'name' (str) and 'country' (ISO 3166-1 alpha-2).
                              Optional keys: 'account', 'entity_type'.
        transaction_amount:   Amount in INR (non-negative integer).
        aml_result:           AML step output dict with 'risk_score' (float) and 'status' (str).
                              None is treated as {"risk_score": 0.0, "status": "clear"}.

    Returns:
        SanctionsCheckResult.to_dict() — canonical wire-format dict per design doc §3.
        On any error: returns a screened=False result; NEVER silently passes.
    """
    start_time = time.monotonic()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Resolve AML context — default to safe/clear if not provided
    aml_dict = aml_result or {}
    aml_risk_score: float = float(aml_dict.get("risk_score", 0.0))
    aml_status: str = str(aml_dict.get("status", "clear"))
    aml_ctx = AMLContext(risk_score=aml_risk_score, status=aml_status)

    # Helper to build a failure result consistently
    def _failure_result(error_code: str) -> dict:
        elapsed = int((time.monotonic() - start_time) * 1000)
        return SanctionsCheckResult(
            screened=False,
            match_list=[],
            confidence=0.0,
            screening_timestamp=now_utc,
            lists_checked=[],
            aml_context=aml_ctx,
            customer_id=customer_id if isinstance(customer_id, str) else "",
            api_version=API_VERSION,
            execution_time_ms=elapsed,
            error_code=error_code,
        ).to_dict()

    # --- Input validation (PCI DSS hardening) ---
    try:
        _validate_input(customer_id, counterparty_details, transaction_amount)
    except InvalidScreeningInputError as exc:
        logger.warning(
            "Sanctions screening rejected malformed input for customer=%s: %s",
            _mask_pii(str(customer_id)),
            exc,
        )
        return _failure_result("invalid_input")

    counterparty_name: str = counterparty_details["name"]
    counterparty_country: str = counterparty_details["country"]

    # Apply AML-driven fuzzy threshold
    fuzzy_threshold = (
        HIGH_RISK_FUZZY_THRESHOLD
        if aml_risk_score >= HIGH_RISK_AML_SCORE
        else BASE_FUZZY_THRESHOLD
    )

    logger.info(
        "Sanctions screening started for customer=%s counterparty=%s threshold=%.2f",
        _mask_pii(customer_id),
        _mask_pii(counterparty_name),
        fuzzy_threshold,
    )

    # --- Sanctions list query ---
    try:
        matches = _query_sanctions_lists(counterparty_name, fuzzy_threshold)
    except Exception as exc:
        logger.error(
            "Sanctions API error for customer=%s counterparty=%s: %s",
            _mask_pii(customer_id),
            _mask_pii(counterparty_name),
            exc,
        )
        return _failure_result("sanctions_api_error")

    # --- Compute overall confidence ---
    if matches:
        confidence = max(m.match_score for m in matches)
    else:
        # No matches found — high confidence in a clean result
        confidence = 0.98

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # --- Build canonical result ---
    result = SanctionsCheckResult(
        screened=True,
        match_list=matches,
        confidence=confidence,
        screening_timestamp=now_utc,
        lists_checked=list(LISTS_CHECKED),
        aml_context=aml_ctx,
        customer_id=customer_id,
        api_version=API_VERSION,
        execution_time_ms=elapsed_ms,
        error_code=None,
    )

    # --- Encrypted result storage (PCI DSS — production uses KMS) ---
    _store_result_encrypted(customer_id, result)

    # --- Audit logging (design doc §4.4) ---
    audit_entry = SanctionsAuditEntry(
        timestamp=now_utc,
        transaction_id=result.transaction_id,
        customer_id=customer_id,
        counterparty_name=counterparty_name,
        counterparty_country=counterparty_country,
        amount_inr=int(transaction_amount),
        screened=True,
        match_count=len(matches),
        confidence=confidence,
        lists_checked=list(LISTS_CHECKED),
        execution_time_ms=elapsed_ms,
    )
    try:
        _write_audit_log(audit_entry)
        result.audit_logged = True
    except OSError as exc:
        logger.error(
            "Failed to write audit log for customer=%s: %s",
            _mask_pii(customer_id),
            exc,
        )

    logger.info(
        "Sanctions screening completed in %dms for customer=%s matches=%d confidence=%.2f",
        elapsed_ms,
        _mask_pii(customer_id),
        len(matches),
        confidence,
    )

    return result.to_dict()
