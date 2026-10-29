# Sanctions Screening — Design Document

**Author:** Heritage National Bank Compliance Team  
**Date:** 2026-10-29  
**Status:** Draft  
**Version:** 1.0

---

## 1. Pipeline Position

### Current Pipeline Architecture

```
kyc_check → aml_analysis → [NEW: sanctions_screening] → approval
(step 1)      (step 2)            (step 3)                (step 4)
```

### Insertion Rationale

**Position:** Between `aml_analysis` (step 2) and `approval` (step 4) as step 3.

**Justification:**
- **After KYC:** Sanctions screening requires verified customer identity from step 1
- **After AML:** AML provides behavioral risk context (transaction patterns, velocity) that informs sanctions screening thresholds
- **Before Approval:** Sanctions hits are regulatory hard stops (OFAC violations carry strict liability) and must block approval before transaction settlement
- **Separation of Concerns:** AML detects suspicious patterns; sanctions checks list membership. Distinct regulatory requirements demand distinct pipeline steps.

**Regulatory Compliance:** Aligns with PCI DSS audit logging requirements and FinCEN guidance on sequential screening.

---

## 2. Data Inputs Required

### Input Schema

| Field | Type | Source | Required | Notes |
|-------|------|--------|----------|-------|
| `customer_id` | string | KYC step (step 1) | Yes | Primary account holder identifier |
| `counterparty_details` | object | Transaction payload | Yes | Receiving party information |
| `counterparty_details.name` | string | Transaction payload | Yes | Full legal name for fuzzy matching |
| `counterparty_details.country` | string (ISO 3166-1 alpha-2) | Transaction payload | Yes | Jurisdiction for geographic sanctions (e.g., "IR", "KP") |
| `counterparty_details.account` | string | Transaction payload | No | IBAN/account for enhanced screening |
| `counterparty_details.entity_type` | string | Transaction payload | No | "individual" or "organization" |
| `transaction_amount` | integer | Transaction payload | Yes | Amount in INR; used for risk-weighting, not match logic |
| `aml_result` | object | AML step (step 2) | Yes | Passed through for context |
| `aml_result.risk_score` | float | AML step (step 2) | Yes | 0-10 scale; scores ≥7 trigger enhanced sanctions screening |
| `aml_result.status` | string | AML step (step 2) | Yes | "clear", "flagged", "manual_review" |

### Example Input Payload

```json
{
  "customer_id": "CUST-887234",
  "counterparty_details": {
    "name": "Acme International Trading LLC",
    "country": "AE",
    "account": "AE070331234567890123456",
    "entity_type": "organization"
  },
  "transaction_amount": 250000,
  "aml_result": {
    "risk_score": 3.2,
    "status": "clear"
  }
}
```

---

## 3. Output Schema

### Schema Definition

```json
{
  "screened": true,
  "match_list": [],
  "confidence": 0.95,
  "screening_timestamp": "2026-10-29T14:32:18Z",
  "lists_checked": ["OFAC-SDN", "UN-1267", "EU-CFSP"],
  "metadata": {
    "api_version": "v2.1",
    "execution_time_ms": 87
  }
}
```

### Field Descriptions

| Field | Type | Range/Values | Description |
|-------|------|--------------|-------------|
| `screened` | bool | true/false | Whether screening executed successfully. `false` triggers manual review. |
| `match_list` | array | 0-N matches | OFAC/UN/EU sanctions matches. Empty array = no hits. |
| `match_list[].list_source` | string | "OFAC-SDN", "UN-1267", etc. | Sanctions list identifier |
| `match_list[].matched_name` | string | - | Name from sanctions list that triggered the match |
| `match_list[].match_score` | float | 0.0-1.0 | Fuzzy match confidence (0.75+ is reportable) |
| `match_list[].list_entry_id` | string | - | Unique identifier in source list |
| `confidence` | float | 0.0-1.0 | Overall screening confidence. <0.90 triggers enhanced review. |
| `screening_timestamp` | string (ISO 8601) | - | UTC timestamp when screening completed |
| `lists_checked` | array | - | Which sanctions lists were queried |
| `metadata` | object | - | Debug/audit information |

### Example Output: Clean Transaction

```json
{
  "screened": true,
  "match_list": [],
  "confidence": 0.98,
  "screening_timestamp": "2026-10-29T14:32:18Z",
  "lists_checked": ["OFAC-SDN", "UN-1267", "EU-CFSP"],
  "metadata": {
    "api_version": "v2.1",
    "execution_time_ms": 87
  }
}
```

