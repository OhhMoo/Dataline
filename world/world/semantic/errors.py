"""The two ways the semantic core refuses (see __init__ rule 1)."""
from __future__ import annotations


class SemanticError(Exception):
    """Base class: every refusal the semantic core raises."""


class Unsupported(SemanticError):
    """A construct outside the supported subset.

    `feature` is named the way the VENDOR names it (`USERELATIONSHIP`, `table
    calculation WINDOW_SUM`, `bidirectional cross-filter`), so the message an agent
    reads is the one an engineer would search for. Raised at parse time when a
    front-end cannot lower the text exactly, and at evaluation time when the engine
    cannot evaluate a tree exactly. Never caught and approximated.
    """

    def __init__(self, feature: str, vendor: str | None = None, detail: str = ""):
        self.feature, self.vendor, self.detail = feature, vendor, detail
        where = f" ({vendor})" if vendor else ""
        tail = f": {detail}" if detail else ""
        super().__init__(f"unsupported{where}: {feature}{tail}")


class ModelError(SemanticError):
    """A model or report that is malformed: an unknown table, column or measure, a
    relationship between columns that do not exist, a cycle of measure references,
    a visual grouping by columns no single fact reaches. The author's mistake, not
    a gap in the subset."""
