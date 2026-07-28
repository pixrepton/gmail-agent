"""STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 Phase 4 — regression guard for the
financial_document_without_payable_context semantic-validation fix.

Root cause (confirmed via live diagnostic re-run on DOC-01/MI-04): the rule required
amounts/invoice_numbers evidence to accept the flag, but the flag's own meaning is "a
financial document IS present, but payable context could NOT be extracted" -- i.e. those
fields are expected to be EMPTY when the flag legitimately applies. The fix adds
`has_attachments AND business_area=="finance"` (BOTH together) as additional valid evidence,
alongside the unchanged, backward-compatible has_amounts/has_invoice_refs OR path.

Post-review tightening: an EARLIER version of this fix used OR instead of AND between
has_attachments and business_area=="finance", which an adversarial review correctly flagged
as too broad (a marketing_growth case with an unrelated PDF attached would have passed).
Every live re-run of DOC-01/MI-04 that legitimately carried this flag had BOTH conditions
true simultaneously, so requiring both together still fully covers the real evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intake_schema import _validate_semantics  # noqa: E402


def _base_valid_review_doc(**over) -> dict:
    doc = {
        "schema_version": "1.0",
        "source": {"channel": "gmail", "mailbox": "x@example.com", "observed_at": "2026-01-01T00:00:00Z"},
        "message": {"message_id": "m1", "date": "", "sender": "a@example.com", "to": [], "subject": "x",
                    "has_attachments": True, "cc": [], "snippet": "x", "labels": []},
        "thread": {"thread_id": "", "thread_position": "unknown", "is_reply_or_forward": False,
                   "thread_summary": "x", "linked_case_candidates": []},
        "business_area": "finance",
        "primary_signal": {"code": "invoice_received", "name": "x", "description": "x", "business_significance": "x"},
        "secondary_signals": [],
        "case_assessment": {"case_family": "finance_settlement", "is_new_case": True,
                             "state_detected": "received", "state_change": {"detected": False}},
        "decision": {"action": "review", "action_rationale": "x"},
        "priority": "medium",
        "confidence": {"signal_confidence": 0.5, "case_link_confidence": 0.1,
                        "decision_confidence": 0.6, "extraction_confidence": 0.2},
        "review": {"required": True, "flags": ["financial_document_without_payable_context"]},
        "reason": "x",
        "extracted_data": {"entities": {"people": [], "organizations": [], "locations": [], "products": []},
                            "dates": [], "amounts": [], "references": {"invoice_numbers": [], "shipment_numbers": [],
                            "order_numbers": [], "transaction_numbers": [], "case_ids": []}, "deadlines": [],
                            "lead_details": {}},
    }
    doc.update(over)
    return doc


def test_financial_document_flag_valid_with_attachment_and_finance_area_no_amounts():
    doc = _base_valid_review_doc()
    errors = _validate_semantics(doc)
    assert not any("financial_document_without_payable_context" in e for e in errors)


def test_financial_document_flag_invalid_with_finance_area_but_no_attachment():
    # business_area=="finance" ALONE (no attachment) is no longer sufficient -- both
    # conditions must hold together
    doc = _base_valid_review_doc()
    doc["message"] = dict(doc["message"], has_attachments=False)
    errors = _validate_semantics(doc)
    assert any("financial_document_without_payable_context" in e for e in errors)


def test_financial_document_flag_invalid_with_attachment_but_business_area_not_finance():
    # has_attachments ALONE (business_area not finance) is no longer sufficient -- both
    # conditions must hold together
    doc = _base_valid_review_doc(business_area="lead")
    errors = _validate_semantics(doc)
    assert any("financial_document_without_payable_context" in e for e in errors)


def test_financial_document_flag_rejects_unrelated_attachment_in_non_finance_case():
    # adversarial-review counter-case: a marketing_growth case with an unrelated PDF
    # attached must NOT be able to rubber-stamp this flag
    doc = _base_valid_review_doc(business_area="marketing_growth")
    doc["case_assessment"] = dict(doc["case_assessment"], case_family="training_or_marketing")
    errors = _validate_semantics(doc)
    assert any("financial_document_without_payable_context" in e for e in errors)


def test_financial_document_flag_still_invalid_with_zero_evidence_of_any_kind():
    # neither attachment, nor finance area, nor amounts/invoice numbers -- the flag is
    # still correctly rejected as unsupported (the fix widens, it does not remove the check)
    doc = _base_valid_review_doc(business_area="lead")
    doc["message"] = dict(doc["message"], has_attachments=False)
    errors = _validate_semantics(doc)
    assert any("financial_document_without_payable_context" in e for e in errors)


def test_financial_document_flag_still_valid_via_amounts_backward_compatible():
    doc = _base_valid_review_doc(business_area="lead")
    doc["message"] = dict(doc["message"], has_attachments=False)
    doc["extracted_data"] = dict(doc["extracted_data"], amounts=[{"value": 100, "currency": "PLN"}])
    errors = _validate_semantics(doc)
    assert not any("financial_document_without_payable_context" in e for e in errors)


def test_fix_only_widens_never_narrows_other_review_flag_rules():
    # deadline_found_without_owner and other flag-consistency rules are untouched
    doc = _base_valid_review_doc(review={"required": True, "flags": ["deadline_found_without_owner"]})
    errors = _validate_semantics(doc)
    assert any("deadline_found_without_owner" in e for e in errors)
