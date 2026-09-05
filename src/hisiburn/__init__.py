"""Open-source USB flashing for HiSilicon Hi3518EV300 cameras.

Replaces the Windows-only HiTool/HiBurn + Zadig/libusbK workflow with a
libusb-based tool that runs natively on macOS and Linux.
"""

from __future__ import annotations

__version__ = "0.1.0"


def installed_from() -> str | None:
    """Where this copy was installed from: a commit, a path, or nothing.

    `uv tool install git+...` and `pip install git+...` pin a commit and do
    not follow the branch afterwards, so an installed copy can be arbitrarily
    far behind without anything saying so -- and the version number alone
    cannot tell you, because it does not change between commits. Both
    installers record what they resolved in `direct_url.json` (PEP 610), so
    that is what gets reported.
    """
    from importlib.metadata import Distribution, PackageNotFoundError

    try:
        raw = Distribution.from_name(__name__).read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return None
    if not raw:
        return None

    import json

    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None

    commit = record.get("vcs_info", {}).get("commit_id")
    if commit:
        return f"git {commit[:12]}"
    if record.get("dir_info", {}).get("editable"):
        return f"editable {record.get('url', '')}"
    return None


def version_string() -> str:
    """What ``hisiburn --version`` prints."""
    origin = installed_from()
    return f"hisiburn {__version__}" + (f" ({origin})" if origin else "")
