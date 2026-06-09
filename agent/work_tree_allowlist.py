"""Shared parser for the work/-tree dossier allowlist.

A single parser so the placement watchdog (and any future write-guard) agree on
exactly which top-level dossiers are approved. The allowlist file lives in the
code repo (config/work-tree-allowlist.txt) so it can only change via a
human-authored commit. See docs/superpowers/specs/2026-06-08-work-tree-clutter-
root-cause-design.md."""


def load_allowlist(path: str) -> set[str]:
    """Parse the allowlist file into a set of approved top-level dossier names.

    Lines starting with '#' are comments; blank lines are ignored. Surrounding
    whitespace and a single trailing '/' are stripped from each name. Raises
    FileNotFoundError if the file does not exist (callers decide how to handle a
    missing allowlist — failing loudly beats silently treating everything as
    drift)."""
    names: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            names.add(line.rstrip("/"))
    return names
