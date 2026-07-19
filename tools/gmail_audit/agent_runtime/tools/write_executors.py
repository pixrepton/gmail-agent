
"""Real executors for write operations — called from _execute_composite_step after HITL approve.

Each executor takes (args: dict, *, mailbox_store=None, correlation_store=None, drive_client=None, **kwargs)
and returns {"status": "ok"|"error", "summary": "..."}.
"""

from __future__ import annotations

from log_config import get_logger
from typing import Any, Callable

logger = get_logger(__name__)


from case_engagement_bridge import resolve_engagement_id as _bridge_resolve_engagement_id


def _engagement_id_for_case(correlation_store: Any, case_id: str) -> str:
    """Resolve engagement_id for a mailbox case_id via correlation registry."""
    eid = _bridge_resolve_engagement_id(case_id, registry_store=correlation_store)
    return eid or ""


def _write_fact_row(
    *,
    case_id: str,
    fact_key: str,
    normalized_value: str,
    raw_value: str = "",
    source_ref: str = "agent:write_executor",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mailbox_memory.facts import stable_id
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fact_id = stable_id("fact", case_id, fact_key)
    return {
        "fact_id": fact_id,
        "case_id": case_id,
        "message_id": "agent",
        "document_id": "",
        "entity_scope": "case",
        "fact_key": fact_key,
        "normalized_value": normalized_value[:500],
        "raw_value": raw_value[:1000],
        "confidence": 1.0,
        "observed_at": now,
        "source_type": "agent_write",
        "source_ref": source_ref,
        "status": "active",
        "metadata": metadata or {},
    }


def execute_delete_document(
    args: dict,
    *,
    mailbox_store: Any = None,
    drive_client: Any = None,
    **kwargs,
) -> dict:
    """Faktycznie usuwa dokument z Drive."""
    file_id = str(args.get("file_id") or args.get("target") or "").strip()
    if not file_id:
        return {"status": "error", "summary": "Brak file_id."}
    try:
        if drive_client is not None:
            drive_client.delete_file(file_id=file_id)
            return {"status": "ok", "summary": f"Usunięto plik {file_id}"}
        if mailbox_store is not None:
            remove = getattr(mailbox_store, "remove_document", None)
            if callable(remove):
                remove(file_id)
                return {"status": "ok", "summary": f"Usunięto dokument {file_id} z mailbox store"}
        return {"status": "error", "summary": "Brak drive_client ani mailbox_store do usunięcia pliku."}
    except Exception as exc:
        logger.error("execute_delete_document failed for %s: %s", file_id, exc)
        return {"status": "error", "summary": f"Błąd usuwania pliku: {exc}"}


def execute_move_document(
    args: dict,
    *,
    mailbox_store: Any = None,
    drive_client: Any = None,
    **kwargs,
) -> dict:
    """Faktycznie przenosi dokument między folderami Drive."""
    file_id = str(args.get("file_id") or args.get("target") or "").strip()
    destination = str(args.get("destination") or args.get("folder_id") or "").strip()
    if not file_id or not destination:
        return {"status": "error", "summary": "Brak file_id lub destination."}
    try:
        if drive_client is not None:
            drive_client.move_file(file_id=file_id, folder_id=destination)
            return {"status": "ok", "summary": f"Przeniesiono plik {file_id} do {destination}"}
        return {"status": "error", "summary": "Brak drive_client do przeniesienia pliku."}
    except Exception as exc:
        logger.error("execute_move_document failed for %s: %s", file_id, exc)
        return {"status": "error", "summary": f"Błąd przenoszenia pliku: {exc}"}


def execute_merge_cases(
    args: dict,
    *,
    mailbox_store: Any = None,
    correlation_store: Any = None,
    **kwargs,
) -> dict:
    """Faktycznie łączy dwie sprawy — używa merge_data z case_intelligence.

    1. Pobiera dane obu spraw z mailbox_store
    2. Woła merge_data(case_a, case_b) z case_intelligence — scala fakty, dokumenty, historię
    3. Zapisuje scalone dane do target_case w mailbox_store
    4. Linkuje w correlation_registry (merged_into)
    5. Oznacza source jako merged
    """
    source = str(args.get("source_case_id") or args.get("source") or "").strip()
    target = str(args.get("target_case_id") or args.get("target") or "").strip()
    if not source or not target:
        return {"status": "error", "summary": "Brak source_case_id lub target_case_id."}
    try:
        from case_intelligence import merge_data

        merge_log: list[dict] = []
        merged_count = 0

        # 1. Pobierz dane obu spraw z mailbox_store
        case_a_data: dict = {}
        case_b_data: dict = {}
        if mailbox_store is not None:
            fetch = getattr(mailbox_store, "fetch_case", None)
            if callable(fetch):
                case_a_data = fetch(source) or {}
                case_b_data = fetch(target) or {}
            else:
                fetch_facts = getattr(mailbox_store, "fetch_facts_for_case", None)
                if callable(fetch_facts):
                    case_a_data["facts"] = fetch_facts(source) or []
                    case_b_data["facts"] = fetch_facts(target) or []

        # 2. Wołaj merge_data
        case_a_data["case_id"] = source
        case_b_data["case_id"] = target
        result = merge_data(case_a_data, case_b_data, merge_log=merge_log)
        merge_log = list(result.get("merge_log") or [])
        merged_data = result.get("merged", {})

        # 3. Zapisz scalone dane do target
        if mailbox_store is not None:
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert({"case_id": target, "merged_data": merged_data})
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append) and merged_data.get("facts"):
                append(merged_data["facts"])
            merged_count = result.get("merged_facts", 0)

        # 4. Link w correlation_registry
        if correlation_store is not None:
            upsert = getattr(correlation_store, "upsert_link", None)
            if callable(upsert):
                source_engagement_id = _engagement_id_for_case(correlation_store, source)
                if not source_engagement_id:
                    return {
                        "status": "error",
                        "summary": f"Brak engagement_id dla sprawy źródłowej {source} w correlation registry.",
                    }
                upsert(
                    engagement_id=source_engagement_id,
                    link_type="merged_into",
                    target_id=target,
                    source_repo="gmail-agent",
                    confidence=1.0,
                )

        warnings = ""
        conflicts = result.get("conflicts", [])
        if conflicts:
            warnings = f" Konflikty: {'; '.join(conflicts[:3])}."

        return {
            "status": "ok",
            "summary": f"Scalono {source} → {target}: {merged_count} faktów, "
            f"{result.get('merged_documents', 0)} dokumentów, "
            f"{result.get('merged_history', 0)} zdarzeń.{warnings}",
        }
    except Exception as exc:
        logger.error("execute_merge_cases failed: %s", exc)
        return {"status": "error", "summary": f"Błąd scalania spraw: {exc}"}


