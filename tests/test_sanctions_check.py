"""
test_sanctions_check.py — Comprehensive Test Suite for Sanctions Screening
Heritage National Bank | Compliance Engineering Team

Test coverage:
1. Clean transaction pass (no sanctions matches)
2. OFAC match detection (score >= threshold)
3. UN list match detection (score >= threshold)
4. Malformed input handling (missing fields, invalid types)
5. Performance assertion (response < 200ms with mocks)

All external sanctions API calls are mocked for deterministic test results.
"""

from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest

# Import the module under test
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sanctions_check import (
    screen_transaction,
    InvalidScreeningInputError,
    SanctionsCheckResult,
    SanctionsMatch,
    _query_sanctions_lists,
    _validate_input,
    BASE_FUZZY_THRESHOLD,
    HIGH_RISK_FUZZY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_transaction():
    """Fixture for a clean transaction with no sanctions matches."""
    return {
        "customer_id": "CUST-12345",
        "counterparty_details": {
            "name": "Legitimate Business Corp",
            "country": "US"
        },
        "transaction_amount": 50000,
        "aml_result": {
            "risk_score": 3.5,
            "status": "clear"
        }
    }


@pytest.fixture
def ofac_match_transaction():
    """Fixture for a transaction that matches OFAC list."""
    return {
        "customer_id": "CUST-67890",
        "counterparty_details": {
            "name": "ACME International Trading LLC",
            "country": "AE"
        },
        "transaction_amount": 100000,
        "aml_result": {
            "risk_score": 4.0,
            "status": "clear"
        }
    }


@pytest.fixture
def un_match_transaction():
    """Fixture for a transaction that matches UN list."""
    return {
        "customer_id": "CUST-11111",
        "counterparty_details": {
            "name": "Acme International Trading LLC",
            "country": "AE"
        },
        "transaction_amount": 75000,
        "aml_result": {
            "risk_score": 8.5,  # High risk - lowers threshold to 0.75
            "status": "flagged"
        }
    }


@pytest.fixture
def eu_match_transaction():
    """Fixture for a transaction that matches EU-CFSP list."""
    return {
        "customer_id": "CUST-22222",
        "counterparty_details": {
            "name": "Silverline Holdings Ltd",
            "country": "RU"
        },
        "transaction_amount": 200000,
        "aml_result": {
            "risk_score": 5.0,
            "status": "clear"
        }
    }


@pytest.fixture
def mock_audit_log(tmp_path):
    """Mock the audit log directory to avoid filesystem side effects."""
    audit_dir = tmp_path / "audit_log"
    audit_dir.mkdir()
    with mock.patch("sanctions_check.AUDIT_LOG_DIR", str(audit_dir)):
        with mock.patch("sanctions_check.AUDIT_LOG_PATH", str(audit_dir / "sanctions_screening.jsonl")):
            yield audit_dir


# ---------------------------------------------------------------------------
# Test 1: Clean Transaction Pass (No Sanctions Matches)
# ---------------------------------------------------------------------------

def test_clean_transaction_pass(clean_transaction, mock_audit_log):
    """
    Test that a clean transaction with no sanctions matches returns:
    - screened=True
    - match_list=[]
    - high confidence (0.98 for no matches)
    """
    result = screen_transaction(**clean_transaction)

    assert result["screened"] is True, "Clean transaction should complete screening successfully"
    assert result["match_list"] == [], "Clean transaction should have no matches"
    assert result["confidence"] == 0.98, "Clean transaction should have high confidence (0.98)"
    assert result["screening_timestamp"], "Result should include timestamp"
    assert result["lists_checked"] == ["OFAC-SDN", "UN-1267", "EU-CFSP"], "All lists should be checked"
    assert "metadata" in result, "Result should include metadata"
    assert result["metadata"]["api_version"] == "v2.1", "API version should be v2.1"
    assert result["metadata"]["execution_time_ms"] >= 0, "Execution time should be non-negative"


# ---------------------------------------------------------------------------
# Test 2: OFAC Match Detection
# ---------------------------------------------------------------------------

def test_ofac_match_detection(ofac_match_transaction, mock_audit_log):
    """
    Test that a transaction matching OFAC list is correctly flagged.
    Expected: match with score 0.91 from OFAC-SDN list.
    """
    result = screen_transaction(**ofac_match_transaction)

    assert result["screened"] is True, "OFAC match transaction should complete screening"
    assert len(result["match_list"]) >= 1, "Should detect at least one OFAC match"

    # Find the OFAC match
    ofac_match = next((m for m in result["match_list"] if m["list_source"] == "OFAC-SDN"), None)
    assert ofac_match is not None, "Should contain OFAC-SDN match"
    assert ofac_match["match_score"] == 0.91, "OFAC match score should be 0.91"
    assert ofac_match["list_entry_id"] == "OFAC-12847", "Should include OFAC list entry ID"
    assert ofac_match["country"] == "AE", "Should include country code"
    assert ofac_match["program"] == "IRAN", "Should include sanctions program"

    # Confidence should be the highest match score
    assert result["confidence"] >= 0.91, "Confidence should be at least the highest match score"


# ---------------------------------------------------------------------------
# Test 3: UN List Match Detection
# ---------------------------------------------------------------------------

def test_un_match_detection(un_match_transaction, mock_audit_log):
    """
    Test that a transaction matching UN list is correctly flagged.
    With high AML risk score (8.5), threshold drops to 0.75, so UN match at 0.83 should trigger.
    """
    result = screen_transaction(**un_match_transaction)

    assert result["screened"] is True, "UN match transaction should complete screening"
    assert len(result["match_list"]) >= 1, "Should detect at least one match (UN and possibly OFAC)"

    # Find the UN match
    un_match = next((m for m in result["match_list"] if m["list_source"] == "UN-1267"), None)
    assert un_match is not None, "Should contain UN-1267 match due to lowered threshold"
    assert un_match["match_score"] == 0.83, "UN match score should be 0.83"
    assert un_match["list_entry_id"] == "UN-8823", "Should include UN list entry ID"
    assert un_match["country"] == "AE", "Should include country code"
    assert un_match["program"] == "DPRK", "Should include sanctions program"


# ---------------------------------------------------------------------------
# Test 4: EU-CFSP Match Detection
# ---------------------------------------------------------------------------

def test_eu_match_detection(eu_match_transaction, mock_audit_log):
    """
    Test that a transaction matching EU-CFSP list is correctly flagged.
    """
    result = screen_transaction(**eu_match_transaction)

    assert result["screened"] is True, "EU match transaction should complete screening"
    assert len(result["match_list"]) >= 1, "Should detect EU-CFSP match"

    # Find the EU match
    eu_match = next((m for m in result["match_list"] if m["list_source"] == "EU-CFSP"), None)
    assert eu_match is not None, "Should contain EU-CFSP match"
    assert eu_match["match_score"] == 0.88, "EU match score should be 0.88"
    assert eu_match["list_entry_id"] == "EU-4421", "Should include EU list entry ID"
    assert eu_match["country"] == "RU", "Should include country code"
    assert eu_match["program"] == "RUS", "Should include sanctions program"


# ---------------------------------------------------------------------------
# Test 5: Malformed Input Handling
# ---------------------------------------------------------------------------

def test_missing_customer_id(mock_audit_log):
    """Test that missing customer_id is rejected."""
    result = screen_transaction(
        customer_id="",
        counterparty_details={"name": "Test Corp", "country": "US"},
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject empty customer_id"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"
    assert result["confidence"] == 0.0, "Failed screening should have zero confidence"


def test_invalid_customer_id_type(mock_audit_log):
    """Test that non-string customer_id is rejected."""
    result = screen_transaction(
        customer_id=None,
        counterparty_details={"name": "Test Corp", "country": "US"},
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject None customer_id"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_missing_counterparty_name(mock_audit_log):
    """Test that missing counterparty name is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"country": "US"},
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject missing counterparty name"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_empty_counterparty_name(mock_audit_log):
    """Test that empty counterparty name is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "   ", "country": "US"},
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject whitespace-only counterparty name"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_missing_counterparty_country(mock_audit_log):
    """Test that missing counterparty country is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "Test Corp"},
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject missing country"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_invalid_counterparty_details_type(mock_audit_log):
    """Test that non-dict counterparty_details is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details="not a dict",
        transaction_amount=10000
    )

    assert result["screened"] is False, "Should reject non-dict counterparty_details"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_negative_transaction_amount(mock_audit_log):
    """Test that negative transaction amount is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "Test Corp", "country": "US"},
        transaction_amount=-1000
    )

    assert result["screened"] is False, "Should reject negative transaction amount"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


def test_invalid_transaction_amount_type(mock_audit_log):
    """Test that non-numeric transaction amount is rejected."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "Test Corp", "country": "US"},
        transaction_amount="ten thousand"
    )

    assert result["screened"] is False, "Should reject non-numeric transaction amount"
    assert result["metadata"]["error"] == "invalid_input", "Should return invalid_input error"


