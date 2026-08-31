from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence, TypedDict, runtime_checkable


class RetrievalAction(str, Enum):
    """How a backend's retrieval policy handles Tavily results."""

    RETURN_LOCAL = "return_local"
    MERGE_FOREIGN = "merge_foreign"
    FALLBACK_TO_FOREIGN = "fallback_to_foreign"


@dataclass(frozen=True)
class LocalSearchRequest:
    """The provider-neutral input for a local retrieval operation."""

    query: str
    query_embeddings: Sequence[float]
    max_results: int
    max_local: int


class TavilyResult(TypedDict):
    """Required fields from a Tavily result used by Hybrid RAG.

    Tavily responses may include additional fields such as ``url`` and
    ``title``. Providers can retain and persist those fields as needed.
    """

    content: str
    score: float


class LocalSearchResult(TypedDict):
    """Provider-neutral result shape returned to the hybrid client."""

    content: str
    score: float
    origin: Literal["local", "foreign"]


PersistenceDocument = Dict[str, Any]


@dataclass(frozen=True)
class PersistenceContext:
    """Context attached to Tavily results before a provider persists them."""

    query: Optional[str] = None
    cache_hit: bool = False


@dataclass
class RetrievalDecision:
    """A backend's local result and its hybrid-search routing decision.

    The backend owns local retrieval and decides whether the shared client
    should return local results, merge them with Tavily, or fall back to
    Tavily. This lets database-specific policies such as Oracle cache and
    durable-memory routing remain outside ``TavilyHybridClient``.
    """

    action: RetrievalAction
    local_results: List[LocalSearchResult] = field(default_factory=list)

    @classmethod
    def return_local(
            cls, results: List[LocalSearchResult]
    ) -> "RetrievalDecision":
        return cls(RetrievalAction.RETURN_LOCAL, results)

    @classmethod
    def merge_foreign(
            cls, results: List[LocalSearchResult]
    ) -> "RetrievalDecision":
        return cls(RetrievalAction.MERGE_FOREIGN, results)

    @classmethod
    def fallback_to_foreign(cls) -> "RetrievalDecision":
        return cls(RetrievalAction.FALLBACK_TO_FOREIGN)

    
@runtime_checkable
class HybridRAGBackend(Protocol):
    """Configured local store and hybrid-search policy for ``TavilyHybridClient``.

    A backend owns its database operations and decides whether a local lookup
    should return immediately, merge with Tavily, or fall back to Tavily. It
    must return ``LocalSearchResult`` dictionaries from ``retrieve()`` and
    receives ``TavilyResult`` dictionaries when persisting foreign results.
    """

    def validate(self) -> None:
        ...

    def retrieve(self, request: LocalSearchRequest) -> RetrievalDecision:
        ...

    def build_persistence_document(
            self,
            foreign_result: TavilyResult,
            embedding: Sequence[float],
            context: PersistenceContext,
    ) -> PersistenceDocument:
        ...

    def select_foreign_results_for_persistence(
            self,
            foreign_results: List[TavilyResult],
    ) -> List[TavilyResult]:
        ...

    def persist(self, documents: List[PersistenceDocument]) -> None:
        ...

    def close(self) -> None:
        ...


class DatabaseAdapter:
    """Optional base class for straightforward Hybrid RAG backends.

    Subclasses only need to implement ``retrieve()``,
    ``build_persistence_document()``, and ``persist()`` for the usual
    merge-based flow. They can override these defaults when they need custom
    validation, foreign-result selection, or resource cleanup.
    """

    def validate(self) -> None:
        return None

    def select_foreign_results_for_persistence(
            self,
            foreign_results: List[TavilyResult],
    ) -> List[TavilyResult]:
        return foreign_results

    def close(self) -> None:
        return None


# ``Database`` remains the compatibility name used by the initial adapter API.
# New provider implementations should use ``HybridRAGBackend`` in annotations
# or subclass ``DatabaseAdapter`` for the default lifecycle behavior.
Database = HybridRAGBackend
