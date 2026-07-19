"""Case Coherence Validator — waliduje spójność faktów i stanu sprawy.

Generic Hands: wywoływany przez propose_mutation i execute_materialize_proposal.
Nigdy nie rzuca wyjątku — zawsze zwraca CoherenceResult.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_contracts.case_lifecycle import ALLOWED_TRANSITIONS, CaseLifecycleState, validate_transition
from log_config import get_logger

COHERENCE_NUMERIC_CONFLICT_RATIO = 0.2  # 20% różnicy między źródłami to konflikt

logger = get_logger("case_coherence")


@dataclass
class CoherenceResult:
    """Wynik walidacji spójności.

    Attributes:
        is_coherent: True jeśli brak blokerów (warnings nie wpływają)
        blocks: twarde blokery — mutacja nie może przejść
        warnings: miękkie ostrzeżenia — mutacja może przejść z flagą
        checked_rules: lista nazw sprawdzonych reguł
    """
    is_coherent: bool = True
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_rules: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_coherent

    def merge(self, other: CoherenceResult) -> CoherenceResult:
        """Scala dwa wyniki walidacji."""
        return CoherenceResult(
            is_coherent=self.is_coherent and other.is_coherent,
            blocks=self.blocks + other.blocks,
            warnings=self.warnings + other.warnings,
            checked_rules=self.checked_rules + other.checked_rules,
        )


class CaseCoherenceValidator:
    """Waliduje spójność faktów i stanu sprawy przed wykonaniem mutacji.

    Wywoływany przez propose_mutation i execute_materialize_proposal.
    """

    # ── Znane klucze faktów, które nie mogą się zmieniać bez ostrzeżenia ──
    STABLE_FACT_KEYS = frozenset({
        "heated_area_m2",
        "plot_area_m2",
        "building_area_m2",
        "property_address",
        "property_city",
        "property_postal_code",
        "installation_type",
        "customer_name",
        "customer_company",
        "customer_nip",
        "customer_regon",
        "customer_email",
        "customer_phone",
        "device_count",
        "power_kw",
    })

    # ── Mapowanie typów akcji na minimalny wymagany stan lifecycle ──
    ACTION_TO_MIN_LIFECYCLE: dict[str, set[CaseLifecycleState]] = {
        "archive_case": {
            CaseLifecycleState.COMPLETED,
            CaseLifecycleState.LOST,
            CaseLifecycleState.STAGNATING,
        },
        "send_email": {
            CaseLifecycleState.NEW_LEAD,
            CaseLifecycleState.QUALIFICATION,
            CaseLifecycleState.OFFER_PREP,
            CaseLifecycleState.WAITING_CLIENT,
            CaseLifecycleState.NEGOTIATION,
        },
        "generate_draft": {
            CaseLifecycleState.OFFER_PREP,
            CaseLifecycleState.WAITING_CLIENT,
            CaseLifecycleState.NEGOTIATION,
        },
        "merge_cases": {
            CaseLifecycleState.NEW_LEAD,
            CaseLifecycleState.QUALIFICATION,
        },
        "delete_document": {
            CaseLifecycleState.NEW_LEAD,
            CaseLifecycleState.QUALIFICATION,
            CaseLifecycleState.OFFER_PREP,
        },
    }

    # ── Akcje blokowane w terminalnych stanach ──
    TERMINAL_BLOCKED_ACTIONS = frozenset({
        "update_case_status",
        "add_case_note",
        "add_case_label",
        "update_case_lifecycle",
        "update_customer_info",
        "reassign_case",
    })

    def validate_fact_consistency(
        self,
        existing_facts: list[dict],
        new_fact_key: str,
        new_fact_value: Any,
    ) -> CoherenceResult:
        """Sprawdza czy nowy fakt jest sprzeczny z istniejącymi.

        Args:
            existing_facts: lista faktów z CaseContextPack.active_facts
            new_fact_key: klucz nowego faktu (np. "heated_area_m2")
            new_fact_value: wartość nowego faktu (np. 200)

        Returns:
            CoherenceResult z blokerami/warnings
        """
        rules_checked: list[str] = []
        blocks: list[str] = []
        warnings: list[str] = []

        if not existing_facts or not new_fact_key:
            return CoherenceResult(checked_rules=["empty_input"])

        new_key_lower = new_fact_key.strip().lower()

        for fact in existing_facts:
            key = str(fact.get("key", fact.get("fact_key", fact.get("name", "")))).strip().lower()
            value = fact.get("value", fact.get("fact_value", ""))

            if key == new_key_lower:
                rules_checked.append(f"same_key_check:{new_key_lower}")

                if new_key_lower in self.STABLE_FACT_KEYS:
                    # Porównanie wartości — ostrzeżenie jeśli różne
                    old_str = str(value).strip().lower() if value is not None else ""
                    new_str = str(new_fact_value).strip().lower() if new_fact_value is not None else ""

                    if old_str and new_str and old_str != new_str:
                        warnings.append(
                            f"Fakt '{new_fact_key}' zmienia się: '{value}' → '{new_fact_value}'. "
                            f"To może wskazywać na niespójność danych."
                        )
                        rules_checked.append(f"stable_value_change:{new_key_lower}")

                # Detekcja konfliktu numerycznego
                try:
                    old_num = float(value) if value is not None else None
                    new_num = float(new_fact_value) if new_fact_value is not None else None
                    if old_num is not None and new_num is not None and old_num != new_num:
                        ratio = abs(new_num - old_num) / max(abs(old_num), 0.01)
                        if ratio > COHERENCE_NUMERIC_CONFLICT_RATIO:  # >20% różnicy = bloker
                            blocks.append(
                                f"Konflikt numeryczny: {new_fact_key}={new_fact_value} "
                                f"(poprzednio: {value}, różnica {ratio:.0%}). "
                                f"Zweryfikuj który fakt jest poprawny."
                            )
                            rules_checked.append(f"numeric_conflict:{new_key_lower}")
                except (ValueError, TypeError):
                    pass  # nie-numeryczne — pomiń

        is_coherent = len(blocks) == 0
        return CoherenceResult(
            is_coherent=is_coherent,
            blocks=blocks,
            warnings=warnings,
            checked_rules=rules_checked or ["no_matching_facts"],
        )

    def validate_lifecycle_action(
        self,
        current_lifecycle: str,
        proposed_action_type: str,
    ) -> CoherenceResult:
        """Sprawdza czy dana akcja jest dozwolona w obecnym stanie lifecycle.

        Args:
            current_lifecycle: aktualny stan lifecycle (np. "new_lead", "completed")
            proposed_action_type: typ akcji (np. "archive_case", "send_email")

        Returns:
            CoherenceResult z blokerami/warnings
        """
        rules_checked: list[str] = []
        blocks: list[str] = []
        warnings: list[str] = []

        if not current_lifecycle:
            return CoherenceResult(checked_rules=["no_lifecycle_state"])

        action = proposed_action_type.strip().lower()

        # Sprawdź stan terminalny
        try:
            state = CaseLifecycleState(current_lifecycle)
        except ValueError:
            return CoherenceResult(
                warnings=[f"Nieznany stan lifecycle: {current_lifecycle}"],
                checked_rules=["unknown_lifecycle_state"],
            )

        if state in {
            CaseLifecycleState.COMPLETED,
            CaseLifecycleState.LOST,
        }:
            if action in self.TERMINAL_BLOCKED_ACTIONS:
                blocks.append(
                    f"Akcja '{action}' jest zablokowana w stanie terminalnym '{state.value}'. "
                    f"Sprawa jest zamknięta."
                )
                rules_checked.append(f"terminal_block:{action}")

        # Sprawdź minimalny wymagany lifecycle dla akcji
        allowed_states = self.ACTION_TO_MIN_LIFECYCLE.get(action)
        if allowed_states is not None:
            if state not in allowed_states:
                allowed_names = ", ".join(sorted(s.value for s in allowed_states))
                blocks.append(
                    f"Akcja '{action}' jest niedozwolona w stanie lifecycle '{state.value}'. "
                    f"Dozwolona w: {allowed_names}."
                )
                rules_checked.append(f"action_lifecycle_restriction:{action}")
            else:
                rules_checked.append(f"action_allowed_in_state:{action}")

        is_coherent = len(blocks) == 0
        return CoherenceResult(
            is_coherent=is_coherent,
            blocks=blocks,
            warnings=warnings,
            checked_rules=rules_checked or ["lifecycle_action_ok"],
        )

    def validate_mutation_coherence(
        self,
        snapshot: Any,
        mutation_payload: dict,
    ) -> CoherenceResult:
        """Master validator — woła fact_consistency + lifecycle_action.

        Args:
            snapshot: EngagementSnapshotV2 obiekt (mająca case_id, operational_status)
            mutation_payload: payload mutacji z propozycji

        Returns:
            CoherenceResult — lista WARNINGS i BLOCKS
        """
        results: list[CoherenceResult] = []

        # Pobierz lifecycle
        lifecycle_state = ""
        if hasattr(snapshot, "lifecycle_state"):
            lifecycle_state = str(getattr(snapshot, "lifecycle_state", ""))
        if not lifecycle_state and hasattr(snapshot, "case_lifecycle"):
            lifecycle_state = str(getattr(snapshot, "case_lifecycle", ""))
        if not lifecycle_state and hasattr(snapshot, "operational_status"):
            op = getattr(snapshot, "operational_status", None)
            if hasattr(op, "code"):
                from llm_contracts.case_lifecycle import map_operational_to_lifecycle
                lifecycle_state = map_operational_to_lifecycle(
                    str(getattr(op, "code", "") or "")
                ).value

        # 1. Walidacja lifecycle-akcja
        action_type = str(mutation_payload.get("operation") or mutation_payload.get("action") or "")
        if action_type:
            lifecycle_result = self.validate_lifecycle_action(
                current_lifecycle=lifecycle_state,
                proposed_action_type=action_type,
            )
            results.append(lifecycle_result)

        # 2. Walidacja spójności faktów
        facts = mutation_payload.get("facts", mutation_payload.get("values", []))
        if isinstance(facts, dict):
            for fact_key, fact_value in facts.items():
                existing = self._extract_existing_facts(snapshot)
                fact_result = self.validate_fact_consistency(
                    existing_facts=existing,
                    new_fact_key=fact_key,
                    new_fact_value=fact_value,
                )
                results.append(fact_result)
        elif isinstance(facts, list):
            for fact_item in facts:
                if isinstance(fact_item, dict):
                    fact_key = str(fact_item.get("key", fact_item.get("fact_key", "")))
                    fact_value = fact_item.get("value", fact_item.get("fact_value"))
                    if fact_key:
                        existing = self._extract_existing_facts(snapshot)
                        fact_result = self.validate_fact_consistency(
                            existing_facts=existing,
                            new_fact_key=fact_key,
                            new_fact_value=fact_value,
                        )
                        results.append(fact_result)

        # Scal wyniki
        final = CoherenceResult()
        for r in results:
            final = final.merge(r)

        case_id = getattr(snapshot, "case_id", "") if hasattr(snapshot, "case_id") else ""
        logger.info("COHERENCE_VALIDATION", extra={"x": {
            "case_id": str(case_id),
            "is_coherent": final.is_coherent,
            "blocks_count": len(final.blocks),
            "warnings_count": len(final.warnings),
            "checked_rules": final.checked_rules[:10],
        }})
        return final

    def _extract_existing_facts(self, snapshot: Any) -> list[dict]:
        """Wyciąga istniejące fakty ze snapshotu."""
        facts: list[dict] = []

        # Z EngagementSnapshotV2
        if hasattr(snapshot, "agent_memory"):
            memory = getattr(snapshot, "agent_memory", None)
            if memory is not None:
                m_facts = getattr(memory, "facts", getattr(memory, "known_facts", None))
                if isinstance(m_facts, list):
                    facts.extend(m_facts)

        # Z operational_status.hvac_profile
        if hasattr(snapshot, "hvac_profile"):
            profile = getattr(snapshot, "hvac_profile", None)
            if profile is not None:
                profile_items = getattr(profile, "system_params", getattr(profile, "items", None))
                if isinstance(profile_items, list):
                    for item in profile_items:
                        if isinstance(item, dict):
                            facts.append({
                                "key": item.get("name", item.get("key", "")),
                                "value": item.get("value", ""),
                            })

        return facts


__all__ = [
    "CaseCoherenceValidator",
    "CoherenceResult",
]
