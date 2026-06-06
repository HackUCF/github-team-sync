"""Small helpers shared across the package."""

_TRUE = {"y", "yes", "t", "true", "on", "1"}
_FALSE = {"n", "no", "f", "false", "off", "0"}


def strtobool(value: str) -> int:
    """Convert a truthy/falsy string to ``1`` or ``0``.

    Drop-in replacement for ``distutils.util.strtobool``, which was removed
    from the standard library in Python 3.12. Raises ``ValueError`` for
    unrecognised values, matching the original behaviour.
    """
    val = value.strip().lower()
    if val in _TRUE:
        return 1
    if val in _FALSE:
        return 0
    raise ValueError(f"invalid truth value {value!r}")
