# Copyright 2021 The Lynx Authors. All rights reserved.
# Licensed under the Apache License Version 2.0 that can be found in the
# LICENSE file in the root directory of this source tree.

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools"))
from font_harness_runner import (font_fingerprints, fontconfig_fingerprint, validate_fontconfig_environment, sha256, validate_oracle,
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

    def fontconfig_oracle(self):
        self.case["backend"] = "fontconfig"
        self.oracle["backend"] = "fontconfig"
        write_json(self.case_path, self.case)
        config = self.repo / "fonts.conf"
        config.write_text("<fontconfig/>")
        inventory = {"version": 21701, "files": [str(self.repo / "font.ttf")],
                     "config_files": [str(config)]}
        self.oracle["fontconfig_inventory"] = inventory
        provenance = self.oracle["provenance"]
        provenance.update(profile="controlled", fontconfig_fixture="controlled",
                          case_sha256=sha256(self.case_path),
                          fontconfig_environment=fontconfig_fingerprint(inventory))
        provenance["inputs"]["controlled_font_0"] = {"path": str(self.repo / "font.ttf"),
                                                      "sha256": sha256(self.repo / "font.ttf")}

    def test_fontconfig_configuration_change_invalidates_oracle(self):
        self.fontconfig_oracle()
        (self.repo / "fonts.conf").write_text("<fontconfig><!-- changed --></fontconfig>")
        write_json(self.oracle_path, self.oracle)
        with self.assertRaisesRegex(ValueError, "Fontconfig environment differs"):
            validate_oracle(self.case_path, self.oracle_path, self.repo, "controlled")

    def test_fontconfig_rejects_unlisted_font(self):
        self.fontconfig_oracle()
        extra = self.repo / "extra.ttf"
        extra.write_bytes(b"extra font")
        self.oracle["fontconfig_inventory"]["files"].append(str(extra))
        self.oracle["provenance"]["fontconfig_environment"] = fontconfig_fingerprint(
            self.oracle["fontconfig_inventory"])
        write_json(self.oracle_path, self.oracle)
        with self.assertRaisesRegex(ValueError, "non-controlled fonts"):
            validate_oracle(self.case_path, self.oracle_path, self.repo, "controlled")

    def test_fontconfig_comparison_rejects_other_environment(self):
        self.fontconfig_oracle()
        expected = self.oracle["provenance"]["fontconfig_environment"]
        for key, value in (("version", 21301), ("files", []), ("config_files", [])):
            actual = dict(expected, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_fontconfig_environment(expected, actual)


    def test_fontconfig_profile_cannot_be_relabelled(self):
        self.fontconfig_oracle()
        self.oracle["provenance"]["profile"] = "system"
        write_json(self.oracle_path, self.oracle)
        with self.assertRaisesRegex(ValueError, "case Fontconfig profile mismatch"):
            validate_oracle(self.case_path, self.oracle_path, self.repo, "system")

    def test_system_coverage_may_be_absent_but_inventory_must_be_complete(self):
        case = {"id": "font.synthetic.system", "backend": "fontconfig",
                "category": "font_manager", "fontconfig_profile": "system",
                "font_manager_request": {"entry": "MatchFamilyStyleCharacter", "character": "U+4E00"}}
        artifact = {"schema_version": 1, "artifact_type": "font_probe_result",
                    "case_id": case["id"], "backend": "fontconfig", "ok": True,
                    "font_manager_probe": {
                        "operation": {"entry": "MatchFamilyStyleCharacter"},
                        "request_input": case["font_manager_request"],
                        "matched_typefaces": [{"available": False}],
                        "font_manager": {"family_count": 1, "family_names": ["Latin Only"]}}}
        validate_payload(case, artifact)
        case["font_manager_expectation"] = {"matched": True}
        with self.assertRaisesRegex(ValueError, "match violates expectation"):
            validate_payload(case, artifact)
        case.pop("font_manager_expectation")
        artifact["font_manager_probe"]["font_manager"]["family_names"] = []
        with self.assertRaisesRegex(ValueError, "family inventory count mismatch"):
            validate_payload(case, artifact)

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