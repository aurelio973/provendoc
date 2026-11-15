from abc import ABC, abstractmethod


class BaseExporter(ABC):
    """Abstract base class for exporters: write pipeline outputs to a path."""

    @abstractmethod
    def export(
            self,
            *args,
            **kwargs
    ):
        """Write pipeline outputs to a path (format decided by subclasses).

        Convention: the first positional argument is the data to export;
        the last keyword argument is usually the output path ``path``.
        Subclasses declare the concrete signature for their payload
        (repr_result / policy / doc_quality etc.); callers follow the
        subclass signature.
        """
        pass