### Example Output: Sanctions Hit

```json
{
  "screened": true,
  "match_list": [
    {
      "list_source": "OFAC-SDN",
      "matched_name": "ACME INTERNATIONAL TRADING L.L.C.",
      "match_score": 0.91,
      "list_entry_id": "OFAC-12847",
      "country": "AE",
      "program": "IRAN"
    }
  ],
  "confidence": 0.91,
  "screening_timestamp": "2026-10-29T14:32:18Z",
  "lists_checked": ["OFAC-SDN", "UN-1267", "EU-CFSP"],
  "metadata": {
    "api_version": "v2.1",
    "execution_time_ms": 134
  }
}
```

### Example Output: Screening Failure

```json
{
  "screened": false,
  "match_list": null,
  "confidence": 0.0,
  "screening_timestamp": "2026-10-29T14:32:18Z",
  "lists_checked": [],
  "metadata": {
    "error": "sanctions_api_timeout",
    "api_version": "v2.1",
    "execution_time_ms": 5000
  }
}
```

---

## 4. Integration Contract with AML Module

### Data Flow

```
AML Module (step 2) → Sanctions Screening (step 3) → Approval (step 4)
       ↓                        ↓                           ↓
   aml_result        sanctions_result + aml_result    final_decision
```

### Contract Rules

#### 4.1 Input from AML Module

- **Passthrough:** The `aml_result` object is passed unchanged to the approval step
- **Independence:** Sanctions screening logic does NOT depend on AML verdict
  - Even if `aml_result.status = "clear"`, sanctions screening still executes fully
  - AML "flagged" status does NOT automatically fail sanctions (they are independent checks)
- **Context Enrichment:** `aml_result.risk_score ≥ 7.0` lowers the fuzzy match threshold from 0.85 to 0.75 (stricter matching for high-risk transactions)

#### 4.2 Output to Approval Step

**Approval Logic:**

| Condition | Action | Reasoning |
|-----------|--------|-----------|
| `screened = false` | **BLOCK** → manual review queue | Cannot certify compliance without screening |
| `match_list` not empty AND any `match_score ≥ 0.75` | **BLOCK** → compliance officer review | Potential sanctions violation (strict liability) |
| `match_list` empty AND `confidence ≥ 0.90` | **PASS** → continue to approval | Clean screening result |
| `match_list` empty AND `confidence < 0.90` | **BLOCK** → enhanced review | Low confidence requires manual verification |
| `aml_result.status = "manual_review"` | **BLOCK** → compliance review (even if sanctions clean) | AML and sanctions are additive checks |

**Combined Payload to Approval:**

```json
{
  "transaction_id": "TXN-LAB-0042",
  "kyc_result": { "verified": true, "customer_id": "CUST-887234" },
  "aml_result": { "risk_score": 3.2, "status": "clear" },
  "sanctions_result": {
    "screened": true,
    "match_list": [],
    "confidence": 0.98
  },
  "recommendation": "APPROVE"
}
```

#### 4.3 Failure Modes

**Sanctions API Timeout/Unavailable:**
- Set `screened = false`
- Route to manual review queue
- **NEVER** silently pass the transaction
- Log to `audit_log/sanctions_failures.jsonl`

**AML Module Failure:**
- Sanctions screening STILL executes (parallel compliance requirements)
- Overall transaction fails if either AML or sanctions cannot complete

### 4.4 Audit Logging

All sanctions screening calls must log to `audit_log/sanctions_screening.jsonl`:

```json
{
  "timestamp": "2026-10-29T14:32:18Z",
  "transaction_id": "TXN-LAB-0042",
  "customer_id": "CUST-887234",
  "input": {
    "counterparty_name": "Acme International Trading LLC",
    "counterparty_country": "AE",
    "amount_inr": 250000
  },
  "output": {
    "screened": true,
    "matches": 0,
    "confidence": 0.98
  },
  "lists_checked": ["OFAC-SDN", "UN-1267", "EU-CFSP"],
  "execution_time_ms": 87
}
```

**PCI DSS Compliance:** No card numbers or sensitive authentication data in logs.

---

## 5. Open Questions / Risks

### 5.1 False Positive Rate

**Question:** What is the acceptable false positive rate for fuzzy name matching?

**Current Assumption:** 0.75 match score threshold balances recall (catching true hits) vs. precision (minimizing false alarms).

**Risk:** Too strict (e.g., 0.90) may miss obfuscated sanctioned entities; too loose (e.g., 0.60) generates excessive manual review load.

