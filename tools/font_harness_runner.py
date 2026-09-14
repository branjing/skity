# Copyright 2021 The Lynx Authors. All rights reserved.
# Licensed under the Apache License Version 2.0 that can be found in the
# LICENSE file in the root directory of this source tree.

"""Font harness adapter for tools/test-runner.py; never invokes a Skia runner."""

import hashlib
import json
import os
import math
import re
from pathlib import Path
import subprocess
import sys
import time


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def font_fingerprints(case, repo):
    result = []
    for font in case.get("font_files", []):
        uri = font["uri"]
        if not uri.startswith("repo://"):
            raise ValueError("font URI must start with repo://")
        path = (Path(repo) / uri[7:]).resolve()
        if not path.is_relative_to(Path(repo).resolve()):
            raise ValueError("font URI escapes repository")
        result.append({"uri": uri, "collection_index": font.get("collection_index", 0),
                       "sha256": sha256(path)})
    return result


def require(value, condition, message):
    if not condition:
        raise ValueError(message)
    return value


def checked_array(value, size, label):
    require(value, isinstance(value, list) and len(value) == size, label + " count mismatch")
    return value


def validate_payload(case, artifact, repo=None):
    """Reject incomplete successful artifacts before subset comparisons can hide omissions."""
    for field, expected in (("schema_version", 1), ("artifact_type", "font_probe_result"),
                            ("case_id", case["id"]), ("backend", case["backend"]), ("ok", True)):
        require(artifact, artifact.get(field) == expected, "artifact " + field + " mismatch")
    chars = case.get("glyphs", {}).get("chars", [])
    glyph_ids = case.get("glyphs", {}).get("glyph_ids", [])
    count = len(chars) + len(glyph_ids)
    category = case["category"]
    if category in ("typeface_probe", "variation", "font_tables"):
        faces = artifact.get("typeface_results", [artifact.get("typeface_result")])
        probes = artifact.get("typeface_probes", [artifact.get("typeface_probe")])
        require(faces, isinstance(faces, list) and len(faces) > 0, "missing typefaces")
        checked_array(probes, len(faces), "typeface_probes")
        indices = case["typeface_request"].get("collection_indices")
        if indices == "all":
            collection = artifact["typeface_collection"]
            size = collection["collection_count"]
            require(collection, isinstance(size, int) and size > 0, "invalid collection count")
            checked_array(faces, size, "collection faces")
            require(collection, collection["indices"] == list(range(size)), "collection indices incomplete")
            for index, (face, probe) in enumerate(zip(faces, probes)):
                require(face, face["collection_index"] == index and probe["collection_index"] == index,
                        "collection face index mismatch")
            if repo is not None:
                font = next(f for f in case["font_files"] if f["id"] == case["typeface_request"]["font_file"])
                with (Path(repo) / font["uri"][7:]).open("rb") as stream:
                    header = stream.read(12)
                expected_count = int.from_bytes(header[8:12], "big") if header[:4] == b"ttcf" else 1
                require(collection, size == expected_count, "collection count differs from font input")
        for face, probe in zip(faces, probes):
            identity = face["identity"]
            for field in ("family_name", "post_script_name", "style", "units_per_em", "glyph_count"):
                require(identity, field in identity, "identity missing " + field)
            require(probe, probe["table_count"] > 0, "missing font tables")
            tables = checked_array(probe["tables"], probe["table_count"], "tables")
            for table in tables:
                require(table, table["size"] > 0 and table["full_copied_size"] == table["size"]
                        and bool(table.get("full_digest")), "incomplete table " + table["tag"])
            glyphs = checked_array(probe["glyphs"], len(chars), "glyph mapping")
            for item, char in zip(glyphs, chars):
                require(item, item["char"] == char and "glyph_id" in item and "contains" in item,
                        "glyph mapping does not match request")
            if case["typeface_request"]["entry"] == "MakeVariation":
                require(probe, bool(probe.get("variation_axes")) and bool(probe.get("variation_position")),
                        "missing variable font axes or position")
    elif category in ("font_metrics", "glyph_metrics", "scaler_context", "glyph_path", "glyph_image"):
        key = ("metrics_probe" if category in ("font_metrics", "glyph_metrics", "scaler_context")
               else category + "_probe")
        probe = artifact[key]
        requests = checked_array(probe["glyph_requests"], count, "glyph requests")
        labels = chars + ["gid:" + str(glyph) for glyph in glyph_ids]
        require(requests, [item["label"] for item in requests] == labels, "glyph requests differ from case")
        source = probe["typeface_source"]
        if "typeface_request" in case:
            require(source, source["entry"] == case["typeface_request"]["entry"] and
                    source["font_file_id"] == case["typeface_request"]["font_file"], "source mismatch")
            font = next(f for f in case["font_files"] if f["id"] == source["font_file_id"])
            require(source, source["collection_index"] == font.get("collection_index", 0), "collection index mismatch")
        branches = ("font_result",) if category == "glyph_image" else ("font_result", "scaler_context_result")
        for branch in branches:
            result = probe[branch]
            require(result, result.get("available", True) is True, "scaler unavailable")
            if key == "metrics_probe":
                for field in ("top", "ascent", "descent", "bottom", "leading", "x_height", "cap_height", "avg_char_width", "max_char_width", "x_min", "x_max", "underline_thickness", "underline_position", "strikeout_thickness", "strikeout_position"):
                    metric = result["font_metrics"].get(field)
                    require(result, isinstance(metric, (int, float)) and math.isfinite(metric), "missing or invalid metric " + field)
                values = checked_array(result["glyph_metrics"], count, "glyph metrics")
                for item in values:
                    for field in ("glyph_id", "advance_x", "advance_y", "left", "top", "width", "height"):
                        require(item, field in item["glyph_data"], "missing glyph metric " + field)
            elif category == "glyph_path":
                for item in checked_array(result["glyph_paths"], count, "glyph paths"):
                    require(item, item["path_finite"] is True and "verbs" in item["path"], "invalid path")
                    if "empty" in case.get("path_expectation", {}):
                        require(item, item["path_empty"] == case["path_expectation"]["empty"], "path violates empty expectation")
            else:
                for item in checked_array(result["glyph_images"], count, "glyph images"):
                    image = item["image"]
                    for field in ("format", "width", "height", "origin_x", "origin_y", "byte_size", "has_buffer"):
                        require(image, field in image, "missing image " + field)
                    if image["byte_size"]:
                        require(image, image["has_buffer"] and bool(image.get("digest")), "missing pixels")
                        if case["backend"] == "freetype":
                            require(image, len(bytes.fromhex(image["pixels_hex"])) == image["byte_size"], "incomplete pixel capture")
    elif category in ("font_manager", "family_style_set"):
        probe = artifact["font_manager_probe"]
        require(probe, probe["operation"]["entry"] == case["font_manager_request"]["entry"], "operation mismatch")
        matches = probe["matched_typefaces"]
        require(matches, isinstance(matches, list), "missing match result")
        expectation = case.get("font_manager_expectation", {})
        operation = probe["operation"]
        entry = operation["entry"]
        if entry in ("MatchFamily", "CreateStyleSet"):
            style_set = operation["style_set" if entry == "MatchFamily" else "create_style_set"]
            require(style_set, style_set["match_style"]["typeface"]["available"] == (style_set["style_count"] > 0),
                    "style set returned a face inconsistent with its count")
            expected_count = expectation.get("style_count")
            require(style_set, style_set["style_count"] == expected_count if expected_count is not None
                    else style_set["style_count"] > 0, "style count violates expectation")
            if expected_count is not None:
                checked_array(style_set["styles"], expected_count, "style inventory")
                for style in style_set["styles"]:
                    require(style, style["create_typeface"]["available"] and bool(style["style"]), "incomplete style face")
        else:
            checked_array(matches, 1, "matched typefaces")
            require(matches, matches[0]["available"] == expectation.get("matched", True), "match violates expectation")
        if "inventory_count" in expectation:
            inventory = probe["font_manager"]
            count = expectation["inventory_count"]
            names = checked_array(inventory["family_names"], count, "family inventory")
            require(inventory, inventory["family_count"] == count and len(set(names)) == count, "incomplete inventory")
        require(probe, probe.get("request_input") == case["font_manager_request"], "request input was not preserved")
        for face in matches:
            require(face, "available" in face, "missing match availability")
            if face["available"]:
                require(face, bool(face["identity"]["family_name"]), "missing match identity")
                summary = face["probe_summary"]
                if case["font_manager_request"]["entry"] == "MatchFamilyStyleCharacter":
                    char = case["font_manager_request"]["character"]
                    mapping = [g for g in summary["glyphs"] if g["char"] == char]
                    require(mapping, len(mapping) == 1 and mapping[0]["glyph_id"] != 0, "fallback lacks requested glyph")
    else:
        raise ValueError("unimplemented artifact category: " + category)


