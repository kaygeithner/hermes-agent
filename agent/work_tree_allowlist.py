"""Shared parser for the work/-tree dossier allowlist.

A single parser so the placement watchdog (and any future write-guard) agree on
exactly which top-level dossiers are approved. The allowlist file lives in the
code repo (config/work-tree-allowlist.txt) so it can only change via a
human-authored commit. See docs/superpowers/specs/2026-06-08-work-tree-clutter-
root-cause-design.md.

Intentionally has no in-repo runtime caller: the consumer is the runtime
``kay-context-hygiene`` placement-watchdog skill deployed on the VPS, which
imports this module from the deployed code repo (``~/.hermes/hermes-agent/``)
rather than living in git. The in-repo ``file_safety`` write-guard that would
also use it is the deferred Approach-B / Phase-5 of the design doc. The unit
test in ``tests/agent/test_work_tree_allowlist.py`` provides regression coverage
for the parser and the committed seed file in the meantime."""


def load_allowlist(path: str) -> set[str]:
    """Parse the allowlist file into a set of approved top-level dossier names.

    Lines starting with '#' are comments; blank lines are ignored. Surrounding
    whitespace and any trailing '/' are stripped from each name. A line that is
    empty after stripping (e.g. a stray '/' or '///') is skipped so it can't
    inject an empty-string member into the approved set. Raises FileNotFoundError
    if the file does not exist (callers decide how to handle a missing allowlist
    — failing loudly beats silently treating everything as drift)."""
    names: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name = line.rstrip("/")
            if name:
                names.add(name)
    return names
