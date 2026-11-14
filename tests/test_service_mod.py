"""Verify service routing against native metadata and unknown-effect contracts."""
from copy import deepcopy
import unittest

from sn2_mcp.native_client import NativeError
from sn2_mcp.service_mod import ServiceMod, ServiceModError, SERVICE_CLASS, SERVICE_CLASS_PATH, SERVICE_ID


def actor_status():
    return {"mod_id": SERVICE_ID, "state": "actor_spawned", "entry_class_path": SERVICE_CLASS_PATH,
            "actor_observation": {"state": "alive"},
            "actor": {"entry_class_path": SERVICE_CLASS_PATH,
                      "object": {"handle": "obj-17", "class": SERVICE_CLASS}}}


def function_metadata(name, owner=SERVICE_CLASS, flags=0x04000000, token="sig-2"):
    return {"name": name, "owner": owner, "flags": flags,
            "signature_token": token, "signature_status": "complete"}


def complete_page(items):
    return {"items": items, "scan_complete": True, "scan_truncated": False,
            "next_offset": None}


class NativeFixture:
    def __init__(self, *, status=None, pages=None, invoke_error=None):
        self.status = actor_status() if status is None else status
        self.pages = pages or [complete_page([function_metadata("ReadWidgetText")])]
        self.invoke_error = invoke_error
        self.calls = []
        self.page = 0

    def __call__(self, operation, params, timeout, target):
        self.calls.append((operation, deepcopy(params), timeout, deepcopy(target)))
        if operation == "blueprint_mod_status":
            return deepcopy(self.status)
        if operation == "functions":
            result = deepcopy(self.pages[self.page])
            self.page += 1
            return result
        if operation == "invoke":
            if self.invoke_error is not None:
                raise self.invoke_error
            return {"executed": True, "result": "service result"}
        if operation == "blueprint_mod_disable":
            return {"mod_id": SERVICE_ID, "state": "disabled", "desired_enabled": False}
        raise AssertionError(operation)