def validate_oracle(case_path, artifact_path, repo, profile):
    case = read_json(case_path)
    artifact = read_json(artifact_path)
    require(artifact, artifact.get("runner") == "skia", "oracle runner must be skia")
    validate_payload(case, artifact, repo)
    provenance = artifact["provenance"]
    require(provenance, provenance["case_sha256"] == sha256(case_path), "stale oracle: case hash")
    require(provenance, provenance["font_files"] == font_fingerprints(case, repo), "stale oracle: fonts")
    require(provenance, provenance["profile"] == profile, "oracle profile mismatch")
    require(provenance, len(provenance["skia_commit"]) == 40, "missing Skia pin")
    inputs = provenance["inputs"]
    for key in ("runner_source", "runner_binary", "skia_library", "gn_args", "environment"):
        item = inputs[key]
        require(item, sha256(item["path"]) == item["sha256"], "stale oracle: " + key)
    for key, item in inputs.items():
        require(item, sha256(item["path"]) == item["sha256"], "stale oracle: " + key)
    if profile == "controlled":
        files = {Path(name).resolve() for name in artifact["fontconfig_inventory"]["files"]}
        allowed = {Path(item["path"]).resolve() for key, item in inputs.items() if key.startswith("controlled_font_")}
        empty = case.get("fontconfig_fixture") == "empty"
        require(provenance, provenance.get("fontconfig_fixture", "controlled") == case.get("fontconfig_fixture", "controlled"), "Fontconfig fixture mismatch")
        require(files, (bool(allowed) or empty) and files == allowed, "oracle includes non-controlled fonts")
    return artifact


