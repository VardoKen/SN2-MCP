"""Reproduce typed MCP acceptance against an already enabled selected game.

The runner resolves fresh objects and reflected signature tokens, calls neutral
engine calculations/getters, and retains every request/result. It never launches,
loads, enables, restarts or stops the game. Creating a textual FName can add that
name to the process-local engine pool. Empty container receipts are explicitly
separated from populated element coverage. No failed call is replayed.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class AcceptanceFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def near(actual, expected):
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), "The value fields differ")
        for key in expected:
            near(actual[key], expected[key])
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), "The value sequence differs")
        for got, wanted in zip(actual, expected):
            near(got, wanted)
    elif isinstance(expected, float):
        require(isinstance(actual, (int, float)) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6),
                f"The numeric result differs: {actual!r} != {expected!r}")
    else:
        require(actual == expected and (not isinstance(expected, bool) or type(actual) is bool) and
                (type(expected) is not int or type(actual) is int),
                f"The result differs: {actual!r} != {expected!r}")


def array_values(envelope):
    require(isinstance(envelope, dict) and envelope.get("partial") is False, "The array copy is partial")
    items = envelope.get("items")
    require(isinstance(items, list) and envelope.get("total_count") == len(items), "The array copy is incomplete")
    require(all(item.get("supported") is True for item in items), "An array element is unsupported")
    return [item["value"] for item in items]


class Probe:
    def __init__(self, client, output):
        self.client = client
        self.output = output
        self.sequence = 0
        self.objects = {}
        self.signatures = {}
        self.results = []

    async def tool(self, tool, arguments, *, expected_error=None):
        self.sequence += 1
        response = await self.client.call_tool(tool, arguments)
        record = {"request": {"tool": tool, "arguments": arguments},
                  "response": response.model_dump(mode="json")}
        receipt = self.output / f"{self.sequence:04d}.json"
        receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if response.is_error:
            if expected_error is not None:
                messages = [block.text for block in response.content if block.type == "text"]
                require(len(messages) == 1 and "{" in messages[0], "The rejection has no structured error")
                error = json.loads(messages[0][messages[0].index("{"):])
                require(error.get("code") == expected_error, f"Unexpected rejection code: {error.get('code')}")
                require(error.get("may_have_executed") is False and error.get("effect", {}).get("executed") is False,
                        "The rejected call has an uncertain ProcessEvent effect")
                return error
            raise AcceptanceFailure(f"{tool} failed; inspect {receipt.name}: " +
                                    " ".join(block.text for block in response.content if block.type == "text"))
        require(expected_error is None, "The expected rejection did not occur")
        require(isinstance(response.structured_content, dict), f"{tool} returned no structured object")
        return response.structured_content

    async def exact(self, path):
        if path not in self.objects:
            result = await self.tool("sn2_find_objects", {"exact_path": path, "limit": 2})
            require(len(result["items"]) == 1, f"Expected one object at {path}")
            self.objects[path] = result["items"][0]["handle"]
        return self.objects[path]

    async def signature(self, handle, name):
        key = (handle, name)
        if key not in self.signatures:
            result = await self.tool("sn2_object_functions", {"handle": handle, "function_query": name, "limit": 100})
            matches = [item for item in result["items"] if item["name"] == name]
            require(len(matches) == 1, f"Expected one reflected function {name}")
            require(matches[0].get("signature_status") == "complete", f"The signature is unsupported: {name}")
            self.signatures[key] = matches[0]
        return self.signatures[key]

    async def invoke(self, handle, name, arguments, *, owned=False):
        signature = await self.signature(handle, name)
        result = await self.tool("sn2_invoke", {"handle": handle, "function": name,
                                                "arguments": arguments,
                                                "expected_signature_token": signature["signature_token"]})
        require(result.get("executed") is True and result.get("process_event_returned") is True,
                f"ProcessEvent did not return for {name}")
        if owned:
            require(result.get("cleanup_confirmed") is True, f"Owned cleanup is unconfirmed for {name}")
        return result

    async def case(self, label, action, *, scope="selected_shape"):
        first = self.sequence + 1
        try:
            details = await action()
            result = {"case": label, "state": "passed", "scope": scope,
                      "receipts": {"first": first, "last": self.sequence}, "details": details}
        except Exception as error:
            result = {"case": label, "state": "failed", "scope": scope,
                      "receipts": {"first": first, "last": self.sequence},
                      "error": f"{type(error).__name__}: {error}"}
        self.results.append(result)
        print(json.dumps({"case": label, "state": result["state"], "scope": scope}, ensure_ascii=False), flush=True)


async def run(args):
    workspace = args.workspace_root.resolve()
    output = args.output.resolve()
    require(not output.exists(), "Choose a new output directory; existing receipts are preserved")
    output.mkdir(parents=True)
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace),
        "--runtime-root", str(args.runtime_root.resolve()), "--game-root", str(args.game_root.resolve()),
        "--native-port", str(args.native_port)], cwd=str(workspace))
    summary = {"started_utc": datetime.now(timezone.utc).isoformat(), "scope": "typed_runtime_acceptance",
               "game_launch": False, "native_loading": False, "automatic_retries": False,
               "requested_process": {"pid": args.expected_pid, "creation_time": args.expected_creation_filetime},
               "coverage_gaps": [], "connection_closed": False}
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            await client.initialize()
            catalog = await client.list_tools()
            (output / "catalog.json").write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
            require(len(catalog.tools) == 22, "The registered initial runtime catalog differs")
            probe = Probe(client, output)
            processes = await probe.tool("sn2_target_processes", {})
            matches = [p for p in processes["processes"] if p["pid"] == args.expected_pid and
                       p["creation_time"] == args.expected_creation_filetime]
            require(len(matches) == 1, "The selected process identity is absent")
            status = await probe.tool("sn2_native_status", {"target_process": matches[0]})
            require(status.get("executor_ready") is True, "The selected native game executor is not ready")
            summary["initial_native_status"] = status
            paths = {"system": "/Script/Engine.Default__KismetSystemLibrary",
                     "string": "/Script/Engine.Default__KismetStringLibrary",
                     "math": "/Script/Engine.Default__KismetMathLibrary",
                     "gameplay": "/Script/Engine.Default__GameplayStatics",
                     "mesh": "/Script/ProceduralMeshComponent.Default__KismetProceduralMeshLibrary",
                     "niagara": "/Script/Niagara.Default__NiagaraDataInterfaceArrayFunctionLibrary"}
            handles = {key: await probe.exact(path) for key, path in paths.items()}

            async def value(owner, name, arguments, expected, *, owned=False):
                result = await probe.invoke(handles[owner], name, arguments, owned=owned)
                near(result["result"], expected)
                return {"result": result["result"]}

            await probe.case("int32_boundary_return", lambda: value("math", "Add_IntInt", {"A": 2147483646, "B": 1}, 2147483647))
            await probe.case("int64_boundary_return", lambda: value("math", "Add_Int64Int64", {"A": 9223372036854775806, "B": 1}, 9223372036854775807))
            await probe.case("bool_input_return", lambda: value("math", "BooleanXOR", {"A": True, "B": False}, True))
            await probe.case("byte_return", lambda: value("math", "Conv_BoolToByte", {"InBool": True}, 1))
            await probe.case("byte_input_string_return", lambda: value("string", "Conv_ByteToString", {"InByte": 255}, "255", owned=True))
            await probe.case("class_null_return", lambda: value("system", "IsValidClass", {"Class": None}, False))

            async def valid_class():
                handle = await probe.exact("/Script/Engine.Actor")
                result = await probe.invoke(handles["system"], "IsValidClass", {"Class": {"handle": handle}})
                near(result["result"], True)
                return {"class_reference_accepted": True}

            await probe.case("class_reference_input_return", valid_class)
            await probe.case("fname_unicode_input", lambda: value("string", "Conv_NameToString", {"InName": {"name": "SN2MCP_Acceptance_名字"}}, "SN2MCP_Acceptance_名字", owned=True))
            await probe.case("fname_empty_input", lambda: value("string", "Conv_NameToString", {"InName": {"name": ""}}, "None", owned=True))
            await probe.case("fname_none_input", lambda: value("string", "Conv_NameToString", {"InName": {"name": "None"}}, "None", owned=True))
            await probe.case("fname_return", lambda: value("string", "Conv_StringToName", {"InString": "SN2MCP_Acceptance_名字"}, "SN2MCP_Acceptance_名字", owned=True))
            await probe.case("fstring_unicode_return", lambda: value("string", "Replace", {
                "SourceString": "alpha名字 Ω", "From": "alpha", "To": "", "SearchCase": 0}, "名字 Ω", owned=True))
            await probe.case("fstring_empty_return", lambda: value("string", "Replace", {
                "SourceString": "", "From": "|", "To": "", "SearchCase": 0}, "", owned=True))

            async def split():
                result = await probe.invoke(handles["string"], "Split", {"SourceString": "alpha|名字", "InStr": "|",
                    "LeftS": {"out": True}, "RightS": {"out": True}, "SearchCase": 0, "SearchDir": 0}, owned=True)
                near(result["result"], True)
                near(result["out_parameters"], {"LeftS": "alpha", "RightS": "名字"})
                return {"outputs": result["out_parameters"]}

            await probe.case("fstring_pure_outputs_enum_inputs", split)

            async def rejected_nul():
                signature = await probe.signature(handles["string"], "JoinStringArray")
                result = await probe.tool("sn2_invoke", {"handle": handles["string"], "function": "JoinStringArray",
                    "arguments": {"SourceArray": ["alpha", "bad\u0000text"], "Separator": "|"},
                    "expected_signature_token": signature["signature_token"]}, expected_error="invalid_argument")
                return {"rejection": result["code"], "process_event_started": False}

            await probe.case("embedded_nul_rejected_before_execution", rejected_nul)
            vector = {"X": 1.25, "Y": -2.5, "Z": 3.0}
            await probe.case("vector_return_double_inputs", lambda: value("math", "MakeVector", vector, vector))
            await probe.case("vector_const_reference_input", lambda: value("math", "Vector_IsZero", {"A": {"X": 0, "Y": 0, "Z": 0}}, True))

            async def break_vector():
                result = await probe.invoke(handles["math"], "BreakVector", {"InVec": vector,
                    "X": {"out": True}, "Y": {"out": True}, "Z": {"out": True}})
                near(result["out_parameters"], vector)
                return {"outputs": result["out_parameters"]}

            await probe.case("vector_value_input_scalar_outputs", break_vector)

            async def vector_output():
                result = await probe.invoke(handles["string"], "Conv_StringToVector", {"InString": "X=1.25 Y=-2.5 Z=3",
                    "OutConvertedVector": {"out": True}, "OutIsValid": {"out": True}}, owned=True)
                near(result["out_parameters"], {"OutConvertedVector": vector, "OutIsValid": True})
                return {"outputs": result["out_parameters"]}

            await probe.case("vector_pure_output_bool_output", vector_output)
            rotator = {"rotator": {"Pitch": 0, "Yaw": 0, "Roll": 0}}
            await probe.case("rotator_value_input", lambda: value("string", "Conv_RotatorToString", {"InRot": rotator},
                "P=0.000000 Y=0.000000 R=0.000000", owned=True))

            async def rotator_float_outputs():
                result = await probe.invoke(handles["math"], "BreakRotator", {"InRot": {"rotator": {
                    "Pitch": 10.0, "Yaw": 20.0, "Roll": 30.0}},
                    "Roll": {"out": True}, "Pitch": {"out": True}, "Yaw": {"out": True}})
                near(result["out_parameters"], {"Roll": 30.0, "Pitch": 10.0, "Yaw": 20.0})
                return {"outputs": result["out_parameters"]}

            await probe.case("rotator_value_float_outputs", rotator_float_outputs)
            transform = {"Rotation": {"X": 0.0, "Y": 0.0, "Z": 0.0, "W": 1.0},
                         "Translation": {"X": 1.0, "Y": 2.0, "Z": 3.0}, "Scale3D": {"X": 1.0, "Y": 1.0, "Z": 1.0}}
            identity = {**transform, "Translation": {"X": 0.0, "Y": 0.0, "Z": 0.0}}
            await probe.case("rotator_const_reference_transform_return", lambda: value("math", "Conv_RotatorToTransform", {"InRotator": rotator}, identity))
            await probe.case("transform_const_reference_input", lambda: value("math", "TransformLocation", {"T": transform,
                "Location": {"X": 4.0, "Y": 5.0, "Z": 6.0}}, {"X": 5.0, "Y": 7.0, "Z": 9.0}))
            color = {"R": 0.25, "G": 0.5, "B": 0.75, "A": 1.0}
            await probe.case("linearcolor_value_input_return", lambda: value("math", "Multiply_LinearColorFloat", {"A": color, "B": 2.0},
                {"R": 0.5, "G": 1.0, "B": 1.5, "A": 2.0}))
            await probe.case("nested_dto_value_input_return", lambda: value("math", "MakeQualifiedFrameTime", {"Frame": {"Value": 2147483647},
                "FrameRate": {"Numerator": 24, "Denominator": 1}, "SubFrame": 0.5},
                {"Time": {"FrameNumber": {"Value": 2147483647}, "SubFrame": 0.5}, "Rate": {"Numerator": 24, "Denominator": 1}}))

            async def date_output():
                result = await probe.invoke(handles["math"], "DateTimeFromIsoString", {"IsoString": "2026-11-10T00:00:00Z", "Result": {"out": True}}, owned=True)
                near(result["result"], True)
                near(result["out_parameters"], {"Result": {"Ticks": 639298656000000000}})
                return {"outputs": result["out_parameters"]}

            await probe.case("scalar_dto_pure_output", date_output)
            await probe.case("uint64_nested_dto_input", lambda: value("system", "K2_IsValidTimerHandle", {
                "Handle": {"Handle": 18446744073709551615}}, True))

            async def min_array():
                result = await probe.invoke(handles["math"], "MinOfIntArray", {"IntArray": [-2147483648, 0, 2147483647],
                    "IndexOfMinValue": {"out": True}, "MinValue": {"out": True}}, owned=True)
                near(result["out_parameters"], {"IndexOfMinValue": 0, "MinValue": -2147483648})
                return {"outputs": result["out_parameters"]}

            await probe.case("int_array_input_scalar_outputs", min_array)

            async def triangles():
                result = await probe.invoke(handles["mesh"], "CreateGridMeshTriangles", {"NumX": 2, "NumY": 2,
                    "bWinding": False, "Triangles": {"out": True}}, owned=True)
                values = array_values(result["out_parameters"]["Triangles"])
                near(values, [0, 1, 2, 1, 3, 2])
                return {"elements": len(values)}

            await probe.case("int_array_pure_output", triangles)

            async def empty_niagara(name):
                result = await probe.invoke(handles["niagara"], name, {"NiagaraSystem": None,
                    "OverrideName": {"name": "SN2MCP_Acceptance_Array"}}, owned=True)
                near(array_values(result["result"]), [])
                return {"elements": 0, "element_decoding_exercised": False}

            await probe.case("int_array_empty_return", lambda: empty_niagara("GetNiagaraArrayInt32"), scope="empty_container_only")
            await probe.case("dto_array_empty_return", lambda: empty_niagara("GetNiagaraArrayVector4"), scope="empty_container_only")
            await probe.case("string_array_const_reference_input", lambda: value("string", "JoinStringArray", {
                "SourceArray": ["alpha", "名字", "", "alpha"], "Separator": "|"}, "alpha|名字||alpha", owned=True))
            await probe.case("string_array_empty_input", lambda: value("string", "JoinStringArray", {
                "SourceArray": [], "Separator": ""}, "", owned=True))

            async def parse_strings():
                result = await probe.invoke(handles["string"], "ParseIntoArray", {"SourceString": "alpha|名字||alpha",
                    "Delimiter": "|", "CullEmptyStrings": False}, owned=True)
                near(array_values(result["result"]), ["alpha", "名字", "", "alpha"])
                return {"elements": 4}

            await probe.case("string_array_populated_return", parse_strings)

            async def cull_output():
                result = await probe.invoke(handles["string"], "CullArray", {"SourceString": "", "inArray": {"out": True}}, owned=True)
                near(array_values(result["out_parameters"]["inArray"]), [])
                return {"elements": 0, "element_decoding_exercised": False}

            await probe.case("string_array_empty_pure_output", cull_output, scope="empty_container_only")
            missing_id = {"PrimaryAssetType": {"Name": {"name": "SN2MCP_Acceptance_MissingType"}},
                          "PrimaryAssetName": {"name": "SN2MCP_Acceptance_MissingAsset"}}
            await probe.case("nested_name_dto_value_input", lambda: value("system", "Conv_PrimaryAssetIdToString", {
                "PrimaryAssetId": missing_id}, "SN2MCP_Acceptance_MissingType:SN2MCP_Acceptance_MissingAsset", owned=True))

            async def names_output():
                result = await probe.invoke(handles["system"], "GetCurrentBundleState", {"PrimaryAssetId": missing_id,
                    "bForceCurrentState": True, "OutBundles": {"out": True}}, owned=True)
                near(result["result"], False)
                near(array_values(result["out_parameters"]["OutBundles"]), [])
                return {"elements": 0, "element_decoding_exercised": False}

            await probe.case("nested_name_dto_input_name_array_output", names_output, scope="empty_output_container")

            async def names_and_dtos():
                result = await probe.invoke(handles["system"], "GetPrimaryAssetsWithBundleState", {
                    "RequiredBundles": [{"name": "SN2MCP_Acceptance_MissingBundle"}, {"name": "SN2MCP_Acceptance_MissingBundle"}],
                    "ExcludedBundles": [], "ValidTypes": [{"Name": {"name": "SN2MCP_Acceptance_MissingType"}}],
                    "bForceCurrentState": True, "OutPrimaryAssetIdList": {"out": True}}, owned=True)
                near(array_values(result["out_parameters"]["OutPrimaryAssetIdList"]), [])
                return {"input_name_elements": 2, "input_dto_elements": 1, "output_elements": 0}

            await probe.case("name_array_dto_array_inputs_output", names_and_dtos, scope="populated_inputs_empty_output")

            async def mesh_arrays():
                result = await probe.invoke(handles["mesh"], "GenerateBoxMesh", {"BoxRadius": {"X": 1, "Y": 2, "Z": 3},
                    **{name: {"out": True} for name in ("Vertices", "Triangles", "Normals", "UVs", "Tangents")}}, owned=True)
                arrays = {name: array_values(value) for name, value in result["out_parameters"].items()}
                require(len(arrays["Triangles"]) == 36 and len(arrays["Vertices"]) == 24 and
                        len(arrays["Tangents"]) == 24, "The generated box arrays differ")
                require(all(set(item) == {"TangentX", "bFlipTangentY"} for item in arrays["Tangents"]),
                        "The nested tangent DTO fields differ")
                return {"counts": {name: len(values) for name, values in arrays.items()}}

            await probe.case("nested_dto_array_populated_outputs", mesh_arrays)

            async def mesh_array_inputs():
                result = await probe.invoke(handles["mesh"], "CalculateTangentsForMesh", {
                    "Vertices": [{"X": 0, "Y": 0, "Z": 0}, {"X": 1, "Y": 0, "Z": 0}, {"X": 0, "Y": 1, "Z": 0}],
                    "Triangles": [0, 1, 2], "UVs": [{"X": 0, "Y": 0}, {"X": 1, "Y": 0}, {"X": 0, "Y": 1}],
                    "Normals": {"out": True}, "Tangents": {"out": True}}, owned=True)
                normals = array_values(result["out_parameters"]["Normals"])
                tangents = array_values(result["out_parameters"]["Tangents"])
                require(len(normals) == len(tangents) == 3, "The triangle outputs differ")
                require(all(abs(abs(item["Z"]) - 1.0) < 1e-6 for item in normals), "The triangle normals differ")
                return {"normals": len(normals), "nested_tangents": len(tangents)}

            await probe.case("struct_array_inputs_nested_dto_outputs", mesh_array_inputs)

            async def unsigned_property():
                handle = await probe.exact("/Script/UWESculpturalBase.Default__UWESculpturalBaseModuleSettings")
                result = await probe.tool("sn2_object_properties", {"handle": handle,
                    "property_names": ["MaxGlobalCellCount"]})
                require(len(result["items"]) == 1 and result["items"][0]["type"] == "UInt64Property" and
                        result["items"][0].get("supported") is True, "The UInt64 property is unavailable")
                require(type(result["items"][0]["value"]) is int and result["items"][0]["value"] >= 0,
                        "The UInt64 property is not an exact nonnegative integer")
                snapshot = await probe.tool("sn2_snapshot", {"targets": [{"handle": handle,
                    "properties": ["MaxGlobalCellCount"]}]})
                require(snapshot.get("partial") is False, "The UInt64 snapshot is partial")
                near(snapshot["objects"][0]["properties"][0]["value"], result["items"][0]["value"])
                return {"property_type": "UInt64Property", "snapshot_copied": True}

            await probe.case("uint64_property_snapshot", unsigned_property)

            async def primitive_property_array(path):
                handle = await probe.exact(path)
                metadata = await probe.tool("sn2_object_properties", {"handle": handle, "limit": 100})
                fields = [item for item in metadata["items"] if item["type"] == "ArrayProperty"]
                require(len(fields) == 1, "Expected one reflected data array in the selected Niagara interface")
                property_name = fields[0]["name"]
                result = await probe.tool("sn2_read_array", {"handle": handle, "property_name": property_name,
                                                            "offset": 0, "limit": 16})
                require(result.get("partial") is False, "The primitive array property read is partial")
                return {"property_name": property_name, "total_count": result["total_count"], "element_type": result["element_type"],
                        "element_decoding_exercised": bool(result["items"])}

            for suffix in ("Int32", "Float", "Bool"):
                path = f"/Script/Niagara.Default__NiagaraDataInterfaceArray{suffix}"
                await probe.case(f"{suffix.lower()}_array_property_read", lambda p=path:
                    primitive_property_array(p), scope="current_property_contents")

            engine_result = await probe.tool("sn2_find_objects", {"class_query": "GameEngine", "limit": 100})
            engine = [x for x in engine_result["items"] if x["path"].startswith("/Engine/Transient.")]
            require(len(engine) == 1, "Expected one live GameEngine")
            properties = await probe.tool("sn2_object_properties", {"handle": engine[0]["handle"], "property_names": ["GameViewport"]})
            viewport = properties["items"][0]["value"]["handle"]
            properties = await probe.tool("sn2_object_properties", {"handle": viewport, "property_names": ["World"]})
            world = properties["items"][0]["value"]
            summary["selected_world"] = world

            async def unsigned32_snapshot():
                properties = await probe.tool("sn2_object_properties", {"handle": engine[0]["handle"],
                    "property_names": ["GlobalNetTravelCount"]})
                require(len(properties["items"]) == 1 and properties["items"][0]["type"] == "UInt32Property" and
                        properties["items"][0]["supported"] is True, "The UInt32 property is unavailable")
                snapshot = await probe.tool("sn2_snapshot", {"targets": [{"handle": engine[0]["handle"],
                    "properties": ["GlobalNetTravelCount"]}]})
                require(snapshot["partial"] is False, "The UInt32 snapshot is partial")
                near(snapshot["objects"][0]["properties"][0]["value"], properties["items"][0]["value"])
                return {"property_type": "UInt32Property", "snapshot_copied": True}

            await probe.case("uint32_property_snapshot", unsigned32_snapshot)
            components = await probe.tool("sn2_find_objects", {"class_query": "StaticMeshComponent",
                "query": world["path"] + ".PersistentLevel.", "limit": 1000})
            selected = [x for x in components["items"] if x["path"].startswith(world["path"] + ".PersistentLevel.")]
            if selected:
                component = selected[0]

                async def incompatible_class():
                    signature = await probe.signature(handles["system"], "IsValidClass")
                    result = await probe.tool("sn2_invoke", {"handle": handles["system"], "function": "IsValidClass",
                        "arguments": {"Class": {"handle": component["handle"]}},
                        "expected_signature_token": signature["signature_token"]}, expected_error="invalid_class_argument")
                    return {"rejection": result["code"], "process_event_started": False}

                await probe.case("object_rejected_as_class", incompatible_class)

                async def object_class_return():
                    result = await probe.invoke(handles["gameplay"], "GetObjectClass", {"Object": {"handle": component["handle"]}})
                    require(result["result"]["class"] == "Class" and isinstance(result["result"]["handle"], str),
                            "The object class reference was not returned")
                    return {"class_returned": True}

                await probe.case("object_input_class_return", object_class_return)

                async def object_inputs_outputs():
                    result = await probe.invoke(handles["system"], "GetActorListFromComponentList", {
                        "ComponentList": [None, {"handle": component["handle"]}, {"handle": component["handle"]}, None],
                        "ActorClassFilter": None, "OutActorList": {"out": True}}, owned=True)
                    values = array_values(result["out_parameters"]["OutActorList"])
                    require(len(values) == 1 and values[0] is not None, "The owner list did not deduplicate the selected component")
                    return {"input_elements": 4, "output_elements": len(values)}

                await probe.case("object_array_const_reference_input_pure_output", object_inputs_outputs)

                async def materials():
                    result = await probe.invoke(component["handle"], "GetMaterials", {}, owned=True)
                    values = array_values(result["result"])
                    require(all(value is None or isinstance(value.get("handle"), str) for value in values), "Invalid material references")
                    if not values:
                        summary["coverage_gaps"].append("The selected mesh returned no material elements")
                    return {"elements": len(values), "element_decoding_exercised": bool(values)}

                await probe.case("object_array_return", materials)

                async def slot_names():
                    result = await probe.invoke(component["handle"], "GetMaterialSlotNames", {}, owned=True)
                    values = array_values(result["result"])
                    require(all(isinstance(value, str) for value in values), "Invalid material slot names")
                    if not values:
                        summary["coverage_gaps"].append("The selected mesh returned no material slot names")
                    return {"elements": len(values), "element_decoding_exercised": bool(values)}

                await probe.case("name_array_return", slot_names)
            else:
                summary["coverage_gaps"].append("No live StaticMeshComponent was found in the selected world")
            summary["coverage_gaps"].extend([
                "No Transform value-input signature was present in the inspected engine calculations; const-reference input is exercised",
                "Empty int32/Vector4-array returns do not establish populated element decoding",
                "Empty FString/FName-array pure outputs do not establish populated output element decoding",
                "This runner does not replace property, sparse-container, lifecycle or isolated target-fixture acceptance",
            ])
            summary["final_native_status"] = await probe.tool("sn2_native_status", {})
            summary["cases"] = probe.results
    summary["connection_closed"] = True
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    summary["passed"] = sum(case["state"] == "passed" for case in summary["cases"])
    summary["failed"] = sum(case["state"] == "failed" for case in summary["cases"])
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"],
                      "connection_closed": True, "summary": str(output / "summary.json")}), flush=True)
    return 0 if summary["failed"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="Verify selected typed MCP shapes against an already enabled game")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--expected-creation-filetime", required=True)
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(args.expected_pid > 0 and args.expected_creation_filetime.isdecimal(), "Select an exact positive PID and FILETIME")
    if args.output.exists():
        print(json.dumps({"state": "failed", "error": "Choose a new output directory; existing receipts are preserved"}),
              file=sys.stderr)
        return 1
    try:
        return asyncio.run(run(args))
    except Exception as error:
        failure = {"state": "failed", "phase": "setup_or_session", "automatic_retries": False,
                   "error": f"{type(error).__name__}: {error}"}
        if args.output.is_dir():
            (args.output / "runner-error.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
