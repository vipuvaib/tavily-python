from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence


class RetrievalAction(str, Enum):
    """How the hybrid client should handle a provider's local results."""

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


@dataclass(frozen=True)
class PersistenceContext:
    """Context attached to Tavily results before a provider persists them."""

    query: Optional[str] = None
    cache_hit: bool = False


@dataclass
class RetrievalDecision:
    """A provider's retrieval result and the next orchestration action.

    Providers use this value to own local retrieval policy without making the
    hybrid client aware of provider-specific modes such as Oracle's cache and
    durable-memory tiers.
    """

    action: RetrievalAction
    local_results: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def return_local(cls, results: List[Dict[str, Any]]) -> "RetrievalDecision":
        return cls(RetrievalAction.RETURN_LOCAL, results)

    @classmethod
    def merge_foreign(cls, results: List[Dict[str, Any]]) -> "RetrievalDecision":
        return cls(RetrievalAction.MERGE_FOREIGN, results)

    @classmethod
    def fallback_to_foreign(cls) -> "RetrievalDecision":
        return cls(RetrievalAction.FALLBACK_TO_FOREIGN)


class Database(Protocol):
    """Configured local-store adapter used by :class:`TavilyHybridClient`."""

    def validate(self) -> None:
        ...

    def retrieve(self, request: LocalSearchRequest) -> RetrievalDecision:
        ...

    def build_persistence_document(
            self,
            foreign_result: Dict[str, Any],
            embedding: Sequence[float],
            context: PersistenceContext,
    ) -> Dict[str, Any]:
        ...

    def select_foreign_results_for_persistence(
            self,
            foreign_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        ...

    def persist(self, documents: List[Dict[str, Any]]) -> None:
        ...

    def close(self) -> None:
        ...
