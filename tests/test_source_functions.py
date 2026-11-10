"""Функциональные проверки лексического индекса AngelScript-функций."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1r_mcp.sources import SourceCatalog


class SourceFunctionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "Script").mkdir()
        self.catalog = SourceCatalog(self.root, revision="function-fixture")

    def write(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = source if isinstance(source, bytes) else source.encode("utf-8")
        path.write_bytes(raw)
        return path

    def hashes(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_full_multiline_prototype_preserves_defaults_templates_and_source_lines(self):
        prototype = '''TArray<TMap<FName, TArray<int>>> Build(
    const TArray<FName>&in Inputs,
    FString&out Result,
    FString Text = "текст ) // /* (",
    int Count = Select(1, Pair(2, 3))
) const'''
        source = '''// FString Commented(int X) {}
namespace Alpha {
namespace Beta {
UFUNCTION()
''' + prototype + '''
{
    Build(Inputs, Result);
    if (Ready()) { Notify(); }
    FString Literal = "void StringFunction() {}";
}
}}
'''
        raw = b"\xef\xbb\xbf" + source.replace("\n", "\r\n").encode("utf-8")
        path = self.write("Script/Multiline.as", raw)
        before = self.hashes()
        result = self.catalog.list_functions()
        self.assertEqual([item["name"] for item in result["items"]], ["Build"])
        item = result["items"][0]
        self.assertEqual(item["prototype"], prototype.replace("\n", "\r\n"))
        self.assertEqual((item["start_line"], item["end_line"]), (5, 10))
        self.assertEqual(item["namespace"], "Alpha::Beta")
        self.assertEqual(item["owner"], "")
        self.assertEqual(item["kind"], "function")
        self.assertEqual(item["path"], "Script/Multiline.as")
        self.assertEqual(item["root_kind"], "game_script")
        self.assertEqual(item["file_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["parser_kind"], "lexical")
        self.assertTrue(result["limitations"])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.hashes(), before)

    def test_owners_mixin_constructors_destructors_and_access_qualified_declarations(self):
        source = '''namespace Alpha::Beta {
class UHost : UObject
{
public:
    UHost(int Seed = 1) {}
    ~UHost() {}
    UFUNCTION()
    protected FString Label(const FString&in Text = "(") const override;
    private:
    void Run() { Label("()"); }
}
mixin class UExtras
{
    mixin void Apply(UHost Host) { Host.Run(); }
}
struct FData
{
    int Count() const { return 1; }
}
}
int Outside() { return 2; }
'''
        self.write("Script/Owners.as", source)
        result = self.catalog.list_functions()
        items = result["items"]
        self.assertEqual([item["name"] for item in items], ["UHost", "~UHost", "Label", "Run", "Apply", "Count", "Outside"])
        self.assertEqual([item["kind"] for item in items[:3]], ["constructor", "destructor", "function"])
        self.assertEqual([item["owner"] for item in items], ["Alpha::Beta::UHost"] * 4 +
                         ["Alpha::Beta::UExtras", "Alpha::Beta::FData", ""])
        self.assertEqual([item["namespace"] for item in items], ["Alpha::Beta"] * 6 + [""])
        self.assertEqual(items[2]["prototype"], 'protected FString Label(const FString&in Text = "(") const override')
        self.assertEqual(items[4]["prototype"], "mixin void Apply(UHost Host)")
        self.assertEqual((items[2]["start_line"], items[2]["end_line"]), (8, 8))
        self.assertEqual(items[-1]["start_line"], 21)

    def test_comments_strings_control_flow_and_function_calls_are_not_declarations(self):
        self.write("Script/FalsePositives.as", '''/*
int BlockFake(int X) { return X; }
*/
FString Text = "FString StringFake() { }";
// void LineFake();
UCLASS() class UCalls : UObject
{
    UFUNCTION()
    void Real()
    {
        FString Inner = "int LocalFake() {}";
        if (IsReady()) { Run(); }
        for (int I = 0; I < Count(); ++I) { Tick(I); }
        while (Poll()) { Yield(); }
        switch (Mode()) { case 0: Break(); }
        External(Select(1, 2));
        return;
    }
}
void Declared(const FString&in Label = "void Nope();");
''')
        items = self.catalog.list_functions()["items"]
        self.assertEqual([item["name"] for item in items], ["Real", "Declared"])
        self.assertEqual(items[0]["prototype"], "void Real()")
        self.assertEqual(items[1]["prototype"], 'void Declared(const FString&in Label = "void Nope();")')
        self.assertEqual([item["start_line"] for item in items], [9, 20])

    def test_query_relative_paths_and_pagination_share_the_same_catalog_order(self):
        self.write("Script/A.as", "void SharedFirst() {}\nvoid Other() {}\n")
        self.write("Script/Sub/B.as", "namespace N { class UOwner { void SharedSecond() {} } }\n")
        self.write("Examples/Sample/Script/C.as", "void SharedExample() {}\n")
        self.write("Examples/Sample/Content/Hidden.as", "void SharedHidden() {}\n")
        self.write("Other/Hidden.as", "void SharedHidden() {}\n")
        before = self.hashes()
        complete = self.catalog.list_functions("shared")
        first = self.catalog.list_functions("SHARED", limit=2)
        second = self.catalog.list_functions("shared", limit=2, offset=first["next_offset"])
        self.assertEqual(first["items"] + second["items"], complete["items"])
        self.assertEqual([item["path"] for item in complete["items"]],
                         ["Examples/Sample/Script/C.as", "Script/A.as", "Script/Sub/B.as"])
        self.assertEqual(first["total_matches"], 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual([item["name"] for item in self.catalog.list_functions(relative_path="Script/Sub")["items"]], ["SharedSecond"])
        self.assertEqual([item["name"] for item in self.catalog.list_functions(relative_path="Script/A.as")["items"]], ["SharedFirst", "Other"])
        self.assertEqual([item["name"] for item in self.catalog.list_functions("uowner")["items"]], ["SharedSecond"])
        self.assertEqual([item["name"] for item in self.catalog.list_functions("void other")["items"]], ["Other"])
        self.assertEqual(self.hashes(), before)

    def test_caller_pagination_and_query_do_not_inherit_legacy_api_caps(self):
        self.write("Script/LargeIndex.as", "\n".join(f"void Entry{index:03}() {{}}" for index in range(300)))
        before = self.hashes()
        first = self.catalog.list_functions(limit=250)
        second = self.catalog.list_functions(limit=250, offset=first["next_offset"])
        self.assertEqual([len(first["items"]), len(second["items"])], [250, 50])
        self.assertEqual([item["name"] for page in (first, second) for item in page["items"]],
                         [f"Entry{index:03}" for index in range(300)])
        self.assertEqual(first["next_offset"], 250)
        self.assertEqual(second["total_matches"], 300)
        self.assertFalse(second["has_more"])
        self.assertEqual(self.catalog.list_functions("x" * 600)["items"], [])
        exhausted = self.catalog.list_functions(offset=1_000_001)
        self.assertEqual(exhausted["items"], [])
        self.assertEqual(exhausted["total_matches"], 300)
        self.assertFalse(exhausted["has_more"])
        self.assertEqual(self.hashes(), before)


if __name__ == "__main__":
    unittest.main()
