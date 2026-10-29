# Recommended Python Data Model: Sanctions Check Result

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SanctionsMatch:
    """A single hit returned by a sanctions list lookup."""
    list_source: str          # e.g. "OFAC-SDN", "UN-1267", "EU-CFSP"
    matched_name: str         # Name from the sanctions list that triggered the match
    match_score: float        # Fuzzy-match confidence: 0.0–1.0 (reportable at ≥ 0.75)
    list_entry_id: str        # Unique identifier in the source list
    country: Optional[str] = None    # ISO 3166-1 alpha-2 jurisdiction of the listed entity
    program: Optional[str] = None    # Sanctions program name (e.g. "IRAN", "DPRK")


@dataclass
class AMLContext:
    """Subset of the AML step (step 2) result passed through for pipeline context."""
    risk_score: float   # 0–10 scale; score ≥ 7.0 lowers fuzzy-match threshold to 0.75
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


@dataclass
class SanctionsCheckResult:
    """
    Canonical output of the sanctions_screening pipeline step (step 3).

    Pipeline position: kyc_check (1) → aml_analysis (2) → sanctions_screening (3) → approval (4)

    Approval gate rules driven by this result:
      - screened=False                              → BLOCK, route to manual review
      - any match_list entry with match_score≥0.75  → BLOCK, compliance officer review
      - match_list empty and confidence≥0.90        → PASS to approval
      - match_list empty and confidence<0.90        → BLOCK, enhanced review
    """
    # --- Required core fields (must always be present) ---
    screened: bool              # True = screening executed; False = API failure → manual review
    match_list: list[SanctionsMatch]  # Empty list = no hits; never None on success
    confidence: float           # Overall screening confidence: 0.0–1.0

    # --- Pipeline integration fields ---
    screening_timestamp: str    # ISO 8601 UTC timestamp when screening completed
    lists_checked: list[str]    # Sanctions lists queried, e.g. ["OFAC-SDN","UN-1267","EU-CFSP"]
    aml_context: Optional[AMLContext] = None  # Passed through from step 2 for approval (step 4)

    # --- Audit / PCI DSS fields ---
    transaction_id: str = ""    # Ties result to initiate_transfer reference_id for audit log
    customer_id: str = ""       # Ties result to KYC-verified identity (step 1)
    audit_logged: bool = False  # Set True after record is written to audit_log/

    # --- Operational metadata (no sensitive card data — PCI DSS Req 3) ---
    api_version: str = "v2.1"
    execution_time_ms: int = 0
    error_code: Optional[str] = None  # Populated only when screened=False (e.g. "sanctions_api_timeout")
```

The required `{screened, match_list, confidence}` triplet forms the immutable core of `SanctionsCheckResult`; every other field extends it to satisfy the three integration constraints identified in the server and design document. First, pipeline continuity: `aml_context` carries the AML step-2 result (risk score and status) forward so the approval step (step 4) can apply additive compliance logic without re-querying upstream services, and `pipeline_step=3` in the audit entry fixes the canonical position between `aml_analysis` and `approval` as defined in `get_compliance_pipeline()`. Second, PCI DSS Requirement 3 compliance: `SanctionsAuditEntry` records only non-card fields (counterparty name, country, INR amount) and a match count rather than raw match detail, ensuring no primary account numbers or sensitive authentication data reach the audit log; the existing `_mask_pci` pattern in `server.py` provides a complementary transport-layer safeguard. Third, failure-safe operation: `screened=False` combined with a non-null `error_code` gives the approval step an unambiguous hard-stop signal on API timeout or unavailability, consistent with the design document's rule that a transaction must never silently pass when screening cannot be certified complete.