# ---------------------------------------------------------------------------
# Test 6: Performance Assertion (< 200ms with mocks)
# ---------------------------------------------------------------------------

def test_performance_under_200ms(clean_transaction, mock_audit_log):
    """
    Test that sanctions screening completes in under 200ms when using mocks.
    This is a critical performance requirement for the pipeline.
    """
    start_time = time.monotonic()
    result = screen_transaction(**clean_transaction)
    elapsed_ms = (time.monotonic() - start_time) * 1000

    assert elapsed_ms < 200, f"Screening should complete in < 200ms, took {elapsed_ms:.2f}ms"
    assert result["metadata"]["execution_time_ms"] < 200, \
        f"Reported execution time should be < 200ms, was {result['metadata']['execution_time_ms']}ms"


def test_performance_with_ofac_match(ofac_match_transaction, mock_audit_log):
    """Test performance is maintained even when matches are found."""
    start_time = time.monotonic()
    result = screen_transaction(**ofac_match_transaction)
    elapsed_ms = (time.monotonic() - start_time) * 1000

    assert elapsed_ms < 200, f"OFAC match screening should complete in < 200ms, took {elapsed_ms:.2f}ms"


def test_performance_with_multiple_matches(un_match_transaction, mock_audit_log):
    """Test performance with high-risk transaction that may trigger multiple matches."""
    start_time = time.monotonic()
    result = screen_transaction(**un_match_transaction)
    elapsed_ms = (time.monotonic() - start_time) * 1000

    assert elapsed_ms < 200, f"Multi-match screening should complete in < 200ms, took {elapsed_ms:.2f}ms"


