# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Utilities for building sanitized temp paths from local file names."""

import re
from pathlib import PurePosixPath

_ALLOWED_SEGMENT_RE = re.compile(r"[^A-Za-z0-9!\-_.\*'()]")


class TempPathBuilder:
    """Build sanitized, deduplicated temp paths for parser outputs."""

    @staticmethod
    def sanitize_name_segment(segment: str) -> str:
        sanitized = _ALLOWED_SEGMENT_RE.sub("_", segment)
        return sanitized or "_"

    @classmethod
    def sanitize_rel_path(cls, rel_path: str) -> str:
        normalized = rel_path.replace("\\", "/")
        parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".", "..")]
        if not parts:
            return cls.sanitize_name_segment(normalized)
        return "/".join(cls.sanitize_name_segment(part) for part in parts)

    @classmethod
    def dedupe_name(cls, name: str, used_names: set[str], *, is_dir: bool) -> str:
        if is_dir:
            base_name = cls.sanitize_name_segment(name)
            candidate = base_name
            index = 1
            while candidate in used_names:
                candidate = f"{base_name}_{index}"
                index += 1
            used_names.add(candidate)
            return candidate

        path = PurePosixPath(name)
        suffixes = "".join(path.suffixes)
        stem = name[: -len(suffixes)] if suffixes else name
        safe_stem = cls.sanitize_name_segment(stem)
        safe_suffix = "".join(cls.sanitize_name_segment(suffix) for suffix in path.suffixes)
        candidate = f"{safe_stem}{safe_suffix}"
        index = 1
        while candidate in used_names:
            candidate = f"{safe_stem}_{index}{safe_suffix}"
            index += 1
        used_names.add(candidate)
        return candidate

    @classmethod
    def build_rel_path_mapping(
        cls,
        rel_paths: list[str],
        *,
        preserve_structure: bool,
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        dir_mapping: dict[str, str] = {"": ""}
        used_names_by_parent: dict[str, set[str]] = {}

        for rel_path in rel_paths:
            normalized = rel_path.replace("\\", "/")
            parts = [
                part for part in PurePosixPath(normalized).parts if part not in ("", ".", "..")
            ]
            if not parts:
                continue

            raw_parent = ""
            sanitized_parent = ""
            if preserve_structure:
                for part in parts[:-1]:
                    raw_parent = f"{raw_parent}/{part}".strip("/")
                    if raw_parent in dir_mapping:
                        sanitized_parent = dir_mapping[raw_parent]
                        continue
                    used = used_names_by_parent.setdefault(sanitized_parent, set())
                    safe_dir = cls.dedupe_name(part, used, is_dir=True)
                    sanitized_parent = f"{sanitized_parent}/{safe_dir}".strip("/")
                    dir_mapping[raw_parent] = sanitized_parent

            used = used_names_by_parent.setdefault(sanitized_parent, set())
            file_name = cls.dedupe_name(parts[-1], used, is_dir=False)
            mapping[rel_path] = f"{sanitized_parent}/{file_name}".strip("/")

        return mapping
