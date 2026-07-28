"""STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 post-review — the case_kind
classifier must consume every canonical hvac_intent the extractor can produce.

Two independent adversarial reviews found the same gap: `odroczenie_decyzji` was a member of
HVAC_INTENT_CANONICAL_VALUES but absent from _HVAC_INTENT_TO_KIND, and the raw-text keyword
heuristics have no rule for deferral language -- so a correctly-detected deferral fell all the
way through to "niezaklasyfikowane", discarding the signal.

This test is exhaustive over the canonical vocabulary rather than per-value, so adding a new
canonical intent without wiring it into the classifier fails here immediately.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.tools.handlers import _HVAC_INTENT_TO_KIND, _classify_case_kind  # noqa: E402
from llm_contracts.signal_extraction import HVAC_INTENT_CANONICAL_VALUES  # noqa: E402

# "nieznane" is the deliberate exception: no intent was detected, so the raw-text keyword
# heuristics are meant to remain the fallback.
_INTENTIONALLY_UNMAPPED = {"nieznane"}


def test_every_canonical_hvac_intent_except_the_unknown_sentinel_is_mapped():
    unmapped = (HVAC_INTENT_CANONICAL_VALUES - _INTENTIONALLY_UNMAPPED) - set(_HVAC_INTENT_TO_KIND)
    assert unmapped == set(), f"canonical hvac_intent values with no case_kind mapping: {sorted(unmapped)}"


def test_no_canonical_intent_falls_through_to_unclassified_on_bare_text():
    # with no other HVAC keyword in the body, the intent alone must still produce a real bucket
    for intent in sorted(HVAC_INTENT_CANONICAL_VALUES - _INTENTIONALLY_UNMAPPED):
        kind = _classify_case_kind(business_area="", case_family="", hvac_intent=intent, text="Dzien dobry.")
        assert kind != "niezaklasyfikowane", f"{intent} fell through to niezaklasyfikowane"


def test_deferred_decision_classifies_as_a_sales_pipeline_case():
    kind = _classify_case_kind(
        business_area="",
        case_family="",
        hvac_intent="odroczenie_decyzji",
        # deliberately no "wycena"/"oferta"/"pompa ciep" keyword -- the intent must carry it
        text="Musze to jeszcze przemyslec z zona, wrocimy za miesiac.",
    )
    assert kind == "wycena_oferta"


def test_unknown_sentinel_still_defers_to_the_raw_text_heuristics():
    # the documented exception must keep working: "nieznane" does not short-circuit step 3
    kind = _classify_case_kind(
        business_area="", case_family="", hvac_intent="nieznane", text="Pompa ciepla nie dziala od tygodnia."
    )
    assert kind == "awaria_naprawa"
