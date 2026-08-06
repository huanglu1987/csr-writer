"""Stable paths for running the plugin in place or from an editable install."""

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PLUGIN_ROOT / "skills" / "csr-writer"
REFERENCES_DIR = SKILL_ROOT / "references"
ASSETS_DIR = SKILL_ROOT / "assets"

DEFAULT_STRUCTURE = REFERENCES_DIR / "ich-e3-structure.yaml"
DEFAULT_FORMAT_SPEC = REFERENCES_DIR / "format-spec.yaml"
DEFAULT_TITLE_OVERRIDES = REFERENCES_DIR / "title-overrides.yaml"
DEFAULT_PRODUCTION_METHODS = REFERENCES_DIR / "production-methods.yaml"
DEFAULT_METHOD_EXTRACTION_CONTRACTS = REFERENCES_DIR / "method-extraction-contracts.yaml"
DEFAULT_CONSTRAINED_NARRATION_GUARDS = (
    REFERENCES_DIR / "constrained-narration-guards.yaml"
)
DEFAULT_TEMPLATE = ASSETS_DIR / "csr-template-zh.docx"
