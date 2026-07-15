# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for _transform_gitignore_line directory-pattern anchoring.

A trailing-slash directory pattern (e.g. ``build/``) from a nested .gitignore
must remain unanchored (match anywhere under base_rel) instead of being scoped
directly to base_rel because the trailing ``/`` made it look like a path with
a separator.
"""

from openviking.parse.gitignore import _transform_gitignore_line


class TestTransformGitignoreDirAnchoring:
    def test_dir_only_pattern_stays_unanchored(self):
        """``build/`` must expand to ``src/**/build/`` (match anywhere), not ``src/build/``."""
        result = _transform_gitignore_line("build/", "src")
        assert result == "src/**/build/"

    def test_plain_pattern_without_slash_is_unanchored(self):
        """``build`` (no slash) matches anywhere under base_rel."""
        assert _transform_gitignore_line("build", "src") == "src/**/build"

    def test_leading_slash_pattern_is_anchored(self):
        """``/build`` is anchored directly to base_rel."""
        assert _transform_gitignore_line("/build", "src") == "src/build"

    def test_dir_only_with_leading_slash_stays_anchored(self):
        """``/build/`` stays anchored to base_rel but preserves the dir-only slash."""
        assert _transform_gitignore_line("/build/", "src") == "src/build/"

    def test_negated_dir_only_pattern_preserves_bang(self):
        """Negation prefix is preserved through the unanchored transform."""
        assert _transform_gitignore_line("!build/", "src") == "!src/**/build/"

    def test_nested_dir_pattern_with_internal_slash_stays_anchored(self):
        """``foo/bar/`` has an internal '/' so it should anchor to base_rel."""
        assert _transform_gitignore_line("foo/bar/", "src") == "src/foo/bar/"

    def test_multiple_trailing_slashes_collapsed(self):
        """``build///`` -- duplicated trailing slashes are collapsed to a single one."""
        assert _transform_gitignore_line("build///", "src") == "src/**/build/"

    def test_double_star_dir_only(self):
        """``**/build/`` (a globstar directory pattern) remains unanchored."""
        assert _transform_gitignore_line("**/build/", "src") == "src/**/build/"

    def test_empty_body_leading_slash_only(self):
        """``/`` (just a slash) means the base_rel directory itself."""
        assert _transform_gitignore_line("/", "src") == "src/"

    def test_leading_slash_dir_only_empty_body(self):
        """``//`` -- after stripping slashes the body is empty; anchors to base_rel."""
        assert _transform_gitignore_line("//", "src") == "src/"

    def test_deeply_nested_base_rel(self):
        """Pattern from a .gitignore at ``a/b/c/`` must prefix correctly."""
        assert _transform_gitignore_line("build/", "a/b/c") == "a/b/c/**/build/"

    def test_root_base_rel_no_transform(self):
        """Empty base_rel returns the line unchanged."""
        assert _transform_gitignore_line("build/", "") == "build/"

    def test_comment_line_passed_through(self):
        """Comment lines are returned as-is regardless of content."""
        assert _transform_gitignore_line("# build/", "src") == "# build/"

    def test_negated_anchored_dir_only(self):
        """``!/build/`` negates an anchored directory-only pattern."""
        assert _transform_gitignore_line("!/build/", "src") == "!src/build/"

    def test_trailing_spaces_stripped_per_gitignore_spec(self):
        """``build/   `` -- trailing spaces are ignored (gitignore spec),
        stripping them yields a clean unanchored directory-only pattern."""
        result = _transform_gitignore_line("build/   ", "src")
        assert result == "src/**/build/"

    def test_pattern_with_globstar_prefix(self):
        """``**/foo`` -- a globstar-prefixed non-dir pattern stays unanchored."""
        assert _transform_gitignore_line("**/foo", "src") == "src/**/foo"

    def test_negation_of_simple_pattern(self):
        """``!build`` (negation, no dir marker) should stay unanchored."""
        assert _transform_gitignore_line("!build", "src") == "!src/**/build"

    def test_whitespace_only_line_returns_empty(self):
        """A line with only spaces produces an empty (no-op) pattern."""
        assert _transform_gitignore_line("   ", "src") == ""
