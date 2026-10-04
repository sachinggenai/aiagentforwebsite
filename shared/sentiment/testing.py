"""Test helpers a second product can import.

`assert_store_conforms` makes CR-01's DoD mechanically checkable: "a second
consumer can instantiate both modules with its own config and no code edits".
For a store, that means an implementation satisfies the Protocol by *shape*, and
a product can prove its own store is acceptable without reading this package's
source.
"""

from __future__ import annotations

from .store import SentimentStore


def assert_store_conforms(store: object) -> None:
    """Raise if ``store`` does not satisfy :class:`SentimentStore`.

    Checks behaviour, not just attribute presence: a store that has the methods
    but returns the wrong shape fails the first time the core reads from it, and
    that is a worse place to find out.
    """
    required = ("save", "for_lead", "recent_scores", "count_labelled_outcomes")
    missing = [name for name in required if not callable(getattr(store, name, None))]
    if missing:
        raise TypeError(
            f"{type(store).__name__} does not satisfy SentimentStore — "
            f"missing callable(s): {', '.join(missing)}"
        )
    if not isinstance(store, SentimentStore):
        # runtime_checkable Protocols check method presence; reaching here means
        # the names exist but the runtime protocol disagrees, which is worth
        # surfacing rather than tolerating.
        raise TypeError(f"{type(store).__name__} is not a runtime SentimentStore")


__all__ = ["assert_store_conforms"]
