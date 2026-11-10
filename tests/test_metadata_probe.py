"""Проверки подтверждений read-only MCP-пробы FName/enum на копиях ответов."""

import copy
import importlib.util
from pathlib import Path
import unittest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "probe_metadata_mcp.py"
SPEC = importlib.util.spec_from_file_location("probe_metadata_mcp", TOOL)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class MetadataProbeTests(unittest.TestCase):
    def fixture(self):
        targets = {key: {**value, "handle": "obj-" + str(index)}
                   for index, (key, value) in enumerate(probe.PROBE_OBJECTS.items(), 1)}
        objects = []
        for label, target in targets.items():
            properties = []
            for name in probe.PROPERTY_NAMES[label]:
                if label == "light":
                    properties.append({"name": name, "type": "NameProperty", "supported": True, "value": "spine_03"})
                else:
                    expected = probe.ENUM_EXPECTATIONS[name]
                    properties.append({"name": name, "type": "EnumProperty", "supported": True, "value": expected["value"],
                                       "enum": {"path": expected["path"], "underlying_type": "ByteProperty", "name_status": "matched",
                                                "value_name": expected["value_name"], "matched_count": 1,
                                                "ambiguous": False, "reason": None}})
            objects.append({"handle": target["handle"], "path": target["path"], "class": target["class"],
                            "partial": False, "truncated": False, "properties": properties})
        return targets, {"snapshot_id": "snapshot-1", "source": "live_native", "session_id": "session-1", "process_id": 10,
                         "thread_id": 20, "tick_count": 30, "consistency": "single_tick_sequential", "objects": objects,
                         "partial": False, "truncated": False}

    def test_actual_metadata_and_name_are_confirmed_without_mutating_inputs(self):
        targets, snapshot = self.fixture()
        original = copy.deepcopy(snapshot)
        values = probe.check_snapshot(snapshot, targets)
        self.assertEqual(values["m_SocketName"], {"type": "NameProperty", "value": "spine_03"})
        self.assertEqual(values["UsageType"]["value_name"], "EAttackUsageType::Regular")
        self.assertEqual(snapshot, original)

    def test_discovery_requires_one_exact_cdo_with_complete_result(self):
        target = {**probe.PROBE_OBJECTS["light"], "handle": "obj-1"}
        response = {"items": [target], "truncated": False, "total_slots": 200000,
                    "scanned_slots": 200000, "skipped": 10, "thread_id": 20}
        self.assertEqual(probe.check_target(response, probe.PROBE_OBJECTS["light"]), target)
        for mutate in (lambda r: r["items"].append(dict(target)), lambda r: r.update(truncated=True),
                       lambda r: r["items"][0].update(path="/Other.Default__GA_Spell_Light")):
            with self.subTest(mutate=mutate):
                invalid = copy.deepcopy(response)
                mutate(invalid)
                with self.assertRaises(probe.ProbeError):
                    probe.check_target(invalid, probe.PROBE_OBJECTS["light"])

    def test_actual_discovery_schema_allows_unrelated_skipped_slots_and_rejects_incomplete_scan(self):
        target = {**probe.PROBE_OBJECTS["light"], "handle": "obj-1"}
        valid = {"items": [target], "truncated": False, "total_slots": 123,
                 "scanned_slots": 123, "skipped": 5, "thread_id": 20}
        self.assertEqual(probe.check_target(valid, probe.PROBE_OBJECTS["light"]), target)
        for change in ({"scanned_slots": 122}, {"skipped": -1}, {"skipped": True},
                       {"total_slots": True}, {"skipped": 124}, {"truncated": True},
                       {"total_slots": 0, "scanned_slots": 0, "skipped": 0}):
            with self.subTest(change=change):
                invalid = copy.deepcopy(valid)
                invalid.update(change)
                with self.assertRaises(probe.ProbeError) as caught:
                    probe.check_target(invalid, probe.PROBE_OBJECTS["light"])
                self.assertEqual(caught.exception.code, "probe_discovery_incomplete")
                self.assertEqual(caught.exception.context["item_count"], 1)

    def test_wrong_reflected_type_value_or_support_cannot_confirm_fname(self):
        for change in ({"type": "StrProperty"}, {"value": "spine_04"}, {"supported": False}, {"enum": {}}):
            with self.subTest(change=change):
                targets, snapshot = self.fixture()
                snapshot["objects"][0]["properties"][0].update(change)
                with self.assertRaises(probe.ProbeError) as caught:
                    probe.check_snapshot(snapshot, targets)
                self.assertEqual(caught.exception.code, "probe_name_mismatch")

    def test_missing_duplicate_property_and_partial_snapshot_are_failures(self):
        for change in ("missing", "duplicate", "partial", "object_truncated", "unknown_source"):
            with self.subTest(change=change):
                targets, snapshot = self.fixture()
                if change == "missing": snapshot["objects"][1]["properties"].pop()
                elif change == "duplicate": snapshot["objects"][1]["properties"].append(copy.deepcopy(snapshot["objects"][1]["properties"][0]))
                elif change == "partial": snapshot["partial"] = True
                elif change == "object_truncated": snapshot["objects"][1]["truncated"] = True
                else: snapshot["source"] = "client_copy"
                with self.assertRaises(probe.ProbeError):
                    probe.check_snapshot(snapshot, targets)

    def test_snapshot_id_is_required_and_cannot_be_bool_or_empty(self):
        for value in (None, "", True):
            with self.subTest(value=value):
                targets, snapshot = self.fixture()
                if value is None: snapshot.pop("snapshot_id")
                else: snapshot["snapshot_id"] = value
                with self.assertRaises(probe.ProbeError) as caught:
                    probe.check_snapshot(snapshot, targets)
                self.assertEqual(caught.exception.code, "probe_snapshot_incomplete")

    def test_enum_numeric_bool_unknown_underlying_and_extra_metadata_are_rejected(self):
        for change in ("bool", "float", "underlying", "extra", "unsupported"):
            with self.subTest(change=change):
                targets, snapshot = self.fixture()
                prop = snapshot["objects"][1]["properties"][0]
                if change == "bool": prop["value"] = False
                elif change == "float": prop["value"] = 0.0
                elif change == "underlying": prop["enum"]["underlying_type"] = "FloatProperty"
                elif change == "extra": prop["enum"]["unexpected"] = True
                else: prop["supported"] = False
                with self.assertRaises(probe.ProbeError):
                    probe.check_snapshot(snapshot, targets)

    def test_label_path_match_and_ambiguity_are_required(self):
        for change in ({"value_name": "EAttackDirectionRequirement::Any"}, {"name_status": "unavailable"},
                       {"path": "/Script/Angelscript.OtherEnum"}, {"matched_count": True},
                       {"matched_count": 2, "ambiguous": True}, {"reason": "enum_table_changed"}):
            with self.subTest(change=change):
                targets, snapshot = self.fixture()
                snapshot["objects"][1]["properties"][0]["enum"].update(change)
                with self.assertRaises(probe.ProbeError) as caught:
                    probe.check_snapshot(snapshot, targets)
                self.assertEqual(caught.exception.code, "probe_enum_label_mismatch")

    def test_byte_enum_is_allowed_only_with_byte_underlying(self):
        targets, snapshot = self.fixture()
        prop = snapshot["objects"][1]["properties"][0]
        prop["type"] = "ByteProperty"
        probe.check_snapshot(snapshot, targets)
        prop["enum"]["underlying_type"] = "IntProperty"
        with self.assertRaises(probe.ProbeError) as caught:
            probe.check_snapshot(snapshot, targets)
        self.assertEqual(caught.exception.code, "probe_enum_underlying_invalid")

    def test_incomplete_or_changed_comparison_cannot_be_success(self):
        _, before = self.fixture()
        after = copy.deepcopy(before)
        after["snapshot_id"] = "snapshot-2"
        valid = {"source": "copied_snapshots", "validation": "client_supplied_copies", "provenance": "unverified",
                 "before_snapshot_id": before["snapshot_id"], "after_snapshot_id": after["snapshot_id"], "comparable": True,
                 "reason": "same_session_process", "equal_selected_values": True, "items": [], "unknown_count": 0,
                 "value_changes": 0, "type_changes": 0, "coverage_changes": 0, "truncated": False,
                 "coverage": {"complete": True}, "metadata_annotation_count": 0, "metadata_annotations": []}
        probe.check_comparison(valid, before, after)
        for change in ({"comparable": False}, {"equal_selected_values": False}, {"unknown_count": 1},
                       {"coverage": {"complete": False}}, {"metadata_annotation_count": 1},
                       {"after_snapshot_id": "other"}, {"unknown_count": False},
                       {"metadata_annotations": [{"kind": "enum_label_changed"}]}):
            with self.subTest(change=change):
                invalid = copy.deepcopy(valid)
                invalid.update(change)
                with self.assertRaises(probe.ProbeError):
                    probe.check_comparison(invalid, before, after)


if __name__ == "__main__":
    unittest.main()
