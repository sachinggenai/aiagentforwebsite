"""The vocabulary net — sibling-domain language must not return to product code.

CL-1…CL-9 (`work-status.md` §10) removed the copied voice engine's
university-admissions vocabulary from every live path. This is the net that
keeps it out: it would have failed on the day before each of those nine tasks
landed.

**What is scanned.** Only *string literals* and the names of functions, classes
and variables — the text a caller can hear, read, or be matched against.
Docstrings and comments are deliberately exempt: the migration's audit trail
lives there and prose cannot reach a caller. That distinction is why the
exemption list is three files rather than twenty.

**The exemptions are earned, not assumed.** Each exempt file must actually
contain the vocabulary it is exempted for (`test_every_exemption_is_earned`);
if the term leaves the file, the exemption fails and must be deleted with it.

**Tests are out of scope, deliberately.** The negative fixtures name the
sibling on purpose — `test_mod09_boot` refuses a boot whose db is `admissions`,
`test_shared_rag` rejects a corpus marked `meridian` — and those fixtures are
themselves guards.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent

sys.path.insert(0, str(PLATFORM / "app"))

SCAN_DIRS = (
    PLATFORM / "app" / "anita",
    REPO / "shared",
    PLATFORM / "scripts",
)
SCAN_FILES = (PLATFORM / "streamlit_app.py",)

#: The sibling product's domain as it reached this repository: its name, its
#: corpus markers, its admissions vocabulary, and the source module the voice
#: engine was copied from. Word-boundary matched, case-insensitive — `mba`
#: must not fire inside `samba`, `umd` not inside another word.
SIBLING_VOCABULARY = (
    "meridian",
    "university",
    "student",
    "university of maryland",
    "terrapin",
    "fafsa",
    "mddcs",
    "mhec",
    "fairleigh dickinson",
    "umd",
    "fdu",
    "admissions",
    "tuition",
    "mba",
    "ielts",
    "toefl",
    "gmat",
    "campus",
    "semester",
    "enrollment",
    "scholarship",
    "intake",
    "voice_handler",
)

_PATTERNS = tuple(
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in SIBLING_VOCABULARY
)

#: Files whose *job* is to name the sibling deployment so it can be refused.
#: Keyed by repo-relative path and paired with the reason: an exemption without
#: a reason is how an allow-list becomes a blindfold.
EXEMPT = {
    "platform/app/anita/config.py": (
        "the isolation guard — names the sibling's db, collection, ports and "
        "corpus markers so a colliding boot is refused"
    ),
    "platform/app/anita/index.py": (
        "FORBIDDEN_CORPUS_MARKERS — refuses to index a corpus that belongs to "
        "the sibling product"
    ),
    "platform/app/anita/stores.py": (
        "FORBIDDEN_COLLECTIONS — refuses a vector store that belongs to the "
        "sibling product"
    ),
}


def _scanned_paths() -> list[Path]:
    paths: list[Path] = []
    for directory in SCAN_DIRS:
        paths.extend(
            p for p in sorted(directory.rglob("*.py")) if "__pycache__" not in p.parts
        )
    paths.extend(p for p in SCAN_FILES if p.exists())
    return paths


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of Constant nodes that are docstrings — prose, not code."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def violations_in(source: str) -> list[tuple[int, str, str]]:
    """Every (line, term, text) hit in *code* — string literals and names."""
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    found: set[tuple[int, str, str]] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            text = node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = node.name
        elif isinstance(node, ast.Name):
            text = node.id
        else:
            continue

        for term, pattern in _PATTERNS:
            if pattern.search(text):
                found.add((node.lineno, term, text))

    return sorted(found)


class TestNoSiblingVocabulary:
    def test_the_scanner_finds_planted_terms(self):
        """The net's own test. A scanner that silently finds nothing would pass
        every run; this plants a term in each of the three shapes it checks."""
        planted = (
            'CLOSING = "Thank you for calling Meridian University Admissions."\n'
            "def meridian_helper():\n"
            "    admissions = 1\n"
            "    return admissions\n"
        )
        terms = {term for _, term, _ in violations_in(planted)}
        assert {"meridian", "admissions"} <= terms, (
            f"the scanner missed a planted term; found {sorted(terms)}"
        )

    def test_the_scan_covers_the_product_tree(self):
        paths = _scanned_paths()
        assert len(paths) > 40, f"only {len(paths)} files scanned — did a root move?"
        names = {p.name for p in paths}
        assert "voice.py" in names, "the engine is not being scanned"
        assert "core.py" in names, "the shared sentiment package is not being scanned"

    def test_product_code_carries_no_sibling_vocabulary(self):
        violations: list[str] = []
        for path in _scanned_paths():
            rel = path.relative_to(REPO).as_posix()
            if rel in EXEMPT:
                continue
            for lineno, term, text in violations_in(path.read_text(encoding="utf-8")):
                violations.append(f"{rel}:{lineno} [{term}] {text[:70]!r}")

        assert not violations, (
            "sibling-domain vocabulary reached product code again:\n  "
            + "\n  ".join(violations)
            + "\n\nIf this is a deliberate isolation guard, add the file to EXEMPT "
            "in this test with a reason — and expect the exemption to be reviewed."
        )

    def test_every_exemption_is_earned(self):
        """An exemption must be doing something: the file it covers must still
        contain the vocabulary. When a term leaves a guard file, the exemption
        is deleted in the same change."""
        stale: list[str] = []
        for rel, reason in EXEMPT.items():
            path = REPO / rel
            assert path.exists(), f"exempt file no longer exists: {rel}"
            if not violations_in(path.read_text(encoding="utf-8")):
                stale.append(f"{rel} — {reason}")
        assert not stale, (
            "these exemptions are no longer needed and must be removed:\n  "
            + "\n  ".join(stale)
        )
