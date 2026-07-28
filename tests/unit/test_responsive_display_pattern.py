"""Guard: never let a responsive display utility have to OUT-RANK `hidden`.

`hidden md:flex` and `flex max-md:hidden` express the same intent, but they
fail differently.

`hidden md:flex` renders only if `.md\\:flex{display:flex}` wins the cascade
against `.hidden{display:none}`. Both are plain single-class selectors in
Tailwind's `utilities` layer, so the winner is decided by source order, and
that is fragile: anything that injects CSS into the page can flip it. On
2026-07-28 a browser extension did exactly that on the deployed dashboard.
Every element written this way vanished at once: the whole left sidebar
(app-shell), the desktop tables on Clusters and Fleet, the replication
topology column, the chat sidebar. The stylesheet was provably correct (the
identical CSS resolved right in an isolated iframe), so no amount of
rebuilding or cache clearing would have fixed it, and the product is
distributed for self-service `cdk deploy`, so "ask every operator to change
a browser setting" is not a fix.

`flex max-md:hidden` only ever declares `display:none` at the width where we
actually want it gone. Nothing has to win a fight, so an injected stylesheet
cannot erase the element.

This test is deliberately narrow: it flags only the combination that creates
the fight. A bare `hidden`, a bare `md:hidden`, and `hidden md:mt-4` are all
fine, because a responsive `display:none` winning is the outcome we want
anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

# Tailwind utilities that set `display`. `hidden` is the display:none one and is
# handled separately as the base class.
_DISPLAY_UTILITIES = (
    "block",
    "inline-block",
    "inline-flex",
    "inline-grid",
    "inline-table",
    "inline",
    "flex",
    "grid",
    "table",
    "contents",
    "flow-root",
    "list-item",
)
_BREAKPOINTS = ("sm", "md", "lg", "xl", "2xl")

# A responsive display utility, e.g. `md:flex`. Not `max-md:flex`: a max-*
# variant is the safe direction and is what the fix uses.
_RESPONSIVE_DISPLAY = re.compile(
    r"^(?:%s):(?:%s)$" % ("|".join(_BREAKPOINTS), "|".join(_DISPLAY_UTILITIES))
)

# One pass that consumes comments AND strings, so a comment can never be read
# as a class list. This matters: the explanatory comment on the sidebar quotes
# the bad pattern in backticks, and a string-only scan reported it as a real
# occurrence. A guard that counts its own documentation is the same defect the
# metric_filters census had.
_COMMENT_OR_STRING = re.compile(
    r"""
      (?P<block>/\*.*?\*/)              # /* ... */
    | (?P<line>//[^\n]*)                # // ...
    | (?P<quote>['"`])(?P<body>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)
    """,
    re.S | re.X,
)


def _class_strings(text: str) -> list[str]:
    """Every string literal in the source, with comments discarded."""
    out = []
    for m in _COMMENT_OR_STRING.finditer(text):
        if m.group("block") is not None or m.group("line") is not None:
            continue
        out.append(m.group("body"))
    return out


def _offending_tokens(text: str) -> list[str]:
    """Class strings that pair a bare `hidden` with a responsive display."""
    bad = []
    for body in _class_strings(text):
        tokens = body.split()
        # Exact token match: `overflow-hidden` must NOT count as `hidden`.
        if "hidden" not in tokens:
            continue
        responsive = [t for t in tokens if _RESPONSIVE_DISPLAY.match(t)]
        if responsive:
            bad.append(" ".join(tokens))
    return bad


def _tsx_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.tsx") if p.is_file())


def test_no_hidden_plus_responsive_display_in_frontend_sources():
    assert _SRC.is_dir(), f"frontend source tree not found at {_SRC}"
    files = _tsx_files()
    assert files, "no .tsx files scanned, the guard would pass vacuously"

    offenders: list[str] = []
    for path in files:
        for cls in _offending_tokens(path.read_text(encoding="utf-8")):
            rel = path.relative_to(_SRC.parents[1])
            offenders.append(f"{rel}: {cls!r}")

    assert not offenders, (
        "These class lists make a responsive display utility fight `hidden`, "
        "which an injected stylesheet can flip (it erased the sidebar once "
        "already). Invert them: `hidden md:flex` -> `flex max-md:hidden`, "
        "`hidden md:block` -> `block max-md:hidden`.\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_actually_detects_the_pattern():
    """Mutation check: the detector must fire on the shape it exists to catch,
    and must not fire on the shapes that are fine."""
    assert _offending_tokens('className="hidden md:flex w-60"') == [
        "hidden md:flex w-60"
    ]
    assert _offending_tokens('className="hidden lg:block"') == ["hidden lg:block"]

    # Safe shapes, none of these may be reported.
    assert _offending_tokens('className="flex max-md:hidden w-60"') == []
    assert _offending_tokens('className="grid md:hidden"') == []
    assert _offending_tokens('className="rounded overflow-hidden md:flex"') == []
    assert _offending_tokens('className="hidden md:mt-4"') == []
    assert _offending_tokens('className="hidden"') == []

    # A comment quoting the bad pattern is documentation, not an occurrence.
    # Both comment styles, and backticks inside them, must be ignored.
    assert _offending_tokens("{/* use `flex max-md:hidden`, not `hidden md:flex` */}") == []
    assert _offending_tokens("// never write `hidden md:flex` here\n") == []
    # ...but a real class list on the very next line still gets caught.
    assert _offending_tokens(
        '/* not `hidden md:flex` */\nclassName="hidden md:block"'
    ) == ["hidden md:block"]


def test_the_inverted_form_is_actually_used_somewhere():
    """The sidebar is the site the incident was reported against, so pin that
    it carries the safe form. Without this, deleting the fix and deleting the
    guard's subject would both pass."""
    shell = (_SRC / "components" / "app-shell.tsx").read_text(encoding="utf-8")
    assert "max-md:hidden" in shell, (
        "app-shell.tsx no longer uses the max-* form for the sidebar; "
        "reverting to `hidden md:flex` reintroduces the erasable sidebar"
    )