def image_differences(expected, actual):
    result = []
    expected_images = expected["glyph_image_probe"]["font_result"]["glyph_images"]
    actual_images = actual["glyph_image_probe"]["font_result"]["glyph_images"]
    for left, right in zip(expected_images, actual_images):
        e, a = left["image"], right["image"]
        eb, ab = bytes.fromhex(e.get("pixels_hex", "")), bytes.fromhex(a.get("pixels_hex", ""))
        channels = 1 if e["format"] == "gray8" else 4
        width = int(e["width"])
        differences = [{"byte_index": i, "x": (i // channels) % width,
                        "y": i // (channels * width), "channel": i % channels,
                        "expected": eb[i], "actual": ab[i]}
                       for i in range(min(len(eb), len(ab))) if eb[i] != ab[i]]
        result.append({"label": left["label"], "expected_size": len(eb), "actual_size": len(ab),
                       "expected_format": e["format"], "actual_format": a["format"],
                       "changed_bytes": len(differences), "differences": differences})
    return result


def add_arguments(parser):
    parser.add_argument("--font-action", default="run", choices=["cli-smoke", "case-info", "oracle-check", "probe", "compare", "run"])
    parser.add_argument("--font-case")
    parser.add_argument("--font-manifest")
    parser.add_argument("--font-backend", default="freetype")
    parser.add_argument("--font-oracle-dir")
    parser.add_argument("--font-reference-oracle-dir", help="Check repeat Skia oracle payloads for deterministic output")
    parser.add_argument("--font-artifact-root")
    parser.add_argument("--font-profile", default="explicit", choices=["explicit", "controlled", "system"])


def run_suite(runner, args):
    repo = Path(runner.repo_root)
    output = Path(args.font_artifact_root or repo / "local/font-harness/artifacts" /
                  ("linux-" + args.font_backend) / time.strftime("%Y%m%d-%H%M%S" )).resolve()
    output.mkdir(parents=True, exist_ok=True)
    executable = runner._find_named_test_executable("skity-font")
    if not executable:
        return runner._create_infra_error("missing_executable", "Build skity-font with SKITY_ENABLE_FONT_HARNESS=ON")
    executable = str(Path(executable).resolve())
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["SKITY_FONT_HARNESS_TEST_BINARY"] = executable
    records = []
    sequence = 0

    def invoke(command, report_path=None):
        nonlocal sequence
        sequence += 1
        if report_path:
            Path(report_path).unlink(missing_ok=True)
        result = subprocess.run([str(x) for x in command], cwd=repo, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        log = output / "logs" / (f"{sequence:04d}.json")
        write_json(log, {"command": [str(x) for x in command], "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        payload = read_json(report_path) if report_path and Path(report_path).is_file() else {}
        return result.returncode, payload, log

    def record(name, code, reason, kind, artifacts, error=""):
        records.append({"suite": "font-harness", "case_name": name, "status": "PASS" if code == 0 else "FAIL",
                        "exit_code": 0 if code == 0 else (1 if code in (1, 7) else 2),
                        "child_exit_code": code, "reason_code": reason, "validation_kind": kind,
                        "backend": args.font_backend, "oracle": "skia" if kind != "harness_selftest" else "synthetic",
                        "stage": args.font_action, "error": error, "artifacts": artifacts})

    actual_provenance = {}
    if args.font_action in ("probe", "run"):
        env_path = output / "env/skity.json"
        invoke([executable, "env-info", "--backend", args.font_backend,
                "--repo-root", repo, "--report", env_path], env_path)
        git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
        diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True)
        paths = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                               cwd=repo, capture_output=True).stdout.split(b"\0")
        untracked = {os.fsdecode(path): sha256(repo / os.fsdecode(path)) for path in paths
                     if path and (repo / os.fsdecode(path)).is_file()}
        runtime_libraries = {}
        if sys.platform == "linux":
            linked = subprocess.run(["ldd", executable], capture_output=True, text=True)
            for name in re.findall(r"=> (/[^\s]+)", linked.stdout):
                library = Path(name).resolve()
                if library.is_file():
                    runtime_libraries[str(library)] = sha256(library)
        actual_provenance = {"runtime_libraries": runtime_libraries, "runner_binary": {"path": executable, "sha256": sha256(executable)},
                             "case_environment": {"path": str(env_path), "sha256": sha256(env_path)},
                             "skity_commit": git_head.stdout.strip(),
                             "tracked_diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
                             "untracked_files": untracked}
        write_json(output / "env/source.json", actual_provenance)

    if args.font_action == "cli-smoke":
        command = [sys.executable, repo / "harness/font/tests/smoke/skity_font_cli_smoke.py", executable, args.font_backend]
        code, _, log = invoke(command)
        record("CLI protocol smoke", code, "pass" if code == 0 else "cli_smoke_failed", "harness_selftest", {"log": str(log)})
        command = [sys.executable, "-m", "unittest", "discover", "-s", "harness/font/tests/python", "-v"]
        code, _, log = invoke(command)
        count = re.search(r"Ran (\d+) tests?", read_json(log)["stderr"])
        if not count or int(count.group(1)) == 0:
            code = 2
        record("Oracle contract selftests", code, "pass" if code == 0 else "oracle_selftest_failed", "harness_selftest", {"log": str(log)})
    else:
        kind = {"oracle-check": "oracle_readiness", "run": "engine_comparison", "compare": "engine_comparison",
                "probe": "skity_probe", "case-info": "case_schema"}[args.font_action]
        try:
            require(args, bool(args.font_case) != bool(args.font_manifest), "specify exactly one --font-case or --font-manifest")
            if args.font_case:
                paths = [Path(args.font_case).resolve()]
            else:
                manifest = read_json(args.font_manifest)
                require(manifest, manifest["schema_version"] == 1 and manifest["backend"] == args.font_backend,
                        "manifest backend/schema mismatch")
                platform_backends = {"macos-coretext": "coretext", "ios-sim-coretext": "coretext", "ios-device-coretext": "coretext",
                                     "android-freetype": "freetype", "windows-directwrite": "directwrite", "host-ft": "host-ft",
                                     "linux-freetype": "freetype", "linux-fontconfig": "fontconfig"}
                require(manifest, manifest["target_platform"] in manifest["platforms"] and
                        platform_backends.get(manifest["target_platform"]) == args.font_backend, "manifest platform mismatch")
                require(manifest, isinstance(manifest["cases"], list) and len(manifest["cases"]) > 0, "empty manifest")
                root = (repo / manifest["case_root"]).resolve()
                require(root, root.is_relative_to(repo), "case_root escapes repo")
                paths = [(root / name).resolve() for name in manifest["cases"]]
                require(paths, len(paths) == len(set(paths)) and all(p.is_relative_to(root) for p in paths),
                        "duplicate case or case path escapes manifest root")
            seen = set()
            for path in paths:
                case = read_json(path)
                require(case, case["id"] not in seen, "duplicate case id")
                seen.add(case["id"])
                name = case["id"]
                require(name, '/' not in name and '\\' not in name and name not in ('.', '..'), "unsafe case id")
                artifacts = {"case": str(path)}
                try:
                    require(case, case["backend"] == args.font_backend, "case backend mismatch")
                    if args.font_manifest:
                        require(case, manifest["target_platform"] in case["platforms"], "case does not target manifest platform")
                    if args.font_manifest and args.font_action in ("run", "compare"):
                        require(case, case["status"] == "active", "manifest comparison accepts active cases only")
                    schema_path = output / "case-info" / (name + ".json")
                    code, data, log = invoke([executable, "case-info", "--case", path, "--repo-root", repo, "--report", schema_path], schema_path)
                    artifacts["case_info"] = str(schema_path)
                    if code or not data.get("valid"):
                        record(name, code or 3, "schema_validation_failed", kind, artifacts)
                        continue
                    if args.font_action == "case-info":
                        record(name, 0, "pass", kind, artifacts)
                        continue
                    oracle = None
                    if args.font_action in ("oracle-check", "compare", "run"):
                        require(args, bool(args.font_oracle_dir), "--font-oracle-dir is required")
                        expected = Path(args.font_oracle_dir).resolve() / (name + ".skia.json")
                        artifacts["expected"] = str(expected)
                        oracle = validate_oracle(path, expected, repo, args.font_profile)
                        if args.font_action == "oracle-check":
                            if args.font_reference_oracle_dir:
                                reference_path = Path(args.font_reference_oracle_dir) / (name + ".skia.json")
                                reference = validate_oracle(path, reference_path, repo, args.font_profile)
                                require(oracle, {k: v for k, v in oracle.items() if k != "provenance"} ==
                                        {k: v for k, v in reference.items() if k != "provenance"}, "oracle repeat payload mismatch")
                                artifacts["reference_oracle"] = str(reference_path)
                            record(name, 0, "oracle_ready", kind, artifacts)
                            continue
                    actual = output / "skity" / (name + "." + args.font_backend + ".json")
                    artifacts["actual"] = str(actual)
                    if args.font_action in ("probe", "run"):
                        code, data, log = invoke([executable, "probe", "--case", path, "--backend", args.font_backend,
                                                  "--repo-root", repo, "--out", actual], actual)
                        if code:
                            record(name, code, data.get("reason_code", "probe_failed"), kind, artifacts)
                            continue
                        validate_payload(case, data, repo)
                        data["provenance"] = dict(actual_provenance, case_sha256=sha256(path),
                                                  font_files=font_fingerprints(case, repo))
                        write_json(actual, data)
                        if args.font_action == "probe":
                            record(name, 0, "pass", kind, artifacts)
                            continue
                    else:
                        actual_data = read_json(actual)
                        validate_payload(case, actual_data, repo)
                        provenance = actual_data["provenance"]
                        require(provenance, provenance["case_sha256"] == sha256(path) and
                                provenance["font_files"] == font_fingerprints(case, repo), "stale actual input")
                        require(provenance, provenance["runner_binary"]["sha256"] == sha256(executable), "stale actual binary")
                        for library, fingerprint in provenance.get("runtime_libraries", {}).items():
                            require(provenance, sha256(library) == fingerprint, "stale actual runtime library")
                    compare = output / "compare" / (name + "." + args.font_backend + ".compare.json")
                    artifacts["compare"] = str(compare)
                    code, data, log = invoke([executable, "compare", "--case", path, "--backend", args.font_backend,
                                              "--repo-root", repo, "--expected", expected, "--actual", actual,
                                              "--report", compare], compare)
                    if code == 1 and case["category"] == "glyph_image":
                        pixel_diff = output / "compare" / (name + ".pixels.json")
                        write_json(pixel_diff, image_differences(oracle, read_json(actual)))
                        artifacts["diff_pixels_file"] = str(pixel_diff)
                    record(name, code, data.get("reason_code", "compare_failed"), kind, artifacts)
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    record(name, 6, "invalid_oracle_or_artifact", kind, artifacts, str(error))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            record("Input", 3, "invalid_font_input", kind, {}, str(error))
    failures = [r for r in records if r["status"] == "FAIL"]
    report = {"summary": {"total": len(records), "passed": len(records) - len(failures), "failed": len(failures),
                          "duration_ms": int((time.time() - runner.started) * 1000)},
              "validation_counts": {}, "failures": failures, "results": records, "artifact_root": str(output)}
    for item in records:
        counts = report["validation_counts"].setdefault(item["validation_kind"], {"total": 0, "passed": 0, "failed": 0})
        counts["total"] += 1
        counts["passed" if item["status"] == "PASS" else "failed"] += 1
    if not records:
        return runner._create_infra_error("empty_font_run", "No cases executed")
    write_json(output / "agent_test_report.json", report)
    return report