"""Tests for the chat model picker: which Claude generations reach the dropdown.

The guarded regression is measured, not hypothetical. Until 2026-09-03 this filter was
a hardcoded allowlist of version substrings ("sonnet-4-5", "sonnet-4-6", "sonnet-4-7",
and the same for opus/haiku), duplicated across the filter, the label table and the
sort key. On that date the account had claude-opus-5, claude-sonnet-5, claude-opus-4-8
and claude-fable-5-1 all ACTIVE and verified callable, and every one of them was
invisible in the picker because the list stopped at 4-7. Nothing logged a reason: a new
model simply never appeared.

So these tests assert the FLOOR behaves numerically, and specifically that a model
newer than anything named in the code still passes.
"""

import importlib.util
import sys
from pathlib import Path

_MODELS_DIR = str(Path(__file__).resolve().parents[3] / "api" / "models")
if _MODELS_DIR not in sys.path:
    sys.path.insert(0, _MODELS_DIR)

_PATH = Path(__file__).resolve().parents[3] / "api" / "models" / "handler.py"
_spec = importlib.util.spec_from_file_location("models_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# Verified ACTIVE and callable in ap-northeast-2 on 2026-09-03, both streaming text
# and emitting a valid toolUse block.
LIVE_CURRENT = [
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-sonnet-5",
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-fable-5-1",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
]

# Also ACTIVE in the same account, and deliberately hidden.
LIVE_DEPRECATED = [
    "apac.anthropic.claude-3-5-sonnet-20240620-v1:0",
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "apac.anthropic.claude-3-haiku-20240307-v1:0",
    "apac.anthropic.claude-3-sonnet-20240229-v1:0",
    "apac.anthropic.claude-sonnet-4-20250514-v1:0",
]


def test_every_current_model_in_the_account_is_offered():
    hidden = [m for m in LIVE_CURRENT if not handler._is_latest(m)]
    assert hidden == [], f"current models filtered out of the picker: {hidden}"


def test_every_deprecated_model_in_the_account_is_hidden():
    leaked = [m for m in LIVE_DEPRECATED if handler._is_latest(m)]
    assert leaked == [], f"deprecated models leaked into the picker: {leaked}"


def test_a_release_date_is_not_read_as_a_minor_version():
    """The bare Sonnet 4 id carries a date, and the date must not become the minor.

    `claude-sonnet-4-20250514` would parse as 4.20250514 under a naive
    `(?:-(\\d+))?` minor group, which compares ABOVE the 4.5 floor and would let the
    oldest Sonnet 4 into the dropdown. This is the single most likely way to break
    the parser while "improving" the regex.
    """
    assert handler._parse_model("apac.anthropic.claude-sonnet-4-20250514-v1:0") == (
        "sonnet", (4, 0),
    )
    assert handler._is_latest("apac.anthropic.claude-sonnet-4-20250514-v1:0") is False
    # A real minor still parses, even with a date after it.
    assert handler._parse_model("global.anthropic.claude-haiku-4-5-20251001-v1:0") == (
        "haiku", (4, 5),
    )


def test_a_generation_newer_than_any_name_in_the_code_passes():
    """The whole point of the numeric floor: no source edit for the next release."""
    for future in ("global.anthropic.claude-sonnet-9",
                   "global.anthropic.claude-opus-12-3",
                   "global.anthropic.claude-haiku-6"):
        assert handler._is_latest(future), future


def test_legacy_family_after_version_form_is_parsed_not_ignored():
    """claude-3-5-sonnet puts the family AFTER the version. If the parser returns
    None for it the model is excluded by accident rather than by the floor, which
    happens to be the right answer today and the wrong reason."""
    assert handler._parse_model("apac.anthropic.claude-3-5-sonnet-20241022-v2:0") == (
        "sonnet", (3, 5),
    )
    assert handler._parse_model("apac.anthropic.claude-3-haiku-20240307-v1:0") == (
        "haiku", (3, 0),
    )


def test_labels_read_the_way_a_human_writes_the_version():
    assert handler._label("global.anthropic.claude-sonnet-5") == "Sonnet 5"
    assert handler._label("global.anthropic.claude-opus-4-8") == "Opus 4.8"
    assert handler._label("global.anthropic.claude-fable-5-1") == "Fable 5.1"
    assert handler._label("global.anthropic.claude-haiku-4-5-20251001-v1:0") == "Haiku 4.5"


def test_newer_version_sorts_before_older_within_a_family():
    """Opus 5 must outrank Opus 4.8. The old sort key only knew the strings 4.5/4.6/4.7,
    so "Opus 5" scored 0 and sorted BEHIND "Opus 4.7" (-47) under an ascending sort:
    the newest model appeared at the bottom of its group."""
    labels = ["Opus 4.8", "Opus 5", "Sonnet 4.6", "Sonnet 5", "Haiku 4.5", "Fable 5.1"]
    assert sorted(labels, key=handler._rank_by_label) == [
        "Opus 5", "Opus 4.8", "Sonnet 5", "Sonnet 4.6", "Haiku 4.5", "Fable 5.1",
    ]


def test_the_default_pick_is_the_newest_sonnet():
    """lambda_handler picks the first label containing "sonnet" from the sorted list,
    so the ranking above is what makes the default current. Asserted separately
    because a ranking regression would silently downgrade the default model."""
    labels = ["Opus 5", "Sonnet 4.6", "Sonnet 5", "Haiku 4.5"]
    ordered = sorted(labels, key=handler._rank_by_label)
    first_sonnet = next(m for m in ordered if "sonnet" in m.lower())
    assert first_sonnet == "Sonnet 5"


def test_an_unparseable_id_is_hidden_rather_than_offered():
    """Negative control: without this, a checker that returns True on everything
    would pass every test above."""
    for junk in ("", "amazon.nova-pro-v1:0", "meta.llama3-70b", "anthropic-claude"):
        assert handler._is_latest(junk) is False, junk


def test_the_frontend_fallback_list_matches_the_profiles_cdk_creates():
    """chat-panel.tsx hardcodes a fallback list for when /api/models is unreachable,
    and inference_profile_setup._BASE_MODELS is what /api/models actually serves.
    A comment says "keep in sync"; this is the part that enforces it.

    Drift here is invisible in normal use (the live list wins) and only shows up on a
    cold start or an IAM problem, which is exactly when a stale model id would produce
    an AccessDenied instead of a chat reply.
    """
    import ast
    import re

    root = Path(__file__).resolve().parents[3]

    setup_src = (root / "data-pipeline" / "inference_profile_setup" / "handler.py").read_text()
    tree = ast.parse(setup_src)
    base_models = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_BASE_MODELS" for t in node.targets
        ):
            base_models = ast.literal_eval(node.value)
    assert base_models, "_BASE_MODELS not found; did the setup handler move?"
    backend = {(base, label) for _short, base, label in base_models}

    tsx = (root / "frontend" / "src" / "components" / "chat" / "chat-panel.tsx").read_text()
    block = tsx.split("const FALLBACK_MODELS: ModelOption[] = [", 1)[1].split("];", 1)[0]
    frontend = set(
        re.findall(r'id:\s*"([^"]+)"[,\s]+label:\s*"([^"]+)"', block)
    )
    assert frontend, "FALLBACK_MODELS block did not parse; check the literal shape"

    assert frontend == backend, (
        "chat-panel.tsx FALLBACK_MODELS and _BASE_MODELS disagree.\n"
        f"  only in frontend: {sorted(frontend - backend)}\n"
        f"  only in backend:  {sorted(backend - frontend)}"
    )


def test_the_default_model_is_one_the_picker_can_offer():
    """Settings.AGENT_MODEL_ID, the agent's own fallback, and the frontend default all
    have to name a model that survives _is_latest, or the default is a model the user
    cannot see or re-select after switching away from it."""
    import re

    root = Path(__file__).resolve().parents[3]

    tsx = (root / "frontend" / "src" / "components" / "chat" / "chat-panel.tsx").read_text()
    fe_default = re.search(r'const DEFAULT_MODEL = "([^"]+)"', tsx).group(1)
    assert handler._is_latest(fe_default), f"frontend DEFAULT_MODEL is filtered out: {fe_default}"

    server = (root / "agent" / "server.py").read_text()
    agent_default = re.search(
        r'MODEL_ID = os\.environ\.get\("AGENT_MODEL_ID", "([^"]+)"\)', server
    ).group(1)
    assert handler._is_latest(agent_default), f"agent MODEL_ID fallback is filtered out: {agent_default}"

    example = (root / "cdk" / "config" / "settings.example.py").read_text()
    cfg_default = re.search(r'AGENT_MODEL_ID = "([^"]+)"', example).group(1)
    assert handler._is_latest(cfg_default), f"settings.example AGENT_MODEL_ID is filtered out: {cfg_default}"
