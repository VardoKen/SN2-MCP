"""Run isolated target-Tick decoder fixtures from an explicit development DLL.

Requires an already loaded/ready development-probes build. Does not launch or
load a game, replace live properties, or retry effects. Receipts distinguish
engine-allocated synthetic values from naturally populated object properties.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.native_client import NativeClient


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    count = 0
    with NativeClient(port=args.native_port, timeout=20) as native:
        status = native.request("status")
        assert status["process_id"] == args.pid and status["executor_ready"]
        identity = {"expected_process_id": args.pid,
                    "expected_creation_filetime": status["process_creation_filetime"],
                    "expected_session_id": status["session_id"]}

        def call(operation, parameters):
            nonlocal count
            count += 1
            result = native.request(operation, {**parameters, **identity}, timeout=20)
            (args.output / f"{count:04d}-{operation}.json").write_text(
                json.dumps({"request": parameters, "result": result}, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        def find(path):
            result = call("objects", {"exact_path": path, "limit": 1})
            assert len(result["items"]) == 1, path
            return result["items"][0]["handle"]

        settings = find("/Script/Engine.Default__AssetManagerSettings")
        widget = find("/Script/UMG.Default__WidgetTree")
        reference = find("/Script/UMG.Default__UserWidget")
        library = find("/Script/Engine.Default__KismetSystemLibrary")
        for is_map, handle, field in [(False, settings, "MetaDataTagsForAssetRegistry"),
                                      (True, widget, "NamedSlotBindings")]:
            pages = []
            for offset, limit in [(0, 2), (2, 2), (3, 2), (0, 10)]:
                result = call("_development_sparse_fixture", {
                    "handle": handle, "property_name": field, "map": is_map,
                    "value_handle": reference, "offset": offset, "limit": limit})
                assert result["source"] == "development_fixture" and not result["partial"]
                assert result["fixture"]["cleanup_confirmed"] and not result["fixture"]["live_object_modified"]
                assert result["total_count"] == 3 and result["max_index"] == 4
                assert all(item["supported"] for item in result["items"])
                pages.append(result)
            merged = pages[0]["items"] + pages[1]["items"]
            assert [item["sparse_index"] for item in merged] == [0, 2, 3]
            assert pages[0]["next_offset"] == 2 and pages[1]["next_offset"] is None
            assert pages[2]["items"] == []
            if is_map:
                assert merged[1]["value"] is None
                assert merged[0]["value"]["handle"] == reference and merged[2]["value"]["handle"] == reference
            else:
                assert [item["value"] for item in merged] == ["SN2MCPFixture0", "SN2MCPFixture1", "SN2MCPFixture2"]
        result = call("_development_uint64_fixture", {
            "handle": library, "function": "K2_IsValidTimerHandle", "parameter": "Handle"})
        assert result["source"] == "development_fixture"
        assert result["values"] == [0, 1, 18446744073709551615]
        array_paths = [
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayInt32", "IntProperty"),
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayUInt8", "ByteProperty"),
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayBool", "BoolProperty"),
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayFloat", "FloatProperty"),
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayFloat4", "StructProperty"),
            ("/Script/Niagara.Default__NiagaraDataInterfaceArrayColor", "StructProperty"),
            ("/Script/Engine.Default__SceneComponent", "NameProperty"),
        ]
        for path, element_type in array_paths:
            array_handle = find(path)
            properties = call("properties", {"handle": array_handle, "limit": 400})
            arrays = [item for item in properties["items"] if item["type"] == "ArrayProperty"]
            selected = None
            for item in arrays:
                page = call("read_array", {"handle": array_handle, "property_name": item["name"], "limit": 1})
                if page["element_type"] == element_type:
                    selected = item["name"]
                    break
            assert selected is not None, path
            pages = [call("_development_array_fixture", {"handle": array_handle,
                     "property_name": selected, "offset": offset, "limit": 2}) for offset in (0, 2, 3)]
            assert all(page["source"] == "development_fixture" and not page["partial"] for page in pages)
            assert all(page["fixture"]["cleanup_confirmed"] and not page["fixture"]["live_object_modified"] for page in pages)
            assert [len(page["items"]) for page in pages] == [2, 1, 0]
            assert pages[0]["next_offset"] == 2 and pages[1]["next_offset"] is None
            assert all(item["supported"] for page in pages for item in page["items"])
        owned_arrays = find("/Script/Niagara.Default__NiagaraDataInterfaceArrayFunctionLibrary")
        strings = find("/Script/Engine.Default__KismetStringLibrary")
        owned_cases = [(owned_arrays, "GetNiagaraArrayInt32", "ReturnValue"),
                       (owned_arrays, "GetNiagaraArrayVector4", "ReturnValue"),
                       (library, "GetCurrentBundleState", "OutBundles"),
                       (strings, "CullArray", "inArray")]
        for owner, function, parameter in owned_cases:
            result = call("_development_owned_output_fixture", {
                "handle": owner, "function": function, "parameter": parameter})
            assert result["source"] == "development_fixture"
            assert result["fixture"]["cleanup_confirmed"] and not result["fixture"]["process_event_called"]
            copied = result["decoded"]
            assert not copied["partial"] and copied["total_count"] == 3
            assert all(item["supported"] for item in copied["items"])
            values = [item["value"] for item in copied["items"]]
            if function == "GetNiagaraArrayInt32":
                assert values == [-2147483648, 0, 2147483647]
            elif function == "GetCurrentBundleState":
                assert values == ["SN2MCPOwned0", "SN2MCPOwned1", "SN2MCPOwned2"]
            elif function == "CullArray":
                assert values == ["SN2 名字", "", "SN2 名字"]
        summary = {"passed": True, "receipts": count, "process_id": args.pid,
                   "native_session": status["session_id"], "live_properties_modified": False,
                   "coverage": "isolated_target_decoder_fixtures", "frontend_tools_added": False}
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
