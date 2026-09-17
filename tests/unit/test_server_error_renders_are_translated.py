"""A server-authored error string has to be looked up where it is RENDERED.

`api/` handlers write their caller-safe rejection text in Korean, `api-client.ts`
copies the response body's `error` field verbatim into the thrown message, and
the page stores that message in state and prints it. `translate()` is reached
only through `t()`, so a render site that prints the message raw leaves every
`en-server.ts` key for that string unreachable, and an English operator reads
Korean. That was the shipped state until 2026-09-17, and reverting the
one-character fix was measured to leave tsc, the build and both node gates green.

WHY AN INVENTORY AND NOT A GATE. The general rule would have to trace a runtime
string from a Lambda response through a thrown Error, through component state,
to a JSX node. No regex in `tools/i18n-check.mjs` can do that, and every
approximation tried fired on correct code, which is how a gate gets deleted. So
the two ends are pinned by name instead: the api-client functions that copy the
body, and the render sites that print what they threw. Adding either one is
deliberate, and adding it means updating this list.
"""

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# Every render site that prints a message thrown by one of the copying
# functions below. The exact JSX, so unwrapping it fails this test.
_WRAPPED_RENDERS = {
    "src/app/settings/page.tsx": ["{t(error)}", "{t(saveError)}"],
    "src/app/admin/users/page.tsx": ["{t(error)}"],
    "src/app/approval-policies/page.tsx": ["{t(error)}"],
}

# How many api-client functions copy the server's `error` field verbatim. The
# count is the tripwire: a fifth one needs a render site on the list above.
_COPY_SITES = 4


def test_every_known_server_error_render_is_translated():
    for rel, renders in _WRAPPED_RENDERS.items():
        src = (_FRONTEND / rel).read_text(encoding="utf-8")
        for jsx in renders:
            assert jsx in src, f"{rel} no longer wraps its server error: {jsx}"
        # And no unwrapped twin of the same value.
        for jsx in renders:
            bare = jsx.replace("{t(", "{").replace(")}", "}")
            assert bare not in src.replace(jsx, ""), (
                f"{rel} still renders {bare} raw somewhere, which would show "
                "Korean to an English operator"
            )


def test_the_number_of_server_error_copy_sites_has_not_grown():
    src = (_FRONTEND / "src/lib/api-client.ts").read_text(encoding="utf-8")
    found = len(re.findall(r"if \(b\?\.error\) msg = b\.error;", src))
    assert found == _COPY_SITES, (
        f"api-client.ts copies the server's error field at {found} site(s), "
        f"expected {_COPY_SITES}. A new one means a new page can print "
        "server-authored Korean: add its render site to _WRAPPED_RENDERS and "
        "wrap it in t()."
    )