class ServiceModTests(unittest.TestCase):
    def test_disable_validates_actual_class_without_blocking_not_tracked_noop(self):
        foreign_actor = actor_status()
        foreign_actor["actor"]["entry_class_path"] = "/OtherPlugin/BP_DevelopmentServices.BP_DevelopmentServices_C"
        foreign_record = actor_status()
        foreign_record["entry_class_path"] = foreign_actor["actor"]["entry_class_path"]
        foreign_record["actor"] = None
        for status in (foreign_actor, foreign_record):
            with self.subTest(status=status):
                native = NativeFixture(status=status)
                with self.assertRaises(ServiceModError) as caught:
                    ServiceMod(native).disable()
                self.assertEqual(caught.exception.code, "service_actor_identity_mismatch")
                self.assertFalse(caught.exception.details["executed"])
                self.assertEqual([call[0] for call in native.calls], ["blueprint_mod_status"])
        for status in (actor_status(), {"mod_id": SERVICE_ID, "state": "not_tracked"},
                       {"mod_id": SERVICE_ID, "state": "disabled", "actor": None,
                        "entry_class_path": SERVICE_CLASS_PATH}):
            with self.subTest(status=status):
                native = NativeFixture(status=status)
                ServiceMod(native).disable()
                self.assertEqual([call[0] for call in native.calls],
                                 ["blueprint_mod_status", "blueprint_mod_disable"])

    def test_invalid_input_reports_no_invocation_effects(self):
        cases = (("call", ("",), "invalid_service_function"),
                 ("call", ("ReadWidgetText", []), "invalid_service_arguments"),
                 ("enable", ("",), "invalid_package_revision"),
                 ("enable", ("pkg-6", ""), "invalid_source_revision"))
        for method, arguments, code in cases:
            with self.subTest(code=code):
                native = NativeFixture()
                with self.assertRaises(ServiceModError) as caught:
                    getattr(ServiceMod(native), method)(*arguments)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details, {
                    "executed": False, "may_have_executed": False,
                    "process_event_returned": False,
                })
                self.assertEqual(native.calls, [])

    def test_unavailable_actor_does_not_inspect_or_invoke(self):
        invalid = []
        invalid.append({"mod_id": SERVICE_ID, "state": "not_tracked"})
        for field, value in (("state", "destroy_pending"),):
            status = actor_status()
            status[field] = value
            invalid.append(status)
        status = actor_status()
        status["actor_observation"]["state"] = "unknown"
        invalid.append(status)
        status = actor_status()
        status["actor"]["object"]["class"] = "Actor"
        invalid.append(status)
        status = actor_status()
        status["actor"]["entry_class_path"] = "/OtherPlugin/BP_DevelopmentServices.BP_DevelopmentServices_C"
        invalid.append(status)
        status = actor_status()
        del status["actor"]["entry_class_path"]
        invalid.append(status)
        for status in invalid:
            with self.subTest(status=status):
                native = NativeFixture(status=status)
                with self.assertRaises(ServiceModError) as caught:
                    ServiceMod(native).call("ReadWidgetText")
                self.assertEqual(caught.exception.code, "service_actor_unavailable")
                self.assertEqual([call[0] for call in native.calls], ["blueprint_mod_status"])

    def test_exact_own_callable_function_uses_current_signature_and_target(self):
        native = NativeFixture(pages=[complete_page([
            function_metadata("ReadWidgetText", owner="Actor", token="sig-inherited"),
            function_metadata("ReadWidgetTextExtra", token="sig-substring"),
            function_metadata("ReadWidgetText", flags=0, token="sig-not-callable"),
            function_metadata("ReadWidgetText", token="sig-own"),
        ])])
        target = {"pid": 42, "creation_time": "123"}
        arguments = {"Widget": {"handle": "obj-9"}}
        result = ServiceMod(native).call("ReadWidgetText", arguments, 2.5, target)
        self.assertTrue(result["invocation"]["executed"])
        self.assertEqual(native.calls[-1], ("invoke", {
            "handle": "obj-17", "function": "ReadWidgetText", "arguments": arguments,
            "expected_signature_token": "sig-own",
        }, 2.5, target))
        self.assertEqual(native.calls[1][1]["function_query"], "ReadWidgetText")

    def test_inherited_or_substring_match_cannot_be_called(self):
        for items in ([function_metadata("ReadWidgetText", owner="Actor")],
                      [function_metadata("ReadWidgetTextExtra")],
                      [function_metadata("ReadWidgetText", flags=0)]):
            with self.subTest(items=items):
                native = NativeFixture(pages=[complete_page(items)])
                with self.assertRaises(ServiceModError) as caught:
                    ServiceMod(native).call("ReadWidgetText")
                self.assertEqual(caught.exception.code, "service_function_unavailable")
                self.assertNotIn("invoke", [call[0] for call in native.calls])

    def test_duplicate_own_name_is_not_ambiguously_invoked(self):
        native = NativeFixture(pages=[complete_page([
            function_metadata("ReadWidgetText", token="sig-one"),
            function_metadata("ReadWidgetText", token="sig-two"),
        ])])
        with self.assertRaises(ServiceModError) as caught:
            ServiceMod(native).call("ReadWidgetText")
        self.assertEqual(caught.exception.code, "service_function_unavailable")
        self.assertNotIn("invoke", [call[0] for call in native.calls])

    def test_status_follows_native_offsets_without_an_output_cap(self):
        pages = []
        for page in range(4):
            items = [function_metadata(f"ServiceFunction{page * 64 + n}") for n in range(64)]
            if page == 3:
                pages.append(complete_page(items))
            else:
                pages.append({"items": items, "scan_complete": False,
                              "scan_truncated": False, "next_offset": (page + 1) * 64})
        native = NativeFixture(pages=pages)
        result = ServiceMod(native).status()
        self.assertEqual(len(result["functions"]["items"]), 256)
        self.assertTrue(result["functions"]["scan_complete"])
        self.assertEqual([call[1]["offset"] for call in native.calls if call[0] == "functions"],
                         [0, 64, 128, 192])
        self.assertFalse(result["function_behavior_checked"])

    def test_incomplete_native_scan_does_not_invoke_a_partial_match(self):
        for next_offset, scan_truncated in ((None, True), (0, False), (False, False)):
            with self.subTest(next_offset=next_offset, scan_truncated=scan_truncated):
                native = NativeFixture(pages=[{
                    "items": [function_metadata("ReadWidgetText")], "scan_complete": False,
                    "scan_truncated": scan_truncated, "next_offset": next_offset,
                }])
                with self.assertRaises(ServiceModError) as caught:
                    ServiceMod(native).call("ReadWidgetText")
                self.assertEqual(caught.exception.code, "service_function_scan_incomplete")
                self.assertNotIn("invoke", [call[0] for call in native.calls])

    def test_uncertain_native_effect_is_propagated_without_retry(self):
        error = NativeError("response_lost", "Response lost after request", may_have_executed=True,
                            request_id="request-3", details={"process_event_started": True})
        native = NativeFixture(invoke_error=error)
        with self.assertRaises(NativeError) as caught:
            ServiceMod(native).call("ReadWidgetText")
        self.assertIs(caught.exception, error)
        self.assertTrue(caught.exception.may_have_executed)
        self.assertEqual(caught.exception.details, {"process_event_started": True})
        self.assertEqual([call[0] for call in native.calls].count("invoke"), 1)


if __name__ == "__main__":
    unittest.main()