def execute_update_case_status(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Zmienia status sprawy w MailboxMemory.

    Lifecycle validation (2026-06-24): status strings are mapped to lifecycle
    states via CASE_STATUS_TO_LIFECYCLE and the transition is validated.
    """
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    new_status = str(args.get("status") or args.get("new_status") or "").strip()
    if not case_id or not new_status:
        return {"status": "error", "summary": "Brak case_id lub status."}

    # Lifecycle validation — map status to lifecycle state
    try:
        from llm_contracts.case_lifecycle import CaseLifecycleState, ALLOWED_TRANSITIONS, CASE_STATUS_TO_LIFECYCLE, validate_transition

        current = CaseLifecycleState.NEW_LEAD
        if mailbox_store is not None:
            getter = getattr(mailbox_store, "get_case_lifecycle", None)
            if callable(getter):
                current_raw = str(getter(case_id=case_id) or "")
                if current_raw:
                    try:
                        current = CaseLifecycleState(current_raw)
                    except ValueError:
                        pass
            if current == CaseLifecycleState.NEW_LEAD:
                getter = getattr(mailbox_store, "fetch_case", None)
                if callable(getter):
                    case_row = getter(case_id)
                    if case_row:
                        # Try lifecycle_state first
                        lifecycle_raw = str(case_row.get("lifecycle_state", "")).strip()
                        if lifecycle_raw:
                            try:
                                current = CaseLifecycleState(lifecycle_raw)
                            except ValueError:
                                pass
                        # If not set, try to map status to lifecycle
                        if current == CaseLifecycleState.NEW_LEAD and "lifecycle_state" not in (case_row or {}):
                            status_raw = str(case_row.get("status", "")).strip()
                            if status_raw:
                                mapped = CASE_STATUS_TO_LIFECYCLE.get(status_raw)
                                if mapped is not None:
                                    current = mapped

        target_lifecycle = CASE_STATUS_TO_LIFECYCLE.get(new_status)
        if target_lifecycle is not None:
            validation = validate_transition(current, target_lifecycle)
            if not validation.get("allowed"):
                return {"status": "error", "summary": validation.get("reason", f"Niepoprawne przejscie statusu: {current.value} -> {new_status}.")}
    except ImportError:
        logger.debug("case_lifecycle module not available — status validation skipped")

    try:
        if mailbox_store is not None:
            update = getattr(mailbox_store, "update_case_status", None)
            if callable(update):
                update(case_id=case_id, status=new_status)
                return {"status": "ok", "summary": f"Zmieniono status sprawy {case_id} na {new_status}."}
            # Fallback: bezpieczna mutacja przez mutate_case — zachowuje pozostałe
            # pola wiersza (metadata, customer_*, subject, ...). Nie używać
            # upsert_case() z częściowym wierszem: to pełne zastąpienie wiersza,
            # nie merge, i zeruje wszystko poza podanymi kluczami.
            mutate = getattr(mailbox_store, "mutate_case", None)
            if callable(mutate):
                def _apply_status(row: dict[str, Any], *, _status: str = new_status) -> dict[str, Any]:
                    row = dict(row)
                    row["status"] = _status
                    return row

                mutate(case_id, _apply_status)
                return {"status": "ok", "summary": f"Zmieniono status sprawy {case_id} na {new_status}."}
            # Fail-closed: store nie udostępnia ani update_case_status, ani
            # mutate_case. Częściowy upsert_case({"case_id":.., "status":..})
            # zastępuje CAŁY wiersz (metadata, customer_name, customer_email,
            # subject, case_key, last_source_kinds_seen wracają do wartości
            # domyślnych) — to jest destrukcyjne i niedopuszczalne. Nie
            # wykonuj żadnego zapisu, zwróć jawny błąd zamiast cichej utraty
            # danych.
            return {
                "status": "error",
                "summary": (
                    f"Brak bezpiecznej metody aktualizacji statusu sprawy {case_id}: "
                    "mailbox_store nie udostępnia ani update_case_status, ani "
                    "mutate_case. Częściowy upsert_case zniszczyłby istniejące "
                    "dane sprawy — zapis pominięty."
                ),
            }
        return {"status": "error", "summary": "Brak mailbox_store do aktualizacji statusu."}
    except Exception as exc:
        logger.error("execute_update_case_status failed: %s", exc)
        return {"status": "error", "summary": f"Błąd aktualizacji statusu: {exc}"}


def execute_update_case_lifecycle(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Zmienia stan lifecycle sprawy. Waliduje dozwolone przejście.

    Jeśli transition niedozwolone — zwraca error, nie rzuca wyjątku.
    """
    from llm_contracts.case_lifecycle import CaseLifecycleState, ALLOWED_TRANSITIONS, validate_transition

    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    target_state = str(args.get("lifecycle_state") or args.get("state") or args.get("target_state") or "").strip()
    reason_pl = str(args.get("reason_pl") or args.get("reason") or "").strip()
    if not case_id or not target_state:
        return {"status": "error", "summary": "Brak case_id lub lifecycle_state."}

    # Pobierz bieżący lifecycle z MailboxMemory
    current_raw = ""
    if mailbox_store is not None:
        getter = getattr(mailbox_store, "get_case_lifecycle", None)
        if callable(getter):
            current_raw = str(getter(case_id=case_id) or "")
        if not current_raw:
            getter = getattr(mailbox_store, "fetch_case", None)
            if callable(getter):
                case_row = getter(case_id)
                if case_row:
                    current_raw = str(case_row.get("lifecycle_state", case_row.get("status", "")))

    current = CaseLifecycleState(current_raw) if current_raw else CaseLifecycleState.NEW_LEAD

    # Walidacja przejścia
    try:
        validation = validate_transition(current, target_state)
    except ValueError as exc:
        return {"status": "error", "summary": f"Niepoprawna wartość stanu: {exc}"}

    if not validation.get("allowed"):
        return {
            "status": "error",
            "summary": validation.get("reason", f"Niedozwolone przejście: {current.value} → {target_state}"),
            "allowed_targets": validation.get("allowed_targets", []),
        }

    target = CaseLifecycleState(target_state)

    # Zapisz w MailboxMemory
    if mailbox_store is not None:
        setter = getattr(mailbox_store, "set_case_lifecycle", None)
        if callable(setter):
            setter(case_id=case_id, lifecycle_state=target.value, reason_pl=reason_pl)
        else:
            # Fallback: upsert_case z lifecycle_state w metadata
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert({"case_id": case_id, "lifecycle_state": target.value})
            else:
                return {"status": "error", "summary": "Brak mailbox_store do zapisu lifecycle_state."}

    return {
        "status": "ok",
        "summary": f"Zmieniono lifecycle sprawy {case_id}: {current.value} → {target.value}.",
        "previous": current.value,
        "current": target.value,
    }


def execute_add_case_note(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Dodaje notatkę do sprawy."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    note = str(args.get("note") or args.get("text") or "").strip()
    if not case_id or not note:
        return {"status": "error", "summary": "Brak case_id lub treści notatki."}
    try:
        if mailbox_store is not None:
            add_note = getattr(mailbox_store, "add_case_note", None)
            if callable(add_note):
                add_note(case_id=case_id, note=note)
                return {"status": "ok", "summary": f"Dodano notatkę do sprawy {case_id}."}
            # Fallback: zapisz jako fakt
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append):
                row = _write_fact_row(
                    case_id=case_id,
                    fact_key="case_note",
                    normalized_value=note,
                    raw_value=note,
                    metadata={"tool": "add_case_note"},
                )
                append([row])
                return {"status": "ok", "summary": f"Dodano notatkę do sprawy {case_id} (fakt)."}
        return {"status": "error", "summary": "Brak mailbox_store do dodania notatki."}
    except Exception as exc:
        logger.error("execute_add_case_note failed: %s", exc)
        return {"status": "error", "summary": f"Błąd dodawania notatki: {exc}"}


def execute_add_case_label(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Dodaje etykietę do sprawy."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    label = str(args.get("label") or "").strip()
    if not case_id or not label:
        return {"status": "error", "summary": "Brak case_id lub label."}
    try:
        if mailbox_store is not None:
            add_label = getattr(mailbox_store, "add_case_label", None)
            if callable(add_label):
                add_label(case_id=case_id, label=label)
                return {"status": "ok", "summary": f"Dodano etykietę '{label}' do sprawy {case_id}."}
            # Fallback: zapisz jako fact
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append):
                row = _write_fact_row(
                    case_id=case_id,
                    fact_key="case_label",
                    normalized_value=label,
                    raw_value=label,
                    metadata={"tool": "add_case_label"},
                )
                append([row])
                return {"status": "ok", "summary": f"Dodano etykietę '{label}' do sprawy {case_id} (fakt)."}
        return {"status": "error", "summary": "Brak mailbox_store do dodania etykiety."}
    except Exception as exc:
        logger.error("execute_add_case_label failed: %s", exc)
        return {"status": "error", "summary": f"Błąd dodawania etykiety: {exc}"}


def execute_archive_case(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Archiwizuje sprawę — lifecycle validation (2026-06-24).

    Archiwizacja dozwolona tylko gdy lifecycle pozwala na COMPLETED/LOST/STAGNATING.
    """
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}

    # Lifecycle validation
    try:
        from llm_contracts.case_lifecycle import CaseLifecycleState, validate_transition

        current_raw = ""
        if mailbox_store is not None:
            getter = getattr(mailbox_store, "get_case_lifecycle", None)
            if callable(getter):
                current_raw = str(getter(case_id=case_id) or "")
            if not current_raw:
                getter = getattr(mailbox_store, "fetch_case", None)
                if callable(getter):
                    case_row = getter(case_id)
                    if case_row:
                        current_raw = str(case_row.get("lifecycle_state", case_row.get("status", "")))
        current = CaseLifecycleState(current_raw) if current_raw else CaseLifecycleState.NEW_LEAD

        validation = validate_transition(current, CaseLifecycleState.COMPLETED)
        if not validation.get("allowed"):
            lost_check = validate_transition(current, CaseLifecycleState.LOST)
            stag_check = validate_transition(current, CaseLifecycleState.STAGNATING)
            if not lost_check.get("allowed") and not stag_check.get("allowed"):
                return {"status": "error", "summary": validation.get("reason", f"Archiwizacja niedozwolona z poziomu {current.value}.")}
    except ImportError:
        logger.debug("case_lifecycle module not available — archive validation skipped")

    try:
        if mailbox_store is not None:
            archive = getattr(mailbox_store, "archive_case", None)
            if callable(archive):
                archive(case_id=case_id)
                return {"status": "ok", "summary": f"Zarchiwizowano sprawę {case_id}."}
            # Fallback: update status
            update = getattr(mailbox_store, "update_case_status", None)
            if callable(update):
                update(case_id=case_id, status="archived")
                return {"status": "ok", "summary": f"Zarchiwizowano sprawę {case_id} (status)."}
        return {"status": "error", "summary": "Brak mailbox_store do archiwizacji."}
    except Exception as exc:
        logger.error("execute_archive_case failed: %s", exc)
        return {"status": "error", "summary": f"Błąd archiwizacji: {exc}"}


def execute_restore_case(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Przywraca sprawę z archiwum — lifecycle validation (2026-06-24).

    Przywrócenie dozwolone tylko gdy lifecycle pozwala na QUALIFICATION.
    """
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}

    # Lifecycle validation
    try:
        from llm_contracts.case_lifecycle import CaseLifecycleState, validate_transition

        current_raw = ""
        if mailbox_store is not None:
            getter = getattr(mailbox_store, "get_case_lifecycle", None)
            if callable(getter):
                current_raw = str(getter(case_id=case_id) or "")
            if not current_raw:
                getter = getattr(mailbox_store, "fetch_case", None)
                if callable(getter):
                    case_row = getter(case_id)
                    if case_row:
                        current_raw = str(case_row.get("lifecycle_state", case_row.get("status", "")))
        current = CaseLifecycleState(current_raw) if current_raw else CaseLifecycleState.NEW_LEAD

        validation = validate_transition(current, CaseLifecycleState.QUALIFICATION)
        if not validation.get("allowed"):
            return {"status": "error", "summary": validation.get("reason", f"Przywrócenie niedozwolone z poziomu {current.value}.")}
    except ImportError:
        logger.debug("case_lifecycle module not available — restore validation skipped")

    try:
        if mailbox_store is not None:
            restore = getattr(mailbox_store, "restore_case", None)
            if callable(restore):
                restore(case_id=case_id)
                return {"status": "ok", "summary": f"Przywrócono sprawę {case_id}."}
            update = getattr(mailbox_store, "update_case_status", None)
            if callable(update):
                update(case_id=case_id, status="open")
                return {"status": "ok", "summary": f"Przywrócono sprawę {case_id} (status open)."}
        return {"status": "error", "summary": "Brak mailbox_store do przywrócenia sprawy."}
    except Exception as exc:
        logger.error("execute_restore_case failed: %s", exc)
        return {"status": "error", "summary": f"Błąd przywracania: {exc}"}


def execute_reassign_case(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Przypisuje sprawę do innego operatora."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    assignee = str(args.get("assignee") or "").strip()
    if not case_id or not assignee:
        return {"status": "error", "summary": "Brak case_id lub assignee."}
    try:
        if mailbox_store is not None:
            reassign = getattr(mailbox_store, "reassign_case", None)
            if callable(reassign):
                reassign(case_id=case_id, assignee=assignee)
                return {"status": "ok", "summary": f"Przypisano sprawę {case_id} do {assignee}."}
        return {"status": "error", "summary": "Brak mailbox_store do reassign."}
    except Exception as exc:
        logger.error("execute_reassign_case failed: %s", exc)
        return {"status": "error", "summary": f"Błąd reassign: {exc}"}


def execute_link_case_to_case(
    args: dict,
    *,
    mailbox_store: Any = None,
    correlation_store: Any = None,
    **kwargs,
) -> dict:
    """Łączy dwie sprawy (link, nie merge)."""
    source = str(args.get("source_case_id") or args.get("target") or "").strip()
    target = str(args.get("target_case_id") or args.get("other_case_id") or "").strip()
    if not source or not target:
        return {"status": "error", "summary": "Brak source_case_id lub target_case_id."}
    try:
        if correlation_store is not None:
            upsert = getattr(correlation_store, "upsert_link", None)
            if callable(upsert):
                source_engagement_id = _engagement_id_for_case(correlation_store, source)
                if not source_engagement_id:
                    return {
                        "status": "error",
                        "summary": f"Brak engagement_id dla sprawy {source} w correlation registry.",
                    }
                upsert(
                    engagement_id=source_engagement_id,
                    link_type="linked_case",
                    target_id=target,
                    source_repo="gmail-agent",
                    confidence=1.0,
                )
        if mailbox_store is not None:
            link = getattr(mailbox_store, "link_cases", None)
            if callable(link):
                link(source_case_id=source, target_case_id=target)
        return {"status": "ok", "summary": f"Połączono sprawy {source} ↔ {target}."}
    except Exception as exc:
        logger.error("execute_link_case_to_case failed: %s", exc)
        return {"status": "error", "summary": f"Błąd łączenia spraw: {exc}"}


def execute_update_customer_info(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Aktualizuje dane klienta w sprawie."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}
    updates = {k: v for k, v in args.items() if k in ("customer_name", "customer_email", "customer_phone", "customer_address")}
    if not updates:
        return {"status": "error", "summary": "Brak danych do aktualizacji."}
    try:
        if mailbox_store is not None:
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert({"case_id": case_id, **updates})
                return {"status": "ok", "summary": f"Zaktualizowano dane klienta w sprawie {case_id}: {', '.join(updates.keys())}."}
        return {"status": "error", "summary": "Brak mailbox_store do aktualizacji."}
    except Exception as exc:
        logger.error("execute_update_customer_info failed: %s", exc)
        return {"status": "error", "summary": f"Błąd aktualizacji danych: {exc}"}


# ── Pending operation stubs (PR-5B: implement real executors) ─────────────────


def execute_create_case(
    args: dict,
    *,
    mailbox_store: Any = None,
    correlation_store: Any = None,
    **kwargs,
) -> dict:
    """Tworzy nową sprawę w MailboxMemory."""
    import uuid
    case_id = str(args.get("case_id") or args.get("target") or f"case_{uuid.uuid4().hex[:12]}").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}
    try:
        from case_routing import apply_routing_to_case_row, classify_mailbox_row

        customer_email = str(args.get("customer_email") or args.get("email") or "").strip()
        customer_name = str(args.get("customer_name") or args.get("name") or "").strip()
        export_type = str(args.get("export_case_type") or "lead_oferta").strip()
        family_hint = str(args.get("case_family") or args.get("family") or "lead_opportunity").strip()
        routing = classify_mailbox_row(family_hint, "materialize", export_type)
        row = apply_routing_to_case_row(
            {
                "case_id": case_id,
                "customer_email": customer_email,
                "customer_name": customer_name,
                "status": "open",
                "lifecycle_state": "new_lead",
            },
            routing,
        )
        if mailbox_store is not None:
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert(row)
            else:
                create = getattr(mailbox_store, "create_case", None)
                if callable(create):
                    create(case_id=case_id, customer_email=customer_email, customer_name=customer_name)
                else:
                    return {"status": "error", "summary": "Brak mailbox_store do utworzenia sprawy."}

            engagement_id = str(
                kwargs.get("engagement_id")
                or args.get("engagement_id")
                or args.get("staging_engagement_id")
                or ""
            ).strip()
            if correlation_store is not None and engagement_id:
                from agent_runtime.materialize import (
                    _register_email_identity,
                    _register_engagement_link,
                )

                _register_engagement_link(
                    correlation_store,
                    engagement_id=engagement_id,
                    case_id=case_id,
                )
                if customer_email:
                    _register_email_identity(
                        correlation_store,
                        email=customer_email,
                        case_id=case_id,
                        customer_name=customer_name,
                    )
            elif correlation_store is not None and engagement_id == "":
                logger.warning(
                    "execute_create_case: brak engagement_id — pominięto correlation registry dla %s",
                    case_id,
                )

            return {"status": "ok", "case_id": case_id, "summary": f"Utworzono sprawę {case_id}."}
        return {"status": "error", "summary": "Brak mailbox_store do utworzenia sprawy."}
    except Exception as exc:
        logger.error("execute_create_case failed: %s", exc)
        return {"status": "error", "summary": f"Błąd tworzenia sprawy: {exc}"}


def execute_send_email(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Wysyła email przez Gmail API — deleguje do execute_hitl_gmail_send."""
    to = str(args.get("to") or args.get("recipient") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or args.get("message") or "").strip()
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    if not to:
        return {"status": "error", "summary": "Brak adresata (to)."}
    try:
        from hitl_gmail_send import execute_hitl_gmail_send
        result = execute_hitl_gmail_send(
            to=to,
            subject=subject or "(brak tematu)",
            body_text=body,
            case_id=case_id,
        )
        if result.get("executed"):
            return {"status": "ok", "summary": f"Wysłano email do {to}: {subject}"}
        return {"status": "ok", "summary": f"Email do {to} zakolejkowany (dry_run): {subject}"}
    except Exception as exc:
        logger.error("execute_send_email failed: %s", exc)
        return {"status": "error", "summary": f"Błąd wysyłki email: {exc}"}


def execute_generate_draft(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Generuje draft odpowiedzi w Gmail — zapisuje jako fakt w MailboxMemory."""
    to = str(args.get("to") or args.get("recipient") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or args.get("message") or "").strip()
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}
    try:
        if mailbox_store is not None:
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append):
                row = _write_fact_row(
                    case_id=case_id,
                    fact_key="agent_draft",
                    normalized_value=f"To: {to}, Subject: {subject}",
                    raw_value=body,
                    source_ref="agent:write_executor:generate_draft",
                    metadata={"to": to, "subject": subject, "tool": "generate_draft"},
                )
                append([row])
                return {"status": "ok", "summary": f"Wygenerowano draft dla {to}: {subject}"}
            upsert = getattr(mailbox_store, "upsert_case", None)
            if callable(upsert):
                upsert({"case_id": case_id, "draft": {"to": to, "subject": subject, "body": body[:500]}})
                return {"status": "ok", "summary": f"Zapisano draft dla {to} (upsert)."}
        return {"status": "error", "summary": "Brak mailbox_store do zapisu draftu."}
    except Exception as exc:
        logger.error("execute_generate_draft failed: %s", exc)
        return {"status": "error", "summary": f"Błąd generowania draftu: {exc}"}


def execute_schedule_visit(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Planuje wizytę serwisową — zapisuje jako fakt w MailboxMemory."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    date = str(args.get("date") or args.get("scheduled_date") or "").strip()
    address = str(args.get("address") or args.get("visit_address") or "").strip()
    technician = str(args.get("technician") or args.get("assignee") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}
    if not date:
        return {"status": "error", "summary": "Brak daty wizyty."}
    try:
        if mailbox_store is not None:
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append):
                row = _write_fact_row(
                    case_id=case_id,
                    fact_key="scheduled_visit",
                    normalized_value=f"Date: {date}, Address: {address}",
                    raw_value=f"Technician: {technician}, Date: {date}, Address: {address}",
                    source_ref="agent:write_executor:schedule_visit",
                    metadata={"date": date, "address": address, "technician": technician},
                )
                append([row])
                return {"status": "ok", "summary": f"Zaplanowano wizytę {date} dla sprawy {case_id}."}
        return {"status": "error", "summary": "Brak mailbox_store do zapisu wizyty."}
    except Exception as exc:
        logger.error("execute_schedule_visit failed: %s", exc)
        return {"status": "error", "summary": f"Błąd planowania wizyty: {exc}"}


def execute_add_deadline(
    args: dict,
    *,
    mailbox_store: Any = None,
    **kwargs,
) -> dict:
    """Dodaje termin do sprawy — zapisuje jako fakt."""
    case_id = str(args.get("case_id") or args.get("target") or "").strip()
    deadline = str(args.get("deadline") or args.get("date") or "").strip()
    description = str(args.get("description") or args.get("note") or "").strip()
    if not case_id:
        return {"status": "error", "summary": "Brak case_id."}
    if not deadline:
        return {"status": "error", "summary": "Brak daty terminu."}
    try:
        if mailbox_store is not None:
            append = getattr(mailbox_store, "append_fact_rows", None)
            if callable(append):
                row = _write_fact_row(
                    case_id=case_id,
                    fact_key="case_deadline",
                    normalized_value=f"Deadline: {deadline}",
                    raw_value=f"Deadline: {deadline}, Description: {description}",
                    source_ref="agent:write_executor:add_deadline",
                    metadata={"deadline": deadline, "description": description},
                )
                append([row])
                return {"status": "ok", "summary": f"Dodano termin {deadline} do sprawy {case_id}."}
        return {"status": "error", "summary": "Brak mailbox_store do zapisu terminu."}
    except Exception as exc:
        logger.error("execute_add_deadline failed: %s", exc)
        return {"status": "error", "summary": f"Błąd dodawania terminu: {exc}"}


# ── Idempotent wrapper (PR-5B) ─────────────────────────────────────────────


def _with_idempotency(
    executor_fn: Callable,
    *,
    name: str | None = None,
) -> Callable:
    """Wrap executor with idempotency_key check/record.

    Każdy executor przyjmuje opcjonalny idempotency_key: Optional[str] = None.
    Jeśli podany — sprawdza w idempotency_log czy operacja była już wykonana.
    """
    op_name = str(name or executor_fn.__name__)

    def wrapper(
        args: dict,
        *,
        mailbox_store: Any = None,
        correlation_store: Any = None,
        drive_client: Any = None,
        db_url: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict:
        # Idempotency check before execution
        if idempotency_key and db_url:
            from agent_runtime.idempotency import check_idempotency

            cached = check_idempotency(db_url, idempotency_key)
            if cached is not None:
                return dict(cached["result"])
        # Execute
        result = executor_fn(
            args,
            mailbox_store=mailbox_store,
            correlation_store=correlation_store,
            drive_client=drive_client,
            **kwargs,
        )
        # Record after execution
        if idempotency_key and db_url and result.get("status") == "ok":
            from agent_runtime.idempotency import record_idempotency

            record_idempotency(db_url, idempotency_key, op_name, result)
        return result

    wrapper.__name__ = executor_fn.__name__
    wrapper.__qualname__ = executor_fn.__qualname__
    wrapper.__doc__ = executor_fn.__doc__
    return wrapper


# ── WRITE_EXECUTORS registry (dynamiczny — source of truth dla propose_mutation) ──
# Każdy executor owinięty _with_idempotency (PR-5B)

WRITE_EXECUTORS: dict[str, Callable] = {
    "delete_document": _with_idempotency(execute_delete_document),
    "move_document": _with_idempotency(execute_move_document),
    "merge_cases": _with_idempotency(execute_merge_cases),
    "update_case_status": _with_idempotency(execute_update_case_status),
    "update_case_lifecycle": _with_idempotency(execute_update_case_lifecycle),
    "add_case_note": _with_idempotency(execute_add_case_note),
    "add_case_label": _with_idempotency(execute_add_case_label),
    "archive_case": _with_idempotency(execute_archive_case),
    "restore_case": _with_idempotency(execute_restore_case),
    "reassign_case": _with_idempotency(execute_reassign_case),
    "link_case_to_case": _with_idempotency(execute_link_case_to_case),
    "update_customer_info": _with_idempotency(execute_update_customer_info),
    # Stubby — do implementacji w PR-5B
    "create_case": _with_idempotency(execute_create_case),
    "send_email": _with_idempotency(execute_send_email),
    "generate_draft": _with_idempotency(execute_generate_draft),
    "schedule_visit": _with_idempotency(execute_schedule_visit),
    "add_deadline": _with_idempotency(execute_add_deadline),
}


__all__ = ["WRITE_EXECUTORS"]
