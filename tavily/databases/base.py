from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

"""
TODO: We can maybe add a small fake-provider contract test that is independent of MongoDB and Oracle. It should cover 
local-only results, merging Tavily results, fallback to Tavily, persistence, validation, and cleanup. This will show that 
the abstraction actually works for future providers.
"""

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

"""
TODO: The provider contract relies heavily on Dict[str, Any], which makes the expected result and persistence shapes unclear. 
It would help to define typed structures, or at least document and test those shapes before the interface becomes public.
"""
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

"""
TODO: The database adapter currently decides whether Tavily should be called and whether its results should be merged or 
used as a fallback. That is more than database retrieval—it is hybrid-search policy. We should either make that 
responsibility explicit in the interface or keep the routing decision in the hybrid client.
"""
@runtime_checkable
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
