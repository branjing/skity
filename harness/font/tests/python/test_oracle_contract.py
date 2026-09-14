# Copyright 2021 The Lynx Authors. All rights reserved.
# Licensed under the Apache License Version 2.0 that can be found in the
# LICENSE file in the root directory of this source tree.

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools"))
from font_harness_runner import (font_fingerprints, sha256, validate_oracle,
                                 validate_payload, write_json)


class OracleContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "font.ttf").write_bytes(b"synthetic font input, not an oracle")
        self.case = {"schema_version": 1, "id": "font.synthetic", "backend": "freetype",
                     "category": "typeface_probe", "status": "skity_gap",
                     "font_files": [{"uri": "repo://font.ttf", "id": "fixture", "collection_index": 0}],
                     "typeface_request": {"entry": "MakeFromFile", "font_file": "fixture"},
                     "glyphs": {"chars": ["U+0041"]}}
        self.case_path = self.repo / "case.json"
        write_json(self.case_path, self.case)
        inputs = {}
        for key in ("runner_source", "runner_binary", "skia_library", "gn_args", "environment"):
            path = self.repo / key
            path.write_text("synthetic contract test")
            inputs[key] = {"path": str(path), "sha256": sha256(path)}
        self.oracle = {"schema_version": 1, "artifact_type": "font_probe_result", "runner": "skia",
                       "case_id": self.case["id"], "backend": "freetype", "ok": True,
                       "typeface_result": {"identity": {"family_name": "Fixture", "post_script_name": "Fixture",
                                             "style": {}, "units_per_em": 1000, "glyph_count": 2}},
                       "typeface_probe": {"table_count": 1, "tables": [{"tag": "test", "size": 1,
                                "full_copied_size": 1, "full_digest": "fnv1a64:example"}],
                                "glyphs": [{"char": "U+0041", "glyph_id": 1, "contains": True}]},
                       "provenance": {"case_sha256": sha256(self.case_path),
                                      "font_files": font_fingerprints(self.case, self.repo),
                                      "profile": "explicit", "skia_commit": "a" * 40, "inputs": inputs}}
        self.oracle_path = self.repo / "oracle.json"

    def check(self):
        write_json(self.oracle_path, self.oracle)
        return validate_oracle(self.case_path, self.oracle_path, self.repo, "explicit")

    def test_gap_oracle_can_be_ready_without_skity_actual(self):
        self.check()
        self.assertFalse((self.repo / "actual.json").exists())

    def test_missing_oracle_is_input_failure(self):
        with self.assertRaises(FileNotFoundError):
            validate_oracle(self.case_path, self.oracle_path, self.repo, "explicit")

    def test_stale_case_is_rejected(self):
        self.case_path.write_text(self.case_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "case hash"):
            self.check()

    def test_changed_font_is_rejected(self):
        (self.repo / "font.ttf").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "fonts"):
            self.check()

    def test_changed_runner_is_rejected(self):
        (self.repo / "runner_binary").write_text("changed")
        with self.assertRaisesRegex(ValueError, "runner_binary"):
            self.check()

    def test_wrong_runner_or_profile_is_rejected(self):
        for field, value in (("runner", "skity"), ("profile", "system")):
            with self.subTest(field=field):
                artifact = copy.deepcopy(self.oracle)
                if field == "runner":
                    self.oracle[field] = value
                else:
                    self.oracle["provenance"][field] = value
                with self.assertRaises(ValueError):
                    self.check()
                self.oracle = artifact

    def test_incomplete_glyphs_and_tables_are_rejected(self):
        for field in ("glyphs", "tables"):
            with self.subTest(field=field):
                artifact = copy.deepcopy(self.oracle)
                artifact["typeface_probe"][field] = []
                with self.assertRaises(ValueError):
                    validate_payload(self.case, artifact)

    def test_all_collection_faces_must_match_the_source_count(self):
        case = copy.deepcopy(self.case)
        case["typeface_request"]["collection_indices"] = "all"
        (self.repo / "font.ttf").write_bytes(b"ttcf\x00\x01\x00\x00\x00\x00\x00\x02")
        artifact = copy.deepcopy(self.oracle)
        face = artifact.pop("typeface_result")
        probe = artifact.pop("typeface_probe")
        face["collection_index"] = 0
        probe["collection_index"] = 0
        artifact.update(typeface_collection={"collection_count": 1, "indices": [0]},
                        typeface_results=[face], typeface_probes=[probe])
        with self.assertRaisesRegex(ValueError, "collection count differs from font input"):
            validate_payload(case, artifact, self.repo)

    def test_success_envelope_alone_is_not_an_oracle(self):
        artifact = {key: value for key, value in self.oracle.items()
                    if key not in ("typeface_result", "typeface_probe")}
        with self.assertRaises((TypeError, ValueError, KeyError)):
            validate_payload(self.case, artifact)


if __name__ == "__main__":
    unittest.main()