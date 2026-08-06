"""Typed failures shared by the M1 through M3 command-line tools."""


class CSRWriterError(Exception):
    """Base class for expected, user-actionable pipeline failures."""


class ConfigurationError(CSRWriterError):
    """Configuration or schema input is invalid."""


class StructureError(CSRWriterError):
    """The ICH E3 chapter structure violates a frozen invariant."""


class TemplateError(CSRWriterError):
    """The DOCX master does not satisfy the renderer contract."""


class RTFParseError(CSRWriterError):
    """The supported SAS ODS RTF adapter cannot parse the input safely."""


class SemanticMappingError(CSRWriterError):
    """A physical TFL cell cannot be mapped to a unique reviewed semantic key."""


class DatapackError(CSRWriterError):
    """The datapack contract is invalid or would lose information."""


class GuardError(CSRWriterError):
    """The numeric guard pipeline cannot produce a releasable result."""