# ---------------------------------------------------------------------------
# Test 7: AML Context Integration
# ---------------------------------------------------------------------------

def test_high_risk_aml_lowers_threshold(un_match_transaction, mock_audit_log):
    """
    Test that high AML risk score (>= 7.0) lowers the fuzzy match threshold to 0.75.
    With threshold at 0.75, UN match at 0.83 should be detected.
    """
    # The UN match has score 0.83, which is below BASE_FUZZY_THRESHOLD (0.85)
    # but above HIGH_RISK_FUZZY_THRESHOLD (0.75)
    result = screen_transaction(**un_match_transaction)

    assert len(result["match_list"]) >= 1, "High AML risk should lower threshold and detect matches"
    un_match = next((m for m in result["match_list"] if m["list_source"] == "UN-1267"), None)
    assert un_match is not None, "Should detect UN match with lowered threshold"


def test_low_risk_aml_uses_base_threshold(ofac_match_transaction, mock_audit_log):
    """
    Test that low AML risk score uses base threshold (0.85).
    OFAC match at 0.91 should still be detected.
    """
    result = screen_transaction(**ofac_match_transaction)

    assert len(result["match_list"]) >= 1, "Should detect OFAC match with base threshold"
    ofac_match = next((m for m in result["match_list"] if m["list_source"] == "OFAC-SDN"), None)
    assert ofac_match is not None, "Should detect OFAC match above base threshold"


