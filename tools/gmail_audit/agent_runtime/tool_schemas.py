"""OpenAI function schemas for agent tools (PR-C)."""

from __future__ import annotations

from typing import Any


def _all_operations() -> list[str]:
    """Zwraca wszystkie operacje write z WRITE_EXECUTORS jako listę.
    To jest jedyne miejsce gdzie definiujemy listę operacji dla OpenAI schemas.
    """
    try:
        from agent_runtime.tools.write_executors import WRITE_EXECUTORS

        return sorted(WRITE_EXECUTORS.keys())
    except ImportError:
        # Fallback dla testów / import cycle
        return [
            "add_case_label", "add_case_note", "add_deadline",
            "archive_case", "create_case", "delete_document",
            "generate_draft", "link_case_to_case", "merge_cases",
            "move_document", "reassign_case", "restore_case",
            "schedule_visit", "send_email", "update_case_status",
            "update_customer_info",
        ]


def openai_tool_definitions(allowlist: tuple[str, ...]) -> list[dict[str, Any]]:
    _ops = _all_operations()
    specs = {
        "read_google_drive_file": {
            "type": "function",
            "function": {
                "name": "read_google_drive_file",
                "description": "Pobierz i sparsuj plik Drive (OCR/Docling).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                        "file_name": {"type": "string"},
                    },
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            },
        },
        "extract_facts_from_text": {
            "type": "function",
            "function": {
                "name": "extract_facts_from_text",
                "description": "Wyciągnij metraż, miasto i typ budynku z tekstu maila/sygnału.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "check_cp2025_eligibility": {
            "type": "function",
            "function": {
                "name": "check_cp2025_eligibility",
                "description": "Deterministyczna ocena kwalifikacji CP2025 z profilu HVAC.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "call_kalk_top_quote": {
            "type": "function",
            "function": {
                "name": "call_kalk_top_quote",
                "description": "WYWOŁAJ TYLKO gdy sprawa ma case_id i potrzebujesz wyceny. NIE uzywaj dla nowego leada bez case_id. Najpierw utworz sprawe przez propose_mutation(operation=create_case).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "heated_area_m2": {"type": "number", "description": "Powierzchnia ogrzewana (m2)"},
                        "location": {"type": "string", "description": "Miasto/miejscowosc"},
                        "building_type": {"type": "string", "description": "Typ budynku"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "request_operator_clarification": {
            "type": "function",
            "function": {
                "name": "request_operator_clarification",
                "description": "Zatrzymaj i poproś operatora o decyzję / brakujące dane.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ask_pl": {"type": "string"},
                    },
                    "required": ["ask_pl"],
                    "additionalProperties": False,
                },
            },
        },
        "report_gaps_and_stop": {
            "type": "function",
            "function": {
                "name": "report_gaps_and_stop",
                "description": "Zakończ run i ustaw pending_operator z listą gaps.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "search_rag_knowledge": {
            "type": "function",
            "function": {
                "name": "search_rag_knowledge",
                "description": "Przeszukaj semantycznie chunki wiadomości i dokumentów powiązane z bieżącą sprawą (mailbox + Drive), nie ogólną bazę dokumentacji HVAC.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Wymagane zapytanie w języku polskim (niepuste)"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        "query_anything": {
            "type": "function",
            "function": {
                "name": "query_anything",
                "description": "Zadaj pytanie do wszystkich źródeł wiedzy: RAG, pamięć temporalna, podobne sprawy, kontekst sprawy. Czytanie nie wymaga zgody operatora.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Pytanie w języku polskim"},
                        "sources": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["rag", "temporal", "similar", "mail"]},
                            "description": "Które źródła przeszukać (domyślnie wszystkie)",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        "propose_mutation": {
            "type": "function",
            "function": {
                "name": "propose_mutation",
                "description": "GŁÓWNE narzędzie do tworzenia spraw i generowania draftów. Dla nowego leada: operation=create_case. Po utworzeniu: operation=generate_draft. Dostępne operacje: create_case, generate_draft, update_case_status, schedule_visit, send_email, add_case_note.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": _ops,
                            "description": "Typ operacji do wykonania",
                        },
                        "target": {"type": "string", "description": "ID/cel operacji (file_id, case_id, itp.)"},
                        "payload": {
                            "type": "object",
                            "description": "Dodatkowe parametry specyficzne dla operacji",
                        },
                        "reasoning_pl": {"type": "string", "description": "Uzasadnienie dlaczego ta operacja jest potrzebna"},
                    },
                    "required": ["operation", "target", "reasoning_pl"],
                    "additionalProperties": False,
                },
            },
        },
        "propose_plan": {
            "type": "function",
            "function": {
                "name": "propose_plan",
                "description": "Propose a multi-step plan for operator approval. Each step can be a write operation (with operation code) or a read tool (with tool name).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_title_pl": {"type": "string", "description": "Tytuł planu dla operatora"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_name_pl": {"type": "string", "description": "Etykieta kroku dla operatora"},
                                    "step_description_pl": {"type": "string", "description": "Szczegółowy opis kroku"},
                                    "operation": {
                                        "type": "string",
                                        "enum": _ops,
                                        "description": "Kod operacji write (jeśli krok wymaga zapisu)",
                                    },
                                    "args": {
                                        "type": "object",
                                        "description": "Parametry specyficzne dla operacji",
                                    },
                                    "tool": {
                                        "type": "string",
                                        "description": "Nazwa narzędzia read (jeśli krok wymaga odczytu, np. search_rag_knowledge)",
                                    },
                                },
                                "additionalProperties": False,
                            }
                        },
                        "reasoning_pl": {"type": "string", "description": "Uzasadnienie planu"},
                    },
                    "required": ["plan_title_pl", "steps", "reasoning_pl"],
                    "additionalProperties": False,
                },
            },
        },
        # DELIVERY-1 RC-2: the following were allowlisted (MAIL_AGENT_TOOL_ALLOWLIST and/or
        # CHAT_AGENT_TOOL_ALLOWLIST) with a real handler in agent_runtime/tools/handlers.py,
        # but had no schema entry here — openai_tool_definitions() silently dropped them from
        # the tools param sent to the model, so choosing one crashed the entire chat-completion
        # request (RC-2, EVAL-1). See tool-reachability-inventory.md for the full classification.
        "search_gmail_thread": {
            "type": "function",
            "function": {
                "name": "search_gmail_thread",
                "description": "Pobierz ostatnie wiadomości Gmail powiązane z bieżącą sprawą (case_id).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "list_drive_folder": {
            "type": "function",
            "function": {
                "name": "list_drive_folder",
                "description": "Wylistuj pliki w folderze Google Drive (domyślnie: skonfigurowany folder root, jeśli folder_id nie podano).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "folder_id": {"type": "string", "description": "ID folderu Drive (opcjonalne — domyślnie root)"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "generate_draft_reply": {
            "type": "function",
            "function": {
                "name": "generate_draft_reply",
                "description": "Przygotuj deterministyczny szablon odpowiedzi na podstawie profilu klienta (bez wysyłki, wymaga zatwierdzenia operatora). System generuje treść automatycznie — NIE przekazuj gotowej treści draftu w argumentach, jedyny argument to intent (klasyfikacja, nie treść).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "enum": ["quote", "missing_info"],
                            "description": "Etykieta klasyfikacji wyboru szablonu, NIE pole treści: quote — wstępna kalkulacja gotowa; missing_info — proś o brakujące dane techniczne.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "retry_hard_parse": {
            "type": "function",
            "function": {
                "name": "retry_hard_parse",
                "description": "Wymuś ponowne parsowanie trudnego dokumentu (OCR/hard lane) po nieudanej pierwszej próbie.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "ID pliku Drive"},
                        "file_name": {"type": "string"},
                    },
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            },
        },
        "get_pipeline_summary": {
            "type": "function",
            "function": {
                "name": "get_pipeline_summary",
                "description": "Business Pulse: podsumowanie pipeline'u spraw (liczba, wartość, etapy).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_client_health": {
            "type": "function",
            "function": {
                "name": "get_client_health",
                "description": "Business Pulse: kondycja relacji z klientami (kto czeka za długo, kto wymaga uwagi).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_daily_delta": {
            "type": "function",
            "function": {
                "name": "get_daily_delta",
                "description": "Business Pulse: co zmieniło się od wczoraj (nowe sprawy, decyzje, postępy).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_win_rate": {
            "type": "function",
            "function": {
                "name": "get_win_rate",
                "description": "Business Pulse: wskaźnik wygranych ofert.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_top_clients": {
            "type": "function",
            "function": {
                "name": "get_top_clients",
                "description": "Business Pulse: najważniejsi klienci wg wartości/aktywności.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_revenue_forecast": {
            "type": "function",
            "function": {
                "name": "get_revenue_forecast",
                "description": "Business Pulse: prognoza przychodu na podstawie aktywnego pipeline'u.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_system_health_snapshot": {
            "type": "function",
            "function": {
                "name": "get_system_health_snapshot",
                "description": "Business Pulse: stan techniczny systemu (worker, integracje, błędy).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_business_signals": {
            "type": "function",
            "function": {
                "name": "get_business_signals",
                "description": "Business Pulse: istotne sygnały biznesowe wymagające uwagi operatora.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        "get_agent_activity_summary": {
            "type": "function",
            "function": {
                "name": "get_agent_activity_summary",
                "description": "Business Pulse: podsumowanie aktywności agenta (ile spraw, ile propozycji, ile decyzji operatora).",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
    }
    return [specs[name] for name in allowlist if name in specs]
