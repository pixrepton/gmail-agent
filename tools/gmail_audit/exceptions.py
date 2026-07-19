"""Custom exception hierarchy for TOP-INSTAL gmail-agent.

Structure:
  TopInstalError (base, always carries context)
  ├── ExternalServiceError
  │   ├── LLMError
  │   │   ├── LLMTimeoutError
  │   │   ├── LLMRateLimitError
  │   │   ├── LLMInvalidResponseError
  │   │   └── LLMHallucinationError
  │   ├── GmailAPIError
  │   │   └── GmailAuthError
  │   ├── DaszekClientError
  │   ├── RAGError
  │   └── KalkTopError
  ├── SignalProcessingError
  │   ├── SignalParseError
  │   ├── SignalClassificationError
  │   ├── SignalReconcileError
  │   └── StagingDeduplicationError
  ├── AgentError
  │   ├── AgentToolError
  │   ├── AgentBudgetExceededError
  │   ├── AgentConstitutionViolation
  │   ├── AgentPolicyViolation
  │   └── AgentPlannerError
  ├── WriteError
  │   ├── WriteIdempotencyError
  │   ├── WritePreconditionError
  │   ├── WriteTransactionError
  │   └── WriteCommitError
  ├── DataValidationError
  │   ├── ContractViolationError
  │   └── IntakeError
"""

from typing import Any


class TopInstalError(Exception):
    """Bazowy wyjątek. Zawsze niesie context."""

    def __init__(self, message: str, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.context = context or {}

    def __str__(self) -> str:
        if self.context:
            ctx = ", ".join(
                f"{k}={v}" for k, v in self.context.items()
            )
            return f"{super().__str__()} [{ctx}]"
        return super().__str__()


# ── Zewnętrzne serwisy ──────────────────────────────────────────────


class ExternalServiceError(TopInstalError):
    """Błąd zewnętrznego serwisu (API, DB, network)."""


class LLMError(ExternalServiceError):
    """Błąd wywołania modelu językowego."""


class LLMTimeoutError(LLMError):
    """Timeout wywołania LLM — provider nie odpowiedział w terminie."""


class LLMRateLimitError(LLMError):
    """Przekroczono limit żądań do providera LLM."""


class LLMInvalidResponseError(LLMError):
    """LLM zwrócił odpowiedź której nie można sparsować lub zwalidować."""


class LLMHallucinationError(LLMError):
    """LLM wywołał narzędzie którego nie ma lub z pustym targetem."""


class GmailAPIError(ExternalServiceError):
    """Błąd API Gmail."""


class GmailAuthError(GmailAPIError):
    """Błąd autoryzacji Gmail — token wygasł lub brak uprawnień."""


class DaszekClientError(ExternalServiceError):
    """Błąd komunikacji z Daszek (WordPress)."""


class RAGError(ExternalServiceError):
    """Błąd zapytania do RAG backend."""


class KalkTopError(ExternalServiceError):
    """Błąd komunikacji z kalk-top (wyceny)."""


# ── Pipeline ─────────────────────────────────────────────────────────


class SignalProcessingError(TopInstalError):
    """Błąd przetwarzania sygnału w pipeline."""


class SignalParseError(SignalProcessingError):
    """Nie można sparsować surowego sygnału."""


class SignalClassificationError(SignalProcessingError):
    """Klasyfikacja sygnału nie powiodła się."""


class SignalReconcileError(SignalProcessingError):
    """Reconciliacja sygnału nie powiodła się."""


class StagingDeduplicationError(SignalProcessingError):
    """Błąd deduplikacji staging engagement — duplikaty mogą przejść."""


# ── Agent ────────────────────────────────────────────────────────────


class AgentError(TopInstalError):
    """Błąd wykonania agenta (mail lub chat)."""


class AgentToolError(AgentError):
    """Błąd wykonania narzędzia przez agenta."""


class AgentBudgetExceededError(AgentError):
    """Przekroczono budżet narzędzia — agent nie może go więcej użyć."""


class AgentConstitutionViolation(AgentError):
    """Agent naruszył konstytucję — próba wykonania zabronionej akcji."""


class AgentPolicyViolation(AgentError):
    """Agent naruszył politykę — akcja zablokowana przez policy_guardrails."""


class AgentPlannerError(AgentError):
    """Błąd planera — LLM nie wygenerował poprawnego planu narzędzi."""


# Alias backward compatibility
OpenAIAgentPlannerError = AgentPlannerError


# ── Write ────────────────────────────────────────────────────────────


class WriteError(TopInstalError):
    """Błąd operacji zapisu."""


class WriteIdempotencyError(WriteError):
    """Naruszenie idempotency — próba ponownego zapisu z tym samym kluczem."""


class WritePreconditionError(WriteError):
    """Niespełniony warunek wstępny przed zapisem."""


class WriteTransactionError(WriteError):
    """Błąd transakcji — częściowy zapis możliwy."""


class WriteCommitError(WriteError):
    """Błąd commitu — dane mogły nie zostać utrwalone."""


# ── Data ─────────────────────────────────────────────────────────────


class DataValidationError(TopInstalError):
    """Błąd walidacji danych."""


class ContractViolationError(DataValidationError):
    """Naruszenie kontraktu między komponentami."""


class IntakeError(TopInstalError):
    """Błąd pipeline'u intake — przetwarzanie wiadomości nie powiodło się."""


class PreclassificationError(IntakeError):
    """Błąd preklasyfikacji — nie można ustalić toru przetwarzania."""


class SignalExtractionError(SignalProcessingError):
    """Błąd ekstrakcji sygnałów HVAC z wiadomości."""


class BusinessReasoningError(IntakeError):
    """Błąd business reasoning — LLM nie zinterpretował znaczenia biznesowego."""


# ── Case Intelligence ─────────────────────────────────────────────────


class CaseIntelligenceError(TopInstalError):
    """Błąd warstwy inteligencji sprawy (case_intelligence)."""


class CaseCoherenceError(CaseIntelligenceError):
    """Naruszenie spójności sprawy — fakt lub akcja niezgodna ze stanem."""


class RiskAssessmentError(CaseIntelligenceError):
    """Błąd oceny ryzyka — nie można określić ryzyk dla sprawy."""


class MissingInfoError(CaseIntelligenceError):
    """Błąd określenia brakujących informacji."""


# ── Materialize / Write ──────────────────────────────────────────────


class MaterializeError(TopInstalError):
    """Błąd materializacji propozycji agenta."""


class CaseLookupError(MaterializeError):
    """Nie znaleziono case_id — wyszukiwanie po email/identyfikatorze nie dało wyniku."""


# ── Skrzat Copilot ───────────────────────────────────────────────────


class SkrzatError(TopInstalError):
    """Błąd asystenta Skrzat — copilot nie odpowiedział."""