# ---------------------------------------------------------------------------
# Test 8: Audit Logging
# ---------------------------------------------------------------------------

def test_audit_log_created(clean_transaction, mock_audit_log):
    """Test that audit log entry is created after screening."""
    result = screen_transaction(**clean_transaction)

    audit_file = mock_audit_log / "sanctions_screening.jsonl"
    assert audit_file.exists(), "Audit log file should be created"

    # Read and verify audit log entry
    with open(audit_file, "r") as f:
        lines = f.readlines()
        assert len(lines) >= 1, "Should have at least one audit log entry"

        # Parse the last entry
        entry = json.loads(lines[-1])
        assert entry["customer_id"] == clean_transaction["customer_id"], "Should log customer_id"
        assert entry["input"]["counterparty_name"] == clean_transaction["counterparty_details"]["name"]
        assert entry["input"]["amount_inr"] == clean_transaction["transaction_amount"]
        assert entry["output"]["screened"] is True, "Should log screening success"
        assert entry["output"]["matches"] == 0, "Should log zero matches for clean transaction"


def test_no_pci_data_in_audit_log(ofac_match_transaction, mock_audit_log):
    """
    Test that audit log contains no sensitive PCI data (no card numbers, no plaintext PII).
    Verify only non-sensitive transaction details are logged.
    """
    result = screen_transaction(**ofac_match_transaction)

    audit_file = mock_audit_log / "sanctions_screening.jsonl"
    with open(audit_file, "r") as f:
        entry = json.loads(f.readlines()[-1])

        # Verify structure follows PCI DSS requirements
        assert "input" in entry, "Should have input section"
        assert "output" in entry, "Should have output section"

        # Verify only summary data in output (no raw match details)
        assert "matches" in entry["output"], "Should include match count"
        assert "match_list" not in entry["output"], "Should NOT include detailed match_list"

        # Verify no card/PAN data fields
        audit_json = json.dumps(entry)
        assert "card" not in audit_json.lower(), "Should not contain 'card' references"
        assert "pan" not in audit_json.lower(), "Should not contain 'pan' references"


# ---------------------------------------------------------------------------
# Test 9: API Error Handling
# ---------------------------------------------------------------------------

def test_api_error_returns_screening_failure(clean_transaction, mock_audit_log):
    """Test that API errors result in screened=False with appropriate error code."""
    with mock.patch("sanctions_check._query_sanctions_lists", side_effect=Exception("API timeout")):
        result = screen_transaction(**clean_transaction)

        assert result["screened"] is False, "API error should result in screening failure"
        assert result["metadata"]["error"] == "sanctions_api_error", "Should return API error code"
        assert result["match_list"] == [], "Failed screening should have empty match_list"
        assert result["confidence"] == 0.0, "Failed screening should have zero confidence"


# ---------------------------------------------------------------------------
# Test 10: Edge Cases
# ---------------------------------------------------------------------------

def test_case_insensitive_matching(mock_audit_log):
    """Test that name matching is case-insensitive."""
    # Test uppercase version
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "ACME INTERNATIONAL TRADING LLC", "country": "AE"},
        transaction_amount=50000
    )

    assert result["screened"] is True
    assert len(result["match_list"]) >= 1, "Should match regardless of case"


def test_zero_transaction_amount(clean_transaction, mock_audit_log):
    """Test that zero transaction amount is accepted."""
    clean_transaction["transaction_amount"] = 0
    result = screen_transaction(**clean_transaction)

    assert result["screened"] is True, "Zero amount should be valid"


def test_no_aml_result_defaults_to_safe(mock_audit_log):
    """Test that missing AML result defaults to safe values."""
    result = screen_transaction(
        customer_id="CUST-12345",
        counterparty_details={"name": "Clean Company", "country": "US"},
        transaction_amount=10000,
        aml_result=None  # No AML context provided
    )

    assert result["screened"] is True, "Should handle missing AML result gracefully"
    # With no AML result, should use base threshold (0.85)


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
