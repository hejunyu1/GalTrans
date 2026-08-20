from __future__ import annotations

from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.translation import (
    TranslationProposal,
    TranslationTask,
    ValidatedTranslation,
    validate_translation_proposal,
)


def validate_renpy_translation_proposal(
    task: TranslationTask,
    proposal: TranslationProposal,
) -> ValidatedTranslation:
    """Validate a proposal with Ren'Py's deterministic protected-token scanner."""
    return validate_translation_proposal(
        task,
        proposal,
        expected_engine="renpy",
        protected_token_finder=find_renpy_protected_tokens,
    )
