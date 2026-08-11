from agent.work_tree_allowlist import load_allowlist


def test_load_allowlist_parses_names_and_skips_comments_and_blanks(tmp_path):
    p = tmp_path / "allow.txt"
    p.write_text(
        "# header comment\n"
        "\n"
        "alpha\n"
        "  beta  \n"          # surrounding whitespace stripped
        "gamma/\n"            # trailing slash tolerated
        "# another comment\n"
        "delta\n",
        encoding="utf-8",
    )
    assert load_allowlist(str(p)) == {"alpha", "beta", "gamma", "delta"}


def test_load_allowlist_seed_file_has_expected_dossiers():
    # The committed seed file must parse to exactly the approved workspace roots.
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo_root, "config", "work-tree-allowlist.txt")
    names = load_allowlist(path)
    assert names == {
        "apmt-work-intelligence", "archive",
        "browser-harness", "checkpoints", "context-system", "deliverables",
        "goals", "graphiti-pilot", "head-ade-application",
        "hermes-context-audits", "hermes-ops",
        "personal-admin-finance-vendors", "research", "scratch",
        "silent-structural-risk-rewrite",
    }


def test_load_allowlist_missing_file_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        load_allowlist(str(tmp_path / "does-not-exist.txt"))
