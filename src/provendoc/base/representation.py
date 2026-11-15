from abc import ABC, abstractmethod


class BaseRepresentation(ABC):
    """Abstract base class for representation builders: turn a
    Document into a representation result."""

    @abstractmethod
    def build(self, document):
        """Build the representation from ``document`` (the concrete
        representation type is decided by the subclass)."""
        pass
