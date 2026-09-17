"""The mirror of the i18n orphan check, run where the source strings live.

`frontend/src/lib/messages/en-server.ts` is the English for prose the BACKEND
wrote, keyed by the Korean source literal. Lookup is exact equality
(`EN[ko] ?? ko`), so the moment a handler reflows one of those sentences the key
stops matching, the lookup misses, and the English UI silently renders Korean
again. Nothing in the frontend can catch that: `tools/i18n-check.mjs` asserts a
key still appears under `frontend/src/`, and these keys never do, which is
exactly why that file is excluded there and checked here instead.

So this test asserts the one direction that rots: every key in en-server.ts is
still a verbatim Python string literal somewhere under `api/`,
`data-pipeline/` or `mcp-servers/`.

Two details that a naive grep gets wrong, and this does not:

  * Python folds implicit concatenation, so `("a" "b")` is ONE literal whose
    value is "ab". Almost every long message in this repo is written that way,
    and a grep for the whole sentence finds nothing. We compare AST constant
    VALUES, which are folded the same way the key was extracted.
  * f-strings are deliberately NOT collected. An interpolated string can never
    be a key, so accepting an f-string prefix as a match would let an
    unreachable key pass.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = ROOT / "frontend/src/lib/messages/en-server.ts"
BACKEND_DIRS = ("api", "data-pipeline", "mcp-servers")

# Same key shapes prettier produces as the .mjs parser handles: a double-quoted
# key, a single-quoted one (used when the key itself holds a double quote), or a
# bare one (Korean is a valid JS identifier, so a single word is written bare).
_KEY_RE = re.compile(
    r"""^\s+(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s:"']+)):""",
    re.M,
)
_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _en_server_keys() -> list[str]:
    raw = MESSAGES.read_text(encoding="utf-8")
    raw = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", raw))
    out = []
    for m in _KEY_RE.finditer(raw):
        dq, sq, bare = m.groups()
        if dq is not None:
            out.append(dq.replace('\\"', '"'))
        elif sq is not None:
            out.append(sq.replace("\\'", "'"))
        else:
            out.append(bare)
    return out


def _backend_string_literals() -> set[str]:
    found: set[str] = set()
    for d in BACKEND_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    found.add(node.value)
    return found


def test_every_en_server_key_still_exists_in_the_backend():
    keys = _en_server_keys()
    assert len(keys) > 200, f"parsed only {len(keys)} keys out of en-server.ts"
    literals = _backend_string_literals()
    missing = [k for k in keys if k not in literals]
    assert not missing, (
        f"{len(missing)} en-server.ts key(s) no longer appear verbatim in the "
        "backend, so they translate nothing and the English UI shows Korean. "
        "Update the key to the new source text (or drop it if the string is "
        f"gone):\n  " + "\n  ".join(repr(k) for k in missing)
    )


def test_no_duplicate_keys_in_en_server():
    keys = _en_server_keys()
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate key(s) in en-server.ts: {dupes}"


def test_keys_are_not_interpolated():
    """A key with a `{...}` placeholder cannot be produced by an f-string-free
    literal, so it would never be looked up. Catch it here rather than shipping
    a dead entry."""
    bad = [k for k in _en_server_keys() if re.search(r"\{[a-z_]+\}", k)]
    assert not bad, f"interpolated key(s) in en-server.ts, which never match: {bad}"


# ---------------------------------------------------------------------------
# The two wiring facts that no other gate can see.
#
# Both fail SILENTLY: the lookup misses, `translate()` returns the key, and the
# English UI renders Korean. tsc, the build and i18n-check all stay green,
# because a missing translation is not a type error and every key is still
# "used".
# ---------------------------------------------------------------------------

_FRONTEND = ROOT / "frontend" / "src"


def test_en_ts_spreads_en_server():
    """Drop the spread and all 238 server keys go dead at once, with no other
    symptom anywhere in the toolchain."""
    en = (_FRONTEND / "lib/messages/en.ts").read_text(encoding="utf-8")
    assert 'from "./en-server"' in en, "en.ts no longer imports the server table"
    export = re.search(
        r"export const EN\s*:\s*Record<string,\s*string>\s*=\s*\{([^}]*)\}", en
    )
    assert export, "en.ts no longer exports EN as a spread of the two halves"
    assert "...EN_SERVER" in export.group(1), (
        "EN no longer spreads EN_SERVER, so every server-authored key resolves "
        "to nothing and the English UI falls back to Korean"
    )


def test_api_client_translates_server_error_bodies():
    """`api-client.ts` is the single place a handler's `error` / `detail` /
    `message` becomes the thrown `Error.message` that panels render unwrapped.
    Every one of those lifts has to go through `tr()`, or that panel shows the
    server's Korean to an English operator."""
    src = (_FRONTEND / "lib/api-client.ts").read_text(encoding="utf-8")
    unwrapped = []
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("//", 1)[0]
        # A server body field (`e.error`, `e?.detail`, `parsed.message`,
        # `body.error`) flowing into the message the caller will render.
        if not re.search(r"\b(?:e|body|parsed|j)\??\.(?:error|detail|message)\b", code):
            continue
        if re.search(r"^\s*(?:let|const)?\s*\w*\s*(?:msg|detail)?\s*[:=]?\s*$", code):
            continue
        if "msg =" in code or "throw new Error" in code or "detail =" in code:
            if "tr(" not in code:
                unwrapped.append(f"api-client.ts:{i}: {code.strip()}")
    assert not unwrapped, (
        "server prose is lifted into a thrown Error without tr(), so it renders "
        "Korean on an English UI:\n  " + "\n  ".join(unwrapped)
    )
