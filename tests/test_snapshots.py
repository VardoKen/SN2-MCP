"""Проверки семантики сравнения выбранных копий и ограничений JSON."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1r_mcp.snapshots import SnapshotError, compare_snapshots


def prop(name="Enabled", value=True, kind="BoolProperty"):
    return {"name": name, "type": kind, "supported": True, "value": value}


def snapshot(properties=None, handle="obj-1"):
    return {
        "snapshot_id": "snapshot-1", "source": "live_native", "session_id": "session-1",
        "process_id": 123, "thread_id": 456, "tick_count": 7,
        "consistency": "single_tick_sequential", "truncated": False, "partial": False,
        "objects": [{"handle": handle, "path": "/Script/Probe.Default__Probe", "class": "Probe",
                     "properties": [prop()] if properties is None else properties}],
    }


def object_reference(handle="ref-1", index=12):
    return {"handle": handle, "index": index, "name": "Default__Target", "path": "/Script/Probe.Default__Target",
            "class": "Target", "session_id": "session-1"}


def enum_property(value=0, *, status="matched", label="EProbe::None", count=1,
                  underlying="IntProperty", path="/Script/Probe.EProbe", reason=None, kind="EnumProperty"):
    return {"name": "Mode", "type": kind, "supported": True, "value": value, "enum": {
        "path": path, "underlying_type": underlying, "name_status": status,
        "value_name": label, "matched_count": count, "ambiguous": count > 1, "reason": reason,
    }}


class SnapshotComparisonTests(unittest.TestCase):
    def assert_invalid(self, candidate, code="invalid_snapshot"):
        with self.assertRaises(SnapshotError) as failure:
            compare_snapshots(candidate, snapshot())
        self.assertEqual(failure.exception.code, code)

    def test_equal_values_report_only_selected_copies_and_do_not_mutate(self):
        before = snapshot([prop("A", 1, "IntProperty"), prop("B", "Текст", "StrProperty")])
        after = copy.deepcopy(before)
        after["snapshot_id"] = "snapshot-2"
        after["objects"][0]["properties"].reverse()
        originals = copy.deepcopy((before, after))
        result = compare_snapshots(before, after)
        self.assertEqual((before, after), originals)
        self.assertTrue(result["equal_selected_values"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["after_snapshot_id"], "snapshot-2")
        self.assertEqual(result["source"], "copied_snapshots")
        self.assertEqual(result["provenance"], "unverified")
        self.assertEqual(result["validation"], "client_supplied_copies")

    def test_type_and_value_changes_are_separate_and_bool_is_not_integer(self):
        result = compare_snapshots(snapshot(), snapshot([prop(value=1, kind="IntProperty")]))
        self.assertEqual([item["kind"] for item in result["items"]], ["type_changed", "value_changed"])
        self.assertEqual((result["type_changes"], result["value_changes"]), (1, 1))
        self.assertFalse(result["equal_selected_values"])
        changed_type_only = compare_snapshots(snapshot([prop(value=1, kind="IntProperty")]),
                                              snapshot([prop(value=1, kind="Int64Property")]))
        self.assertEqual(changed_type_only["type_changes"], 1)
        self.assertEqual(changed_type_only["value_changes"], 0)

    def test_different_process_and_session_do_not_report_changes(self):
        for field, reason in (("process_id", "different_process"), ("session_id", "different_session")):
            before, after = snapshot(), snapshot([prop(value=False)])
            after[field] = 789 if field == "process_id" else "session-2"
            result = compare_snapshots(before, after)
            self.assertFalse(result["comparable"])
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["items"], [])
            self.assertIsNone(result["equal_selected_values"])

    def test_changed_target_set_is_coverage_and_not_object_lifecycle(self):
        result = compare_snapshots(snapshot(), snapshot(handle="obj-2"))
        self.assertEqual({item["kind"] for item in result["items"]}, {"coverage_added", "coverage_removed"})
        self.assertEqual(result["coverage_changes"], 2)
        self.assertIsNone(result["equal_selected_values"])
        self.assertEqual(result["value_changes"], 0)

    def test_object_error_and_identity_mismatch_are_unknown(self):
        for code, expected in (("stale_handle", "object_unavailable"),
                               ("object_destroying", "object_unavailable"), ("invalid_memory", "unknown")):
            after = snapshot()
            after["partial"] = True
            after["objects"] = [{"handle": "obj-1", "error": {"code": code, "message": "Недоступен"}}]
            result = compare_snapshots(snapshot(), after)
            self.assertEqual(result["items"][0]["kind"], expected)
            self.assertEqual(result["unknown_count"], 1)
        after = snapshot([prop(value=False)])
        after["objects"][0]["class"] = "Other"
        result = compare_snapshots(snapshot(), after)
        self.assertEqual(result["items"][0]["reason"], "identity_mismatch")
        self.assertEqual(result["value_changes"], 0)

    def test_partial_and_unsupported_do_not_hide_other_comparable_fields(self):
        before = snapshot([prop("A", 1, "IntProperty"), prop("B")])
        after = snapshot([prop("A", 2, "IntProperty"),
                          {"name": "B", "supported": False,
                           "error": {"code": "property_not_found", "message": "Нет свойства"}}])
        after["partial"] = True
        result = compare_snapshots(before, after)
        self.assertEqual((result["value_changes"], result["unknown_count"]), (1, 1))
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["equal_selected_values"])
        # Returned errors are separate copies as well.
        result["items"][1]["after_error"]["message"] = "Изменён ответ"
        self.assertEqual(after["objects"][0]["properties"][1]["error"]["message"], "Нет свойства")

    def test_property_absence_is_unknown_coverage_not_deleted_property(self):
        result = compare_snapshots(snapshot([prop("A"), prop("B")]), snapshot([prop("A")]))
        self.assertEqual(result["items"][0]["reason"], "selected_property_coverage_changed")
        self.assertEqual(result["items"][0]["property"], "B")
        self.assertEqual(result["value_changes"], 0)

    def test_missing_source_and_unconfirmed_consistency_block_value_changes(self):
        for mode in ("missing_source", "other_source", "other_consistency"):
            before, after = snapshot(), snapshot([prop(value=False)])
            if mode == "missing_source":
                del before["source"]
            elif mode == "other_source":
                after["source"] = "source_only"
            else:
                after["consistency"] = "unconfirmed"
            result = compare_snapshots(before, after)
            self.assertGreater(result["unknown_count"], 0)
            self.assertEqual(result["value_changes"], 0)
            self.assertIsNone(result["equal_selected_values"])

    def test_truncated_source_cannot_prove_complete_equality(self):
        before = snapshot()
        before["truncated"] = True
        result = compare_snapshots(before, snapshot())
        self.assertTrue(result["coverage"]["before_truncated"])
        self.assertIsNone(result["equal_selected_values"])

    def test_result_limit_preserves_totals(self):
        before = snapshot([prop("A", 1, "IntProperty"), prop("B", 1, "IntProperty")])
        after = snapshot([prop("A", 2, "IntProperty"), prop("B", 2, "IntProperty")])
        result = compare_snapshots(before, after, limit=1)
        self.assertEqual((len(result["items"]), result["total_items"], result["value_changes"]), (1, 2, 2))
        self.assertTrue(result["truncated"])
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(SnapshotError) as failure:
                compare_snapshots(before, after, limit)
            self.assertEqual(failure.exception.code, "invalid_limit")

    def test_requested_result_limits_above_two_hundred_preserve_exact_changes(self):
        before = snapshot([prop(f"P{index:03}", index, "IntProperty") for index in range(205)])
        after = snapshot([prop(f"P{index:03}", index + 1, "IntProperty") for index in range(205)])
        expected = [{"kind": "value_changed", "handle": "obj-1", "property": f"P{index:03}",
                     "before": index, "after": index + 1} for index in range(205)]
        for limit in (201, 205):
            with self.subTest(limit=limit):
                result = compare_snapshots(before, after, limit=limit)
                self.assertEqual(result["items"], expected[:limit])
                self.assertEqual(result["total_items"], 205)
                self.assertEqual(result["value_changes"], 205)
                self.assertEqual(result["truncated"], limit < 205)
        default = compare_snapshots(before, after)
        self.assertEqual(default["items"], expected[:100])
        self.assertTrue(default["truncated"])

    def test_duplicate_handles_names_and_invalid_shapes_are_rejected(self):
        candidate = snapshot()
        candidate["objects"].append(copy.deepcopy(candidate["objects"][0]))
        self.assert_invalid(candidate)
        self.assert_invalid(snapshot([prop(), prop()]))
        self.assert_invalid(snapshot([{"name": "A", "type": "BoolProperty", "supported": True}]))
        self.assert_invalid(snapshot([prop(value={"nested": 1})]))
        candidate = snapshot()
        candidate["unexpected"] = True
        self.assert_invalid(candidate)
        for field in ("process_id", "thread_id", "tick_count"):
            candidate = snapshot()
            candidate[field] = True
            self.assert_invalid(candidate)
        candidate = snapshot()
        candidate["partial"] = 1
        self.assert_invalid(candidate)

    def test_invalid_utf8_non_finite_and_json_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), "\ud800", b"bytes"):
            with self.subTest(value=repr(value)):
                self.assert_invalid(snapshot([prop(value=value)]))

    def test_large_snapshot_preserves_seventeen_objects_thirty_three_properties_and_full_text(self):
        before = snapshot()
        before["objects"] = [snapshot([prop(f"P{index:02}", index, "IntProperty") for index in range(33)],
                                      handle=f"obj-{object_index:02}")["objects"][0]
                             for object_index in range(17)]
        text = "Мод 🐉\r\n" * 100000
        before["objects"][0]["properties"][-1] = prop("P32", text, "StrProperty")
        before["limits"] = dict.fromkeys(("objects", "properties_per_object", "scan_fields_per_object",
                                          "property_value_bytes", "snapshot_bytes"))
        self.assertGreater(len(json.dumps(before, ensure_ascii=False).encode("utf-8")), 1024 * 1024)
        after = copy.deepcopy(before)
        after["objects"][0]["properties"][-1]["value"] = text + "конец"
        after["objects"][-1]["properties"][-1]["value"] = 99
        result = compare_snapshots(before, after)
        self.assertEqual(result["items"], [
            {"kind": "value_changed", "handle": "obj-00", "property": "P32", "before": text, "after": text + "конец"},
            {"kind": "value_changed", "handle": "obj-16", "property": "P32", "before": 32, "after": 99},
        ])
        self.assertEqual(result["coverage"]["before_targets"], 17)
        self.assertEqual(result["coverage"]["after_targets"], 17)
        self.assertEqual(result["total_items"], 2)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(before["objects"][0]["properties"][-1]["value"], text)

    def test_large_diff_preserves_full_values_without_losing_counts(self):
        before = snapshot([prop(value="a" * 600000, kind="StrProperty")])
        after = snapshot([prop(value="b" * 600000, kind="StrProperty")])
        result = compare_snapshots(before, after)
        self.assertEqual(result["value_changes"], 1)
        self.assertEqual(result["total_items"], 1)
        self.assertEqual(result["items"], [{"kind": "value_changed", "handle": "obj-1", "property": "Enabled",
                                          "before": "a" * 600000, "after": "b" * 600000}])
        self.assertGreater(len(json.dumps(result).encode("utf-8")), 1024 * 1024)
        self.assertFalse(result["truncated"])

    def test_actual_native_limits_object_flags_and_null_missing_type(self):
        before, after = snapshot(), snapshot()
        for candidate in (before, after):
            candidate["limits"] = {
                "objects": 16, "properties_per_object": 32, "scan_fields_per_object": 4096,
                "property_value_bytes": 16384, "snapshot_bytes": 262144,
            }
            candidate["objects"][0].update(partial=False, truncated=False)
        self.assertTrue(compare_snapshots(before, after)["equal_selected_values"])
        after["objects"][0]["properties"].append({
            "name": "Missing", "type": None, "supported": False,
            "error": {"code": "property_not_found", "message": "Свойство не найдено"},
        })
        after["objects"][0]["partial"] = True
        after["partial"] = True
        result = compare_snapshots(before, after)
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["coverage"]["after_incomplete_objects"], 1)
        self.assertIsNone(result["equal_selected_values"])
        # Even inconsistent caller-supplied top flags cannot hide an object scan limit.
        after = copy.deepcopy(before)
        after["objects"][0]["truncated"] = True
        result = compare_snapshots(before, after)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["equal_selected_values"])

    def test_native_metadata_validation_remains_strict(self):
        limits = {
            "objects": 16, "properties_per_object": 32, "scan_fields_per_object": 4096,
            "property_value_bytes": 16384, "snapshot_bytes": 262144,
        }
        for selected_limits in (dict.fromkeys(limits), {**limits, "objects": None, "snapshot_bytes": None}):
            candidate = snapshot()
            candidate["limits"] = selected_limits
            with self.subTest(limits=selected_limits):
                self.assertTrue(compare_snapshots(candidate, candidate)["equal_selected_values"])
        for key, value in (("objects", True), ("snapshot_bytes", 0), ("other", 1)):
            candidate = snapshot()
            candidate["limits"] = {**limits, key: value}
            self.assert_invalid(candidate)
        candidate = snapshot()
        candidate["limits"] = {"objects": 16}
        self.assert_invalid(candidate)
        for field in ("partial", "truncated"):
            candidate = snapshot()
            candidate["objects"][0][field] = 1
            self.assert_invalid(candidate)
        self.assert_invalid(snapshot([prop(kind=None)]))

    def test_object_reference_and_null_have_stable_declared_type(self):
        null_snapshot = snapshot([prop("Target", None, "ObjectProperty")])
        reference_snapshot = snapshot([prop("Target", object_reference(), "ObjectProperty")])
        self.assertTrue(compare_snapshots(null_snapshot, null_snapshot)["equal_selected_values"])
        self.assertTrue(compare_snapshots(reference_snapshot, reference_snapshot)["equal_selected_values"])
        for before, after in ((null_snapshot, reference_snapshot), (reference_snapshot, null_snapshot)):
            result = compare_snapshots(before, after)
            self.assertEqual(result["type_changes"], 0)
            self.assertEqual(result["value_changes"], 1)
            self.assertEqual(result["items"][0]["kind"], "value_changed")
        other = snapshot([prop("Target", object_reference("ref-2", 13), "ObjectProperty")])
        result = compare_snapshots(reference_snapshot, other)
        self.assertEqual((result["value_changes"], result["unknown_count"]), (1, 0))

    def test_same_reference_handle_with_changed_identity_is_unknown(self):
        before = snapshot([prop("Target", object_reference(), "ObjectProperty")])
        for key, changed in (("index", 13), ("name", "Other"), ("path", "/Script/Other"), ("class", "Other")):
            after = copy.deepcopy(before)
            after["objects"][0]["properties"][0]["value"][key] = changed
            result = compare_snapshots(before, after)
            self.assertEqual((result["unknown_count"], result["value_changes"]), (1, 0))
            self.assertEqual(result["items"][0]["reason"], "object_reference_identity_mismatch")
            self.assertIsNone(result["equal_selected_values"])

    def test_reference_descriptors_remain_client_copies_and_detached(self):
        before = snapshot([prop("Target", None, "ObjectProperty")])
        after = snapshot([prop("Target", object_reference(), "ObjectProperty")])
        result = compare_snapshots(before, after)
        self.assertEqual(result["provenance"], "unverified")
        result["items"][0]["after"]["path"] = "/ChangedOutput"
        self.assertEqual(after["objects"][0]["properties"][0]["value"]["path"], "/Script/Probe.Default__Target")
        del after["source"]
        result = compare_snapshots(before, after)
        self.assertEqual(result["value_changes"], 0)
        self.assertGreater(result["unknown_count"], 0)

    def test_reference_descriptor_sessions_and_shape_are_validated(self):
        reference = object_reference()
        for key, changed in (("session_id", "session-2"), ("index", True), ("index", -1),
                             ("index", 2**31), ("handle", ""), ("name", None), ("class", 123), ("path", "")):
            value = {**reference, key: changed}
            with self.subTest(key=key, value=repr(changed)[:30]):
                self.assert_invalid(snapshot([prop("Target", value, "ObjectProperty")]))
        self.assert_invalid(snapshot([prop("Target", {**reference, "extra": True}, "ObjectProperty")]))
        value = dict(reference)
        del value["index"]
        self.assert_invalid(snapshot([prop("Target", value, "ObjectProperty")]))
        self.assert_invalid(snapshot([prop("Target", reference, "StructProperty")]))

    def test_long_reference_descriptors_preserve_full_identity_and_int32_indices(self):
        value = {**object_reference(), "handle": "ссылка" * 1000, "index": 4_000_001,
                 "name": "ж" * 10000, "class": "名" * 10000, "path": "/Script/" + "Путь" * 5000,
                 "session_id": "сеанс" * 2000}
        for index in (4_000_001, 2**31 - 1):
            for property_type in ("ObjectProperty", "ClassProperty"):
                value["index"] = index
                before = snapshot([prop("Target", None, property_type)], handle="объект" * 2000)
                before.update(session_id=value["session_id"], snapshot_id="снимок" * 2000)
                before["objects"][0].update(path="путь" * 3000, **{"class": "класс" * 3000})
                after = copy.deepcopy(before)
                after["objects"][0]["properties"][0]["value"] = copy.deepcopy(value)
                with self.subTest(index=index, property_type=property_type):
                    result = compare_snapshots(before, after)
                    self.assertEqual(result["items"][0]["after"], value)
                    self.assertEqual(result["value_changes"], 1)
                    self.assertEqual(result["unknown_count"], 0)
                    self.assertFalse(result["truncated"])
                    result["items"][0]["after"]["name"] = "изменённая копия"
                    self.assertEqual(after["objects"][0]["properties"][0]["value"]["name"], value["name"])

    def test_long_object_and_property_errors_preserve_detached_messages(self):
        message = "Ошибка 🐉\r\n" * 3000
        error = {"code": "код" * 3000, "message": message}
        before = snapshot([prop("Поле" * 3000)])
        after = snapshot([{"name": "Поле" * 3000, "type": None, "supported": False, "error": error}])
        result = compare_snapshots(before, after)
        self.assertEqual(result["items"][0]["after_error"], error)
        result["items"][0]["after_error"]["message"] = "изменённая копия"
        self.assertEqual(after["objects"][0]["properties"][0]["error"]["message"], message)
        after = snapshot()
        after["objects"] = [{"handle": "obj-1", "error": copy.deepcopy(error)}]
        result = compare_snapshots(snapshot(), after)
        self.assertEqual(result["items"][0]["after_error"], error)

    def test_same_reference_type_cannot_be_hidden_as_loose_scalar(self):
        for value in (True, 12, "ref-1", [object_reference()]):
            self.assert_invalid(snapshot([prop("Target", value, "ObjectProperty")]))
        before = snapshot([prop("Target", object_reference(), "ObjectProperty")])
        after = snapshot([prop("Target", "ref-1", "StrProperty")])
        result = compare_snapshots(before, after)
        self.assertEqual((result["type_changes"], result["value_changes"]), (1, 1))
        self.assertEqual(result["items"][0]["before_value_kind"], "object_reference")

    def test_enum_label_changes_are_metadata_while_number_compares(self):
        before = snapshot([enum_property(7, label="EProbe::First")])
        after = snapshot([enum_property(7, label="EProbe::Alias", count=2)])
        result = compare_snapshots(before, after)
        self.assertTrue(result["equal_selected_values"])
        self.assertEqual((result["value_changes"], result["type_changes"], result["unknown_count"]), (0, 0, 0))
        self.assertEqual(result["metadata_annotation_count"], 1)
        self.assertEqual(result["metadata_annotations"][0]["kind"], "enum_metadata_changed")
        result["metadata_annotations"][0]["after"]["path"] = "/ChangedOutput"
        self.assertEqual(after["objects"][0]["properties"][0]["enum"]["path"], "/Script/Probe.EProbe")
        after["objects"][0]["properties"][0]["value"] = 8
        result = compare_snapshots(before, after)
        self.assertEqual(result["value_changes"], 1)
        self.assertFalse(result["equal_selected_values"])

    def test_enum_unavailable_metadata_never_hides_numeric_value(self):
        before = snapshot([enum_property(0)])
        after = snapshot([enum_property(2, status="unavailable", label=None, count=0,
                                       path=None, reason="invalid_enum_object")])
        result = compare_snapshots(before, after)
        self.assertEqual((result["value_changes"], result["unknown_count"]), (1, 0))
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["metadata_annotations"][0]["kind"], "enum_metadata_unavailable")
        equal = compare_snapshots(after, after)
        self.assertTrue(equal["equal_selected_values"])
        self.assertEqual(equal["metadata_annotation_count"], 1)
        missing = snapshot([prop("Mode", 2, "EnumProperty")])
        result = compare_snapshots(after, missing)
        self.assertTrue(result["equal_selected_values"])
        self.assertEqual(result["metadata_annotation_count"], 1)

    def test_enum_actual_statuses_unsigned_and_unicode_labels(self):
        cases = [enum_property(0), enum_property(99, status="not_found", label=None, count=0),
                 enum_property(1, status="unavailable", label=None, count=2, reason="enum_name_decode_failed"),
                 enum_property(2**64 - 1, status="value_unrepresentable", label=None, count=0,
                               underlying="UInt64Property", reason="value_exceeds_int64"),
                 enum_property(255, kind="ByteProperty", underlying="ByteProperty", label="Enum::最大"),
                 enum_property(-2**63, underlying="Int64Property", label="Набор::Отрицательное"),
                 enum_property(2**32 - 1, underlying="UInt32Property", label="Enum::Large")]
        for candidate in cases:
            with self.subTest(status=candidate["enum"]["name_status"]):
                copied = snapshot([candidate])
                self.assertTrue(compare_snapshots(copied, copied)["equal_selected_values"])
        self.assertTrue(compare_snapshots(snapshot([prop("Socket", "spine_03", "NameProperty")]),
                                          snapshot([prop("Socket", "spine_03", "NameProperty")]))["equal_selected_values"])

    def test_enum_metadata_ranges_and_states_are_strict(self):
        valid = enum_property()
        malformed = [({"extra": True}, None), ({"underlying_type": "FloatProperty"}, None),
                     ({"name_status": "unknown"}, None), ({"name_status": []}, None), ({"matched_count": True}, None),
                     ({"matched_count": 2}, None),
                     ({"value_name": None}, None), ({"path": None}, None),
                     ({"reason": "still_not_allowed_for_matched"}, None), ({"value_name": "Bad\0Name"}, None),
                     ({"name_status": "not_found", "value_name": None}, None),
                     ({"name_status": "value_unrepresentable", "value_name": None, "matched_count": 0}, None)]
        for metadata, _ in malformed:
            candidate = copy.deepcopy(valid)
            candidate["enum"].update(metadata)
            self.assert_invalid(snapshot([candidate]))
        candidate = copy.deepcopy(valid)
        del candidate["enum"]["ambiguous"]
        self.assert_invalid(snapshot([candidate]))
        for number in (True, 1.0, -2**31 - 1, 2**31):
            self.assert_invalid(snapshot([enum_property(number)]))
        self.assert_invalid(snapshot([enum_property(1, kind="IntProperty")]))
        self.assert_invalid(snapshot([enum_property(1, kind="ByteProperty", underlying="IntProperty")]))
        self.assert_invalid(snapshot([prop("Mode", 2**64, "EnumProperty")]))
        self.assert_invalid(snapshot([prop("Socket", 17, "NameProperty")]))

    def test_long_enum_metadata_and_large_match_count_preserve_annotations(self):
        path, label, reason = "/Script/" + "Перечисление" * 1000, "Имя" * 3000, "Причина" * 1000
        before = snapshot([enum_property(7)])
        after = snapshot([enum_property(7, path=path, label=label, count=5000)])
        result = compare_snapshots(before, after)
        self.assertTrue(result["equal_selected_values"])
        self.assertEqual(result["metadata_annotations"][0]["after"], after["objects"][0]["properties"][0]["enum"])
        result["metadata_annotations"][0]["after"]["value_name"] = "изменённая копия"
        self.assertEqual(after["objects"][0]["properties"][0]["enum"]["value_name"], label)
        unavailable = snapshot([enum_property(7, status="unavailable", path=path, label=None, count=5000, reason=reason)])
        result = compare_snapshots(before, unavailable)
        self.assertTrue(result["equal_selected_values"])
        self.assertEqual(result["metadata_annotations"][0]["after"]["reason"], reason)
        for field in ("path", "value_name", "reason"):
            candidate = copy.deepcopy(unavailable if field == "reason" else after)
            candidate["objects"][0]["properties"][0]["enum"][field] += "\0"
            with self.subTest(field=field):
                self.assert_invalid(candidate)

    def test_enum_metadata_annotations_share_result_limit_and_preserve_totals(self):
        before = snapshot([enum_property(0)])
        after = snapshot([enum_property(1, status="not_found", label=None, count=0)])
        result = compare_snapshots(before, after, limit=1)
        self.assertEqual((result["value_changes"], len(result["items"])), (1, 1))
        self.assertEqual((result["metadata_annotation_count"], len(result["metadata_annotations"])), (1, 0))
        self.assertTrue(result["truncated"])

    def test_plain_byte_property_enforces_uint8_without_enum_metadata(self):
        for value in (True, 1.5, "not-a-byte", -1, 256):
            with self.subTest(value=repr(value)):
                self.assert_invalid(snapshot([prop("Byte", value, "ByteProperty")]))
        for value in (0, 255):
            candidate = snapshot([prop("Byte", value, "ByteProperty")])
            result = compare_snapshots(candidate, candidate)
            self.assertTrue(result["equal_selected_values"])
            self.assertEqual(result["metadata_annotation_count"], 0)

    def test_class_property_accepts_native_and_angelscript_class_descriptors(self):
        for kind, path in (("Class", "/Script/CoreUObject.Object"),
                           ("AngelscriptClass", "/Script/Angelscript.CM_Attack")):
            reference = {**object_reference(), "name": "CM_Attack", "path": path, "class": kind}
            candidate = snapshot([prop("ActorClass", reference, "ClassProperty")])
            result = compare_snapshots(candidate, candidate)
            self.assertTrue(result["equal_selected_values"])
            self.assertEqual(result["provenance"], "unverified")
        null_candidate = snapshot([prop("ActorClass", None, "ClassProperty")])
        self.assertTrue(compare_snapshots(null_candidate, null_candidate)["equal_selected_values"])

    def test_class_property_transitions_preserve_declared_reference_type(self):
        first = {**object_reference(), "name": "First", "path": "/Script/Angelscript.First", "class": "AngelscriptClass"}
        second = {**object_reference("ref-2", 13), "name": "Second", "path": "/Script/Angelscript.Second", "class": "AngelscriptClass"}
        null_candidate = snapshot([prop("ActorClass", None, "ClassProperty")])
        first_candidate = snapshot([prop("ActorClass", first, "ClassProperty")])
        second_candidate = snapshot([prop("ActorClass", second, "ClassProperty")])
        for before, after in ((null_candidate, first_candidate), (first_candidate, null_candidate),
                              (first_candidate, second_candidate)):
            result = compare_snapshots(before, after)
            self.assertEqual((result["type_changes"], result["value_changes"]), (0, 1))
        object_candidate = snapshot([prop("ActorClass", first, "ObjectProperty")])
        result = compare_snapshots(object_candidate, first_candidate)
        self.assertEqual(result["type_changes"], 1)
        self.assertEqual(result["items"][0]["after_value_kind"], "class_reference")
        replaced = snapshot([prop("ActorClass", {**first, "handle": "replacement", "index": 14}, "ClassProperty")])
        result = compare_snapshots(first_candidate, replaced)
        self.assertEqual((result["type_changes"], result["value_changes"]), (0, 1))
        changed_session = copy.deepcopy(first_candidate)
        changed_session["session_id"] = "session-2"
        changed_session["objects"][0]["properties"][0]["value"]["session_id"] = "session-2"
        result = compare_snapshots(first_candidate, changed_session)
        self.assertFalse(result["comparable"])
        self.assertEqual(result["items"], [])

    def test_class_property_reuses_strict_descriptor_checks_and_identity_guard(self):
        reference = {**object_reference(), "name": "CM_Attack", "path": "/Script/Angelscript.CM_Attack", "class": "AngelscriptClass"}
        before = snapshot([prop("ActorClass", reference, "ClassProperty")])
        for key, value in (("index", 13), ("name", "Other"), ("path", "/Script/Angelscript.Other"), ("class", "Class")):
            after = snapshot([prop("ActorClass", {**reference, key: value}, "ClassProperty")])
            result = compare_snapshots(before, after)
            self.assertEqual((result["value_changes"], result["unknown_count"]), (0, 1))
            self.assertEqual(result["items"][0]["reason"], "object_reference_identity_mismatch")
        for value in (True, 12, "ref-1", {**reference, "extra": True},
                      {**reference, "session_id": "another-session"}, {**reference, "index": 2**31}):
            self.assert_invalid(snapshot([prop("ActorClass", value, "ClassProperty")]))


if __name__ == "__main__":
    unittest.main()