**Mitigation:** Pilot with 30-day transaction dataset; measure ratio of false positives to true positives. Target <5% false positive rate at 0.75 threshold.

### 5.2 Name-Matching Fuzziness

**Question:** How should the system handle:
- Transliteration variants (Cyrillic, Arabic scripts)?
- Abbreviations (LLC vs. L.L.C. vs. Limited Liability Company)?
- Name order variations (Eastern vs. Western conventions)?

**Current Approach:** Vendor-supplied fuzzy matching algorithm (Levenshtein distance + phonetic matching).

**Risk:** Proprietary black-box matching may not be auditable for regulatory compliance.

**Mitigation:** Require vendor to document matching algorithm; implement deterministic regression tests with OFAC sample dataset.

### 5.3 Sanctions List Refresh Cadence

**Question:** How frequently are OFAC/UN/EU lists updated in the screening system?

**Current Assumption:** Daily overnight batch update.

**Risk:** OFAC can publish emergency updates intraday (e.g., during geopolitical crises). A transaction could clear screening using stale lists.

**Mitigation:**
- Subscribe to OFAC RSS feed for emergency updates
- Implement "list staleness" check: if last update >24 hours ago, flag for manual review
- Consider real-time API calls to OFAC Sanctions Search API (requires > 500ms latency budget)

### 5.4 Geographic Sanctions vs. Entity Sanctions

**Question:** Should country-level sanctions (e.g., Iran, North Korea) trigger automatic blocks, or require entity-level matching?

**Current Scope:** Entity-level matching only (check `counterparty_details.name` against lists).

**Risk:** Sanctioned countries have legitimate humanitarian exemptions; blanket blocking by `country` field may violate trade rules.

**Mitigation:** Implement separate "high-risk jurisdiction" flag that escalates to enhanced review, not automatic block.

### 5.5 Performance SLA

**Question:** What is the acceptable latency for sanctions screening in the transaction pipeline?

**Current Target:** <200ms p95 latency.

**Risk:** External sanctions API calls add network latency; timeouts may cascade to transaction failures.

**Mitigation:**
- Cache negative results (no match) for 24 hours keyed by `(name, country)` tuple
- Implement circuit breaker: if API p95 latency >1s for 60s, route all transactions to manual review and alert on-call
- Consider local sanctions list database snapshot for sub-50ms latency

### 5.6 Sanctions Screening API Vendor Lock-In

**Question:** Is the sanctions screening contract vendor-agnostic?

**Current Design:** Assumes single API vendor with proprietary output schema.

**Risk:** Switching vendors (e.g., from Dow Jones Risk & Compliance to Refinitiv World-Check) requires rewriting integration logic.

**Mitigation:** Design internal `sanctions_result` schema to abstract vendor-specific responses. Map vendor outputs to canonical format in adapter layer.

---

## 6. Implementation Phases

### Phase 1: Core Integration (Week 1-2)
- Implement sanctions screening step in compliance pipeline
- Wire up inputs from KYC/AML steps
- Map output to approval logic
- Deploy audit logging

### Phase 2: Fuzzy Matching Tuning (Week 3)
- Run pilot on historical transaction dataset
- Calibrate match score threshold (current: 0.75)
- Measure false positive rate
- Document matching algorithm for audit

### Phase 3: Failure Mode Hardening (Week 4)
- Implement circuit breaker for API timeouts
- Add list staleness detection
- Test manual review queue routing
- Load test at 1000 TPS

### Phase 4: Production Rollout (Week 5)
- Shadow mode: run sanctions screening parallel to production (no blocking)
- Compare shadow results to manual compliance reviews
- Cut over to blocking mode with 24hr monitoring

---

## 7. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| False positive rate | <5% | Manual review outcomes / total flags |
| Screening latency (p95) | <200ms | APM metrics in production |
| API availability | >99.9% | Vendor SLA + circuit breaker logs |
| Manual review queue depth | <50 pending | Real-time dashboard |
| Audit log completeness | 100% | Daily log reconciliation job |

---

## 8. References

- [OFAC Sanctions Programs](https://ofac.treasury.gov/sanctions-programs-and-country-information)
- [FinCEN Advisory on Sanctions Screening](https://www.fincen.gov/resources/advisories)
- Heritage National Bank PCI DSS Compliance Policy (internal)
- `CLAUDE.md` — MCP tool registry and model routing

---

**Document Owner:** Compliance Engineering Team  
**Review Cadence:** Quarterly or on regulatory guidance updates  
**Next Review:** 2027-01-29
