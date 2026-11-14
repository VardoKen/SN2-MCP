#include "SN2MCPAuthoringCommandlet.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "Dom/JsonObject.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphToken.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "Engine/DataAsset.h"
#include "GameFramework/Actor.h"
#include "HAL/FileManager.h"
#include "JsonObjectConverter.h"
#include "K2Node_CallArrayFunction.h"
#include "K2Node_CallFunction.h"
#include "K2Node_Event.h"
#include "K2Node_ExecutionSequence.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Logging/TokenizedMessage.h"
#include "Misc/FileHelper.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/PackageName.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/SavePackage.h"
#include "Misc/PackagePath.h"
#include "UObject/SoftObjectPtr.h"
#include "UObject/StrongObjectPtr.h"
#include "UObject/UnrealType.h"

DEFINE_LOG_CATEGORY_STATIC(LogSN2MCPAuthoring, Log, All);

namespace
{
TSharedRef<FJsonObject> Object()
{
    return MakeShared<FJsonObject>();
}

struct FAuthoring
{
    FString PluginId;
    FString PluginRoot;
    FString ContentRoot;
    FString CurrentAsset;
    TArray<TSharedPtr<FJsonValue>> Errors;
    TArray<TSharedPtr<FJsonValue>> AssetReports;
    TArray<TSharedPtr<FJsonValue>> Outputs;
    TArray<TStrongObjectPtr<UObject>> PendingAssets;
    TArray<FString> PendingFilenames;
    TArray<TSharedRef<FJsonObject>> PendingReports;

    bool Fail(const FString& Code, const FString& Message, const FString& Node = {}, const FString& Pin = {})
    {
        TSharedRef<FJsonObject> Error = Object();
        Error->SetStringField(TEXT("code"), Code);
        Error->SetStringField(TEXT("message"), Message);
        if (!CurrentAsset.IsEmpty()) Error->SetStringField(TEXT("asset"), CurrentAsset);
        if (!Node.IsEmpty()) Error->SetStringField(TEXT("node"), Node);
        if (!Pin.IsEmpty()) Error->SetStringField(TEXT("pin"), Pin);
        Errors.Add(MakeShared<FJsonValueObject>(Error));
        UE_LOG(LogSN2MCPAuthoring, Error, TEXT("%s: %s [asset=%s node=%s pin=%s]"),
            *Code, *Message, *CurrentAsset, *Node, *Pin);
        return false;
    }

    bool String(const TSharedPtr<FJsonObject>& Source, const TCHAR* Key, FString& Out, const FString& Node = {})
    {
        if (!Source.IsValid() || !Source->HasTypedField<EJson::String>(Key) || !Source->TryGetStringField(Key, Out) || Out.IsEmpty())
            return Fail(TEXT("invalid_spec"), FString::Printf(TEXT("Nonempty string '%s' is required"), Key), Node);
        return true;
    }

    bool Array(const TSharedPtr<FJsonObject>& Source, const TCHAR* Key, const TArray<TSharedPtr<FJsonValue>>*& Out)
    {
        if (!Source->TryGetArrayField(Key, Out))
            return Fail(TEXT("invalid_spec"), FString::Printf(TEXT("Array '%s' is required"), Key));
        return true;
    }

    UClass* Class(const FString& Path, uint32 LoadFlags = LOAD_None)
    {
        FString Package, Name;
        if (!Path.Split(TEXT("."), &Package, &Name, ESearchCase::CaseSensitive, ESearchDir::FromEnd)
            || !FPackageName::IsValidLongPackageName(Package, true) || Name.IsEmpty()
            || Name.Contains(TEXT("/")) || Name.Contains(TEXT(":")) || Package.Contains(TEXT(".")))
        {
            Fail(TEXT("invalid_class_path"), TEXT("Class path must identify a canonical native or mounted asset class"));
            return nullptr;
        }
        UClass* Result = LoadObject<UClass>(nullptr, *Path, nullptr, LoadFlags);
        if (!Result) Fail(TEXT("class_unavailable"), FString::Printf(TEXT("Class '%s' did not load"), *Path));
        return Result;
    }

    bool ResolveAsset(const FString& Path, FString& PackageName, FString& AssetName, FString& Filename)
    {
        FString Package, Name;
        if (!Path.Split(TEXT("."), &Package, &Name, ESearchCase::CaseSensitive, ESearchDir::FromEnd)
            || Name.IsEmpty() || Package.Contains(TEXT(".")) || Name.Contains(TEXT("/"))
            || !Package.StartsWith(TEXT("/") + PluginId + TEXT("/"))
            || FPackageName::GetLongPackageAssetName(Package) != Name
            || !FPackageName::IsValidLongPackageName(Package, false))
            return Fail(TEXT("invalid_asset_path"), TEXT("Asset must be a canonical top-level path under the selected plugin"));
        FString Relative = Package.Mid(PluginId.Len() + 2);
        Filename = FPaths::ConvertRelativePathToFull(FPaths::Combine(ContentRoot, Relative + TEXT(".uasset")));
        FPaths::NormalizeFilename(Filename);
        if (!Filename.StartsWith(ContentRoot + TEXT("/")))
            return Fail(TEXT("unsafe_asset_path"), TEXT("Asset filename escapes the plugin Content boundary"));
        if (IFileManager::Get().FileExists(*Filename) || FindObject<UPackage>(nullptr, *Package))
            return Fail(TEXT("asset_exists"), TEXT("Authoring requires an isolated capture without previous generated files at the selected path"));
        PackageName = Package;
        AssetName = Name;
        return true;
    }

    bool PinType(const TSharedPtr<FJsonObject>& Variable, FEdGraphPinType& Type)
    {
        FString Name;
        if (!String(Variable, TEXT("type"), Name)) return false;
        if (Name == TEXT("bool")) Type.PinCategory = UEdGraphSchema_K2::PC_Boolean;
        else if (Name == TEXT("int")) Type.PinCategory = UEdGraphSchema_K2::PC_Int;
        else if (Name == TEXT("int64")) Type.PinCategory = UEdGraphSchema_K2::PC_Int64;
        else if (Name == TEXT("float") || Name == TEXT("double"))
        {
            Type.PinCategory = UEdGraphSchema_K2::PC_Real;
            Type.PinSubCategory = Name == TEXT("float") ? UEdGraphSchema_K2::PC_Float : UEdGraphSchema_K2::PC_Double;
        }
        else if (Name == TEXT("string")) Type.PinCategory = UEdGraphSchema_K2::PC_String;
        else if (Name == TEXT("name")) Type.PinCategory = UEdGraphSchema_K2::PC_Name;
        else if (Name == TEXT("text")) Type.PinCategory = UEdGraphSchema_K2::PC_Text;
        else if (Name == TEXT("object") || Name == TEXT("class"))
        {
            FString ClassPath;
            if (!String(Variable, TEXT("class_path"), ClassPath)) return false;
            UClass* TargetClass = Class(ClassPath);
            if (!TargetClass) return false;
            Type.PinCategory = Name == TEXT("object") ? UEdGraphSchema_K2::PC_Object : UEdGraphSchema_K2::PC_Class;
            Type.PinSubCategoryObject = TargetClass;
        }
        else if (Name == TEXT("struct"))
        {
            FString Path;
            if (!String(Variable, TEXT("struct_path"), Path)) return false;
            UScriptStruct* Structure = LoadObject<UScriptStruct>(nullptr, *Path);
            if (!Structure || Structure->GetPathName() != Path)
                return Fail(TEXT("struct_unavailable"), TEXT("Struct type requires an exact reflected script-struct path"));
            Type.PinCategory = UEdGraphSchema_K2::PC_Struct;
            Type.PinSubCategoryObject = Structure;
        }
        else if (Name == TEXT("enum"))
        {
            FString Path;
            if (!String(Variable, TEXT("enum_path"), Path)) return false;
            UEnum* Enum = LoadObject<UEnum>(nullptr, *Path);
            if (!Enum || Enum->GetPathName() != Path)
                return Fail(TEXT("enum_unavailable"), TEXT("Enum type requires an exact reflected enum path"));
            Type.PinCategory = UEdGraphSchema_K2::PC_Byte;
            Type.PinSubCategoryObject = Enum;
        }
        else return Fail(TEXT("unsupported_variable_type"), FString::Printf(TEXT("Variable type '%s' is not supported"), *Name));
        if (Variable->HasField(TEXT("container")))
        {
            FString Container;
            if (!String(Variable, TEXT("container"), Container)) return false;
            if (Container != TEXT("array"))
                return Fail(TEXT("unsupported_container_type"), TEXT("Only the array container is supported"));
            Type.ContainerType = EPinContainerType::Array;
        }
        return true;
    }

    bool Defaults(UEdGraphNode* Node, const FString& Id, const TSharedPtr<FJsonObject>& Spec)
    {
        const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
        const auto PreserveResultDefault = [Node, Schema](UEdGraphPin* Pin)
        {
            if (!Cast<UK2Node_FunctionResult>(Node)) return;
            // Editable defaults also update the autogenerated value. Restore the
            // type baseline so skeleton-pin reconstruction retains explicit data.
            const FString ExplicitDefault = Pin->DefaultValue;
            const TObjectPtr<UObject> ExplicitObject = Pin->DefaultObject;
            const FText ExplicitText = Pin->DefaultTextValue;
            Schema->SetPinAutogeneratedDefaultValueBasedOnType(Pin);
            Pin->DefaultValue = ExplicitDefault;
            Pin->DefaultObject = ExplicitObject;
            Pin->DefaultTextValue = ExplicitText;
        };
        const TSharedPtr<FJsonObject>* Values = nullptr;
        if (Spec->HasField(TEXT("defaults")) && !Spec->TryGetObjectField(TEXT("defaults"), Values))
            return Fail(TEXT("invalid_pin_defaults"), TEXT("Pin defaults must be an object"), Id);
        if (Spec->TryGetObjectField(TEXT("defaults"), Values))
        {
            for (const auto& Pair : (*Values)->Values)
            {
                FString Value;
                if (!Pair.Value.IsValid() || Pair.Value->Type != EJson::String || !Pair.Value->TryGetString(Value)) return Fail(TEXT("invalid_pin_default"), TEXT("Pin defaults must be strings in Unreal pin literal format"), Id, Pair.Key);
                UEdGraphPin* Pin = Node->FindPin(FName(*Pair.Key), EGPD_Input);
                if (!Pin) return Fail(TEXT("pin_unavailable"), TEXT("Default refers to a missing input pin"), Id, Pair.Key);
                FString UseValue;
                TObjectPtr<UObject> UseObject = nullptr;
                FText UseText;
                Schema->GetPinDefaultValuesFromString(Pin->PinType, Node, Value, UseValue, UseObject, UseText);
                const FString Invalid = Schema->IsPinDefaultValid(Pin, UseValue, UseObject, UseText);
                if (!Invalid.IsEmpty()) return Fail(TEXT("invalid_pin_default"), Invalid, Id, Pair.Key);
                Schema->TrySetDefaultValue(*Pin, Value);
                PreserveResultDefault(Pin);
            }
        }
        if (Spec->HasField(TEXT("objects")) && !Spec->TryGetObjectField(TEXT("objects"), Values))
            return Fail(TEXT("invalid_pin_objects"), TEXT("Pin object defaults must be an object"), Id);
        if (Spec->TryGetObjectField(TEXT("objects"), Values))
        {
            for (const auto& Pair : (*Values)->Values)
            {
                FString Path;
                if (!Pair.Value.IsValid() || Pair.Value->Type != EJson::String || !Pair.Value->TryGetString(Path)) return Fail(TEXT("invalid_pin_object"), TEXT("Object defaults must be exact object paths"), Id, Pair.Key);
                UEdGraphPin* Pin = Node->FindPin(FName(*Pair.Key), EGPD_Input);
                if (!Pin) return Fail(TEXT("pin_unavailable"), TEXT("Object default refers to a missing input pin"), Id, Pair.Key);
                UObject* Value = LoadObject<UObject>(nullptr, *Path);
                if (!Value) return Fail(TEXT("object_unavailable"), FString::Printf(TEXT("Object '%s' did not load"), *Path), Id, Pair.Key);
                const FString Invalid = Schema->IsPinDefaultValid(Pin, FString(), Value, FText::GetEmpty());
                if (!Invalid.IsEmpty()) return Fail(TEXT("invalid_pin_object"), Invalid, Id, Pair.Key);
                Schema->TrySetDefaultObject(*Pin, Value);
                PreserveResultDefault(Pin);
            }
        }
        return true;
    }

    bool FunctionParameters(const TSharedPtr<FJsonObject>& Spec, const TCHAR* Key,
        UK2Node_EditablePinBase* Terminator, EEdGraphPinDirection Direction, TSet<FName>& ParameterNames)
    {
        const TArray<TSharedPtr<FJsonValue>>* Parameters = nullptr;
        if (!Array(Spec, Key, Parameters)) return false;
        for (const auto& ParameterValue : *Parameters)
        {
            if (!ParameterValue.IsValid() || ParameterValue->Type != EJson::Object)
                return Fail(TEXT("invalid_function_parameter"), TEXT("Each function parameter must be an object"));
            const TSharedPtr<FJsonObject> Parameter = ParameterValue->AsObject();
            FString ParameterName;
            if (!String(Parameter, TEXT("name"), ParameterName)) return false;
            const FName Name(*ParameterName);
            FText InvalidName;
            if (!Name.IsValidXName(InvalidName) || ParameterNames.Contains(Name)
                || Name == UEdGraphSchema_K2::PN_Execute || Name == UEdGraphSchema_K2::PN_Then
                || Name == UEdGraphSchema_K2::PN_Self || Terminator->FindPin(Name))
                return Fail(TEXT("function_parameter_collision"), TEXT("Function parameter names must be valid, unique and distinct from control pins"), {}, ParameterName);
            FEdGraphPinType Type;
            if (!PinType(Parameter, Type)) return false;
            // K2 array inputs are passed by reference. Const marks them as read-only
            // inputs, rather than mutable Out/Ref parameters in the runtime contract.
            Type.bIsConst = Direction == EGPD_Output && Type.ContainerType == EPinContainerType::Array;
            Type.bIsReference = false;
            FText InvalidType;
            if (!Terminator->CanCreateUserDefinedPin(Type, Direction, InvalidType))
                return Fail(TEXT("invalid_function_parameter"), InvalidType.ToString(), {}, ParameterName);
            UEdGraphPin* Pin = Terminator->CreateUserDefinedPin(Name, Type, Direction, false);
            if (!Pin || Pin->PinName != Name)
                return Fail(TEXT("function_parameter_creation_failed"), TEXT("K2 did not create the requested exact parameter pin"), {}, ParameterName);
            ParameterNames.Add(Name);
        }
        return true;
    }

    bool PopulateGraph(const TSharedPtr<FJsonObject>& Spec, UEdGraph* Graph, UClass* Parent,
        const TSet<FName>& VariableNames, TMap<FString, UEdGraphNode*>& NodesById,
        UK2Node_FunctionEntry* FunctionEntry = nullptr, UK2Node_FunctionResult* FunctionResult = nullptr)
    {
        const TArray<TSharedPtr<FJsonValue>>* Nodes = nullptr;
        if (!Array(Spec, TEXT("nodes"), Nodes)) return false;
        TSet<FName> Events;
        for (const auto& Value : *Nodes)
        {
            if (!Value.IsValid() || Value->Type != EJson::Object) return Fail(TEXT("invalid_spec"), TEXT("Each node must be an object"));
            const TSharedPtr<FJsonObject> NodeSpec = Value->AsObject();
            FString Id, Kind;
            if (!String(NodeSpec, TEXT("id"), Id) || !String(NodeSpec, TEXT("kind"), Kind, Id)) return false;
            if (NodesById.Contains(Id)) return Fail(TEXT("duplicate_node"), TEXT("Node id is duplicated"), Id);
            UEdGraphNode* Node = nullptr;
            if (Kind == TEXT("function_entry") || Kind == TEXT("function_result"))
            {
                if (!FunctionEntry || !FunctionResult)
                    return Fail(TEXT("function_node_outside_function"), TEXT("Function terminators require a named function graph"), Id);
                Node = Kind == TEXT("function_entry") ? static_cast<UEdGraphNode*>(FunctionEntry) : static_cast<UEdGraphNode*>(FunctionResult);
                if (NodesById.FindKey(Node))
                    return Fail(TEXT("duplicate_terminator"), TEXT("Each function terminator must have one node id"), Id);
            }
            else if (Kind == TEXT("event"))
            {
                if (FunctionEntry)
                    return Fail(TEXT("event_inside_function"), TEXT("Parent events belong to the EventGraph"), Id);
                FString FunctionName;
                if (!String(NodeSpec, TEXT("function"), FunctionName, Id)) return false;
                UFunction* Function = Parent->FindFunctionByName(FName(*FunctionName));
                if (!Function || !Function->HasAnyFunctionFlags(FUNC_BlueprintEvent) || Events.Contains(Function->GetFName()))
                    return Fail(TEXT("event_unavailable"), TEXT("Event must identify a unique reflected Blueprint event of the parent"), Id);
                Events.Add(Function->GetFName());
                FGraphNodeCreator<UK2Node_Event> Creator(*Graph);
                UK2Node_Event* Event = Creator.CreateNode(false);
                Event->EventReference.SetExternalMember(Function->GetFName(), Function->GetOwnerClass());
                Event->bOverrideFunction = true;
                Creator.Finalize();
                Node = Event;
            }
            else if (Kind == TEXT("variable_set") || Kind == TEXT("variable_get"))
            {
                FString VariableName;
                if (!String(NodeSpec, TEXT("variable"), VariableName, Id)) return false;
                if (!VariableNames.Contains(FName(*VariableName))) return Fail(TEXT("variable_unavailable"), TEXT("Variable node must reference a declared member"), Id);
                if (Kind == TEXT("variable_set"))
                {
                    FGraphNodeCreator<UK2Node_VariableSet> Creator(*Graph);
                    UK2Node_VariableSet* Variable = Creator.CreateNode(false);
                    Variable->VariableReference.SetSelfMember(FName(*VariableName));
                    Creator.Finalize();
                    Node = Variable;
                }
                else
                {
                    FGraphNodeCreator<UK2Node_VariableGet> Creator(*Graph);
                    UK2Node_VariableGet* Variable = Creator.CreateNode(false);
                    Variable->VariableReference.SetSelfMember(FName(*VariableName));
                    Creator.Finalize();
                    Node = Variable;
                }
            }
            else if (Kind == TEXT("call_function"))
            {
                FString ClassPath, FunctionName;
                if (!String(NodeSpec, TEXT("class"), ClassPath, Id) || !String(NodeSpec, TEXT("function"), FunctionName, Id)) return false;
                UClass* Owner = Class(ClassPath);
                if (!Owner) return false;
                UFunction* Function = Owner->FindFunctionByName(FName(*FunctionName));
                if (!Function || !Function->HasAnyFunctionFlags(FUNC_BlueprintCallable | FUNC_BlueprintPure))
                    return Fail(TEXT("function_unavailable"), TEXT("Function must be reflected and callable from Blueprint"), Id);
                if (Function->HasMetaData(TEXT("DevelopmentOnly")) || Function->HasAnyFunctionFlags(FUNC_EditorOnly))
                    return Fail(TEXT("function_not_shipping"), TEXT("Editor/development-only functions cannot establish Shipping mod behavior"), Id);
                if (FunctionEntry && Function->HasMetaData(TEXT("Latent")))
                    return Fail(TEXT("latent_function_in_function_graph"), TEXT("Latent calls require an event graph and cannot run inside a synchronous function"), Id);
                if (Function->HasMetaData(TEXT("ArrayParm")))
                {
                    FGraphNodeCreator<UK2Node_CallArrayFunction> Creator(*Graph);
                    UK2Node_CallArrayFunction* Call = Creator.CreateNode(false);
                    Call->SetFromFunction(Function);
                    Creator.Finalize();
                    Node = Call;
                }
                else
                {
                    FGraphNodeCreator<UK2Node_CallFunction> Creator(*Graph);
                    UK2Node_CallFunction* Call = Creator.CreateNode(false);
                    Call->SetFromFunction(Function);
                    Creator.Finalize();
                    Node = Call;
                }
            }
            else if (Kind == TEXT("branch"))
            {
                FGraphNodeCreator<UK2Node_IfThenElse> Creator(*Graph);
                Node = Creator.CreateNode(false);
                Creator.Finalize();
            }
            else if (Kind == TEXT("sequence"))
            {
                FGraphNodeCreator<UK2Node_ExecutionSequence> Creator(*Graph);
                UK2Node_ExecutionSequence* Sequence = Creator.CreateNode(false);
                Creator.Finalize();
                int32 Count = 2;
                if (NodeSpec->HasField(TEXT("outputs")) && (!NodeSpec->HasTypedField<EJson::Number>(TEXT("outputs"))
                    || !NodeSpec->TryGetNumberField(TEXT("outputs"), Count) || Count < 2))
                    return Fail(TEXT("invalid_sequence_outputs"), TEXT("Sequence output count must be an integer of at least two"), Id);
                for (int32 Index = 2; Index < Count; ++Index) Sequence->AddInputPin();
                Node = Sequence;
            }
            else return Fail(TEXT("unsupported_node"), FString::Printf(TEXT("Node kind '%s' is not supported"), *Kind), Id);
            if (!Node) return Fail(TEXT("node_creation_failed"), TEXT("Editor failed to create the node"), Id);
            Node->SetEnabledState(ENodeEnabledState::Enabled, true);
            int32 X = 0, Y = NodesById.Num() * 160;
            if (NodeSpec->HasField(TEXT("x")) && (!NodeSpec->HasTypedField<EJson::Number>(TEXT("x")) || !NodeSpec->TryGetNumberField(TEXT("x"), X)))
                return Fail(TEXT("invalid_node_position"), TEXT("Node x coordinate must be a signed 32-bit integer"), Id);
            if (NodeSpec->HasField(TEXT("y")) && (!NodeSpec->HasTypedField<EJson::Number>(TEXT("y")) || !NodeSpec->TryGetNumberField(TEXT("y"), Y)))
                return Fail(TEXT("invalid_node_position"), TEXT("Node y coordinate must be a signed 32-bit integer"), Id);
            Node->NodePosX = X;
            Node->NodePosY = Y;
            NodesById.Add(Id, Node);
            if (NodeSpec->HasField(TEXT("split_pins")))
            {
                const TArray<TSharedPtr<FJsonValue>>* SplitPins = nullptr;
                if (!Array(NodeSpec, TEXT("split_pins"), SplitPins)) return false;
                for (const auto& SplitValue : *SplitPins)
                {
                    FString PinName;
                    if (!SplitValue.IsValid() || SplitValue->Type != EJson::String || !SplitValue->TryGetString(PinName))
                        return Fail(TEXT("invalid_split_pin"), TEXT("Split pins must be exact pin names"), Id);
                    UEdGraphPin* Pin = Node->FindPin(FName(*PinName));
                    if (!Pin || Pin->PinType.PinCategory != UEdGraphSchema_K2::PC_Struct || Pin->SubPins.Num())
                        return Fail(TEXT("invalid_split_pin"), TEXT("Split requires an existing unsplit struct pin"), Id, PinName);
                    GetDefault<UEdGraphSchema_K2>()->SplitPin(Pin);
                    if (!Pin->SubPins.Num())
                        return Fail(TEXT("split_pin_failed"), TEXT("The selected struct pin could not be split"), Id, PinName);
                }
            }
            if (!Defaults(Node, Id, NodeSpec)) return false;
        }
        const TArray<TSharedPtr<FJsonValue>>* Connections = nullptr;
        if (!Array(Spec, TEXT("connections"), Connections)) return false;
        const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
        for (const auto& Value : *Connections)
        {
            if (!Value.IsValid() || Value->Type != EJson::Object) return Fail(TEXT("invalid_spec"), TEXT("Each connection must be an object"));
            const TSharedPtr<FJsonObject> Connection = Value->AsObject();
            const TSharedPtr<FJsonObject>* From = nullptr;
            const TSharedPtr<FJsonObject>* To = nullptr;
            if (!Connection.IsValid() || !Connection->TryGetObjectField(TEXT("from"), From) || !Connection->TryGetObjectField(TEXT("to"), To))
                return Fail(TEXT("invalid_connection"), TEXT("Each connection requires from/to node and pin objects"));
            FString FromId, FromPin, ToId, ToPin;
            if (!String(*From, TEXT("node"), FromId) || !String(*From, TEXT("pin"), FromPin, FromId)
                || !String(*To, TEXT("node"), ToId) || !String(*To, TEXT("pin"), ToPin, ToId)) return false;
            UEdGraphNode** FromNode = NodesById.Find(FromId);
            UEdGraphNode** ToNode = NodesById.Find(ToId);
            if (!FromNode || !ToNode) return Fail(TEXT("node_unavailable"), TEXT("Connection references a missing node"), !FromNode ? FromId : ToId);
            UEdGraphPin* A = (*FromNode)->FindPin(FName(*FromPin), EGPD_Output);
            UEdGraphPin* B = (*ToNode)->FindPin(FName(*ToPin), EGPD_Input);
            if (!A || !B) return Fail(TEXT("pin_unavailable"), TEXT("Connection references a missing directional pin"), !A ? FromId : ToId, !A ? FromPin : ToPin);
            if (B->LinkedTo.Num() && B->PinType.PinCategory != UEdGraphSchema_K2::PC_Exec)
                return Fail(TEXT("connection_replacement"), TEXT("An input already has a connection; implicit rewiring is not accepted"), ToId, ToPin);
            if (A->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec && A->LinkedTo.Num())
                return Fail(TEXT("connection_replacement"), TEXT("An execution output already has a connection; use an explicit graph node for multiple paths"), FromId, FromPin);
            const FPinConnectionResponse ConnectionResponse = Schema->CanCreateConnection(A, B);
            if (!Schema->TryCreateConnection(A, B)) return Fail(TEXT("connection_invalid"), ConnectionResponse.Message.ToString(), ToId, ToPin);
        }
        return true;
    }

    bool Blueprint(const TSharedPtr<FJsonObject>& Spec, UPackage* Package, const FString& Name, UObject*& Out, const TSharedRef<FJsonObject>& Report)
    {
        FString ParentPath;
        if (!String(Spec, TEXT("parent_class"), ParentPath)) return false;
        UClass* Parent = Class(ParentPath);
        if (!Parent || !FKismetEditorUtilities::CanCreateBlueprintOfClass(Parent))
            return Fail(TEXT("invalid_blueprint_parent"), TEXT("The selected class cannot be used as a Blueprint parent"));
        UBlueprint* BP = FKismetEditorUtilities::CreateBlueprint(Parent, Package, FName(*Name), BPTYPE_Normal, TEXT("SN2MCPAuthoring"));
        if (!BP) return Fail(TEXT("blueprint_creation_failed"), TEXT("Editor failed to create the Blueprint"));
        TStrongObjectPtr<UBlueprint> BlueprintLifetime(BP);
        Out = BP;
        const TArray<TSharedPtr<FJsonValue>>* Variables = nullptr;
        if (!Array(Spec, TEXT("variables"), Variables)) return false;
        TSet<FName> VariableNames;
        for (const auto& Value : *Variables)
        {
            if (!Value.IsValid() || Value->Type != EJson::Object) return Fail(TEXT("invalid_spec"), TEXT("Each variable must be an object"));
            const TSharedPtr<FJsonObject> Variable = Value->AsObject();
            FString VariableName;
            if (!String(Variable, TEXT("name"), VariableName)) return false;
            const FName VarName(*VariableName);
            FText InvalidName;
            if (!VarName.IsValidXName(InvalidName)) return Fail(TEXT("invalid_variable_name"), InvalidName.ToString());
            if (VariableNames.Contains(VarName) || Parent->FindPropertyByName(VarName))
                return Fail(TEXT("variable_collision"), TEXT("Variable duplicates a declared or inherited member"));
            VariableNames.Add(VarName);
            FEdGraphPinType Type;
            if (!PinType(Variable, Type)) return false;
            FString Default;
            if (Variable->HasField(TEXT("default")) && (!Variable->HasTypedField<EJson::String>(TEXT("default")) || !Variable->TryGetStringField(TEXT("default"), Default)))
                return Fail(TEXT("invalid_variable_default"), TEXT("Variable defaults must use Unreal property literal strings"));
            if (!FBlueprintEditorUtils::AddMemberVariable(BP, VarName, Type, Default))
                return Fail(TEXT("variable_creation_failed"), FString::Printf(TEXT("Cannot declare variable '%s'"), *VariableName));
        }
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);
        for (const auto& Value : *Variables)
        {
            const TSharedPtr<FJsonObject> Variable = Value->AsObject();
            FString Default;
            if (Variable->TryGetStringField(TEXT("default"), Default))
            {
                const FString VariableName = Variable->GetStringField(TEXT("name"));
                FProperty* Property = BP->SkeletonGeneratedClass ? BP->SkeletonGeneratedClass->FindPropertyByName(FName(*VariableName)) : nullptr;
                if (!Property) return Fail(TEXT("variable_unavailable"), TEXT("Declared variable was not generated in the skeleton"));
                FDefaultConstructedPropertyElement Temporary(Property);
                const TCHAR* End = Property->ImportText_Direct(*Default, Temporary.GetObjAddress(), BP, PPF_None);
                if (!End || *End != TEXT('\0')) return Fail(TEXT("invalid_variable_default"), FString::Printf(TEXT("Variable '%s' has an invalid Unreal property literal"), *VariableName));
            }
        }
        UEdGraph* Graph = nullptr;
        if (BP->UbergraphPages.Num()) Graph = BP->UbergraphPages[0];
        else
        {
            Graph = FBlueprintEditorUtils::CreateNewGraph(BP, TEXT("EventGraph"), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
            FBlueprintEditorUtils::AddUbergraphPage(BP, Graph);
        }
        // Default disabled events are replaced by the explicitly authored graph.
        TArray<TObjectPtr<UEdGraphNode>> DefaultNodes = Graph->Nodes;
        for (UEdGraphNode* DefaultNode : DefaultNodes) Graph->RemoveNode(DefaultNode);
        TMap<FString, UEdGraphNode*> NodesById;
        if (!PopulateGraph(Spec, Graph, Parent, VariableNames, NodesById)) return false;
        TSet<FName> FunctionNames;
        if (Spec->HasField(TEXT("functions")))
        {
            const TArray<TSharedPtr<FJsonValue>>* Functions = nullptr;
            if (!Array(Spec, TEXT("functions"), Functions)) return false;
            for (const auto& FunctionValue : *Functions)
            {
                if (!FunctionValue.IsValid() || FunctionValue->Type != EJson::Object)
                    return Fail(TEXT("invalid_spec"), TEXT("Each function must be an object"));
                const TSharedPtr<FJsonObject> FunctionSpec = FunctionValue->AsObject();
                FString FunctionName;
                if (!String(FunctionSpec, TEXT("name"), FunctionName)) return false;
                const FName FunctionFName(*FunctionName);
                FText InvalidName;
                if (!FunctionFName.IsValidXName(InvalidName) || FunctionNames.Contains(FunctionFName)
                    || VariableNames.Contains(FunctionFName) || Parent->FindFunctionByName(FunctionFName)
                    || Parent->FindPropertyByName(FunctionFName) || FunctionFName == Graph->GetFName())
                    return Fail(TEXT("function_name_collision"), TEXT("Function name must be valid, unique and absent from the parent"), FunctionName);
                FunctionNames.Add(FunctionFName);
                UEdGraph* FunctionGraph = FBlueprintEditorUtils::CreateNewGraph(BP, FunctionFName,
                    UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
                FBlueprintEditorUtils::AddFunctionGraph<UClass>(BP, FunctionGraph, true, nullptr);
                UK2Node_FunctionEntry* Entry = nullptr;
                for (UEdGraphNode* FunctionNode : FunctionGraph->Nodes)
                    if (UK2Node_FunctionEntry* Candidate = Cast<UK2Node_FunctionEntry>(FunctionNode)) Entry = Candidate;
                if (!Entry) return Fail(TEXT("function_entry_unavailable"), TEXT("K2 did not create a function entry"), FunctionName);
                Entry->FunctionReference.SetSelfMember(FunctionFName);
                FGraphNodeCreator<UK2Node_FunctionResult> ResultCreator(*FunctionGraph);
                UK2Node_FunctionResult* Result = ResultCreator.CreateNode(false);
                Result->FunctionReference = Entry->FunctionReference;
                Result->bIsEditable = true;
                ResultCreator.Finalize();
                Entry->SetExtraFlags(FUNC_Public | FUNC_BlueprintCallable | FUNC_BlueprintEvent);
                TSet<FName> ParameterNames;
                if (!FunctionParameters(FunctionSpec, TEXT("inputs"), Entry, EGPD_Output, ParameterNames)
                    || !FunctionParameters(FunctionSpec, TEXT("outputs"), Result, EGPD_Input, ParameterNames)) return false;
                TMap<FString, UEdGraphNode*> FunctionNodes;
                if (!PopulateGraph(FunctionSpec, FunctionGraph, Parent, VariableNames, FunctionNodes, Entry, Result)) return false;
                if (!FunctionNodes.FindKey(Entry) || !FunctionNodes.FindKey(Result))
                    return Fail(TEXT("function_terminator_unmapped"), TEXT("Function nodes must identify both function_entry and function_result"), FunctionName);
                for (const auto& Pair : FunctionNodes)
                {
                    const FString QualifiedNode = FunctionName + TEXT("/") + Pair.Key;
                    if (NodesById.Contains(QualifiedNode))
                        return Fail(TEXT("node_id_collision"), TEXT("Event node id collides with a qualified function node id"), QualifiedNode);
                    NodesById.Add(QualifiedNode, Pair.Value);
                }
            }
        }
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        FCompilerResultsLog Log;
        Log.SetSourcePath(CurrentAsset);
        FKismetEditorUtilities::CompileBlueprint(BP, EBlueprintCompileOptions::None, &Log);
        TSharedRef<FJsonObject> Compiler = Object();
        Compiler->SetNumberField(TEXT("errors"), Log.NumErrors);
        Compiler->SetNumberField(TEXT("warnings"), Log.NumWarnings);
        TArray<TSharedPtr<FJsonValue>> Messages;
        for (const auto& Message : Log.Messages)
        {
            TSharedRef<FJsonObject> Item = Object();
            Item->SetStringField(TEXT("text"), Message->ToText().ToString());
            Item->SetStringField(TEXT("severity"), Message->GetSeverity() == EMessageSeverity::Error ? TEXT("error")
                : Message->GetSeverity() == EMessageSeverity::Warning ? TEXT("warning") : TEXT("info"));
            TArray<UEdGraphNode*> MessageNodes;
            TArray<TSharedPtr<FJsonValue>> ObjectPaths;
            for (const auto& Token : Message->GetMessageTokens())
            {
                if (Token->GetType() == EMessageToken::EdGraph)
                {
                    const FEdGraphToken& GraphToken = static_cast<const FEdGraphToken&>(Token.Get());
                    if (const UObject* GraphObject = GraphToken.GetGraphObject())
                    {
                        ObjectPaths.Add(MakeShared<FJsonValueString>(GraphObject->GetPathName()));
                        if (const UEdGraphNode* MessageNode = Cast<UEdGraphNode>(GraphObject)) MessageNodes.Add(const_cast<UEdGraphNode*>(MessageNode));
                    }
                    if (const UEdGraphPin* Pin = GraphToken.GetPin()) MessageNodes.Add(Pin->GetOwningNode());
                }
            }
            Item->SetArrayField(TEXT("objects"), ObjectPaths);
            TArray<TSharedPtr<FJsonValue>> Locations;
            for (const auto& Pair : NodesById)
                if (MessageNodes.Contains(Pair.Value)) Locations.Add(MakeShared<FJsonValueString>(Pair.Key));
            Item->SetArrayField(TEXT("nodes"), Locations);
            Messages.Add(MakeShared<FJsonValueObject>(Item));
        }
        Compiler->SetArrayField(TEXT("messages"), Messages);
        Report->SetObjectField(TEXT("compiler"), Compiler);
        if (Log.NumErrors || BP->Status == BS_Error || !BP->GeneratedClass)
            return Fail(TEXT("blueprint_compile_failed"), TEXT("Blueprint compilation failed; no asset was saved"));
        TArray<TSharedPtr<FJsonValue>> CompiledFunctions;
        for (UEdGraph* FunctionGraph : BP->FunctionGraphs)
        {
            if (!FunctionNames.Contains(FunctionGraph->GetFName())) continue;
            UFunction* Function = BP->GeneratedClass->FindFunctionByName(FunctionGraph->GetFName());
            TSharedRef<FJsonObject> Compiled = Object();
            Compiled->SetStringField(TEXT("name"), FunctionGraph->GetName());
            Compiled->SetBoolField(TEXT("owned"), Function && Function->GetOuterUClass() == BP->GeneratedClass);
            Compiled->SetNumberField(TEXT("script_bytes"), Function ? Function->Script.Num() : 0);
            CompiledFunctions.Add(MakeShared<FJsonValueObject>(Compiled));
            Compiler->SetArrayField(TEXT("functions"), CompiledFunctions);
            if (!Function || Function->GetOuterUClass() != BP->GeneratedClass || !Function->Script.Num())
                return Fail(TEXT("function_compile_incomplete"), TEXT("Named function must have an owned compiled bytecode body"), FunctionGraph->GetName());
            Compiled->SetStringField(TEXT("script_hex"), BytesToHex(Function->Script.GetData(), Function->Script.Num()));
            TArray<TSharedPtr<FJsonValue>> GraphPins;
            for (UEdGraphNode* FunctionNode : FunctionGraph->Nodes)
            {
                const FString* NodeId = NodesById.FindKey(FunctionNode);
                if (!NodeId) continue;
                for (UEdGraphPin* Pin : FunctionNode->Pins)
                {
                    TSharedRef<FJsonObject> PinInfo = Object();
                    PinInfo->SetStringField(TEXT("node"), *NodeId);
                    PinInfo->SetStringField(TEXT("pin"), Pin->PinName.ToString());
                    PinInfo->SetStringField(TEXT("direction"), Pin->Direction == EGPD_Input ? TEXT("input") : TEXT("output"));
                    PinInfo->SetStringField(TEXT("default"), Pin->GetDefaultAsString());
                    PinInfo->SetStringField(TEXT("autogenerated_default"), Pin->AutogeneratedDefaultValue);
                    PinInfo->SetNumberField(TEXT("links"), Pin->LinkedTo.Num());
                    GraphPins.Add(MakeShared<FJsonValueObject>(PinInfo));
                }
            }
            Compiled->SetArrayField(TEXT("graph_pins"), GraphPins);
        }
        Compiler->SetArrayField(TEXT("functions"), CompiledFunctions);
        Report->SetStringField(TEXT("generated_class"), BP->GeneratedClass->GetPathName());
        return true;
    }

    TSharedPtr<FJsonValue> NormalizeProperty(FProperty* Property, const TSharedPtr<FJsonValue>& Value, bool FixedElement = false)
    {
        if (Value->Type == EJson::Null) return MakeShared<FJsonValueString>(TEXT("None"));
        if (Property->ArrayDim > 1 && !FixedElement)
        {
            TArray<TSharedPtr<FJsonValue>> Normalized;
            for (const auto& Element : Value->AsArray()) Normalized.Add(NormalizeProperty(Property, Element, true));
            return MakeShared<FJsonValueArray>(Normalized);
        }
        if (FArrayProperty* ArrayProperty = CastField<FArrayProperty>(Property))
        {
            TArray<TSharedPtr<FJsonValue>> Normalized;
            for (const auto& Element : Value->AsArray()) Normalized.Add(NormalizeProperty(ArrayProperty->Inner, Element));
            return MakeShared<FJsonValueArray>(Normalized);
        }
        if (FStructProperty* StructProperty = CastField<FStructProperty>(Property))
        {
            TSharedRef<FJsonObject> Normalized = Object();
            for (const auto& Field : Value->AsObject()->Values)
                Normalized->SetField(Field.Key, NormalizeProperty(StructProperty->Struct->FindPropertyByName(FName(*Field.Key)), Field.Value));
            return MakeShared<FJsonValueObject>(Normalized);
        }
        return Value;
    }

    bool JsonProperty(FProperty* Property, const TSharedPtr<FJsonValue>& Value, const FString& Location, bool FixedElement = false)
    {
        if (!Value.IsValid()) return Fail(TEXT("invalid_property_value"), Location + TEXT(": JSON value is absent"));
        if (Value->Type == EJson::Null)
        {
            if (CastField<FObjectPropertyBase>(Property)) return true;
            return Fail(TEXT("invalid_property_value"), Location + TEXT(": null applies only to object references"));
        }
        if (Property->ArrayDim > 1 && !FixedElement)
        {
            if (Value->Type != EJson::Array || Value->AsArray().Num() != Property->ArrayDim)
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": fixed array requires exactly ArrayDim values"));
            for (int32 Index = 0; Index < Property->ArrayDim; ++Index)
                if (!JsonProperty(Property, Value->AsArray()[Index], FString::Printf(TEXT("%s[%d]"), *Location, Index), true)) return false;
            return true;
        }
        if (FArrayProperty* ArrayProperty = CastField<FArrayProperty>(Property))
        {
            if (Value->Type != EJson::Array) return Fail(TEXT("invalid_property_value"), Location + TEXT(": array value is required"));
            for (int32 Index = 0; Index < Value->AsArray().Num(); ++Index)
                if (!JsonProperty(ArrayProperty->Inner, Value->AsArray()[Index], FString::Printf(TEXT("%s[%d]"), *Location, Index))) return false;
            return true;
        }
        UEnum* Enum = nullptr;
        if (FEnumProperty* EnumProperty = CastField<FEnumProperty>(Property)) Enum = EnumProperty->GetEnum();
        else if (FNumericProperty* Numeric = CastField<FNumericProperty>(Property)) Enum = Numeric->GetIntPropertyEnum();
        if (Enum)
        {
            int64 EnumValue = INDEX_NONE;
            if (Value->Type == EJson::String) EnumValue = Enum->GetValueByName(FName(*Value->AsString()), EGetByNameFlags::CheckAuthoredName);
            else if (Value->Type == EJson::Number)
            {
                const double Number = Value->AsNumber();
                if (FMath::IsFinite(Number) && Number == FMath::FloorToDouble(Number) && Number >= -9007199254740991.0 && Number <= 9007199254740991.0)
                    EnumValue = static_cast<int64>(Number);
            }
            if (EnumValue == INDEX_NONE || !Enum->IsValidEnumValueOrBitfield(EnumValue))
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": valid reflected enum name/value is required"));
            return true;
        }
        if (FNumericProperty* Numeric = CastField<FNumericProperty>(Property))
        {
            if (Value->Type != EJson::Number || !FMath::IsFinite(Value->AsNumber()))
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": finite JSON number is required"));
            const double Number = Value->AsNumber();
            if (Numeric->IsInteger())
            {
            const int32 Bits = Numeric->GetElementSize() * 8;
                const bool Unsigned = CastField<FByteProperty>(Property) || CastField<FUInt16Property>(Property)
                    || CastField<FUInt32Property>(Property) || CastField<FUInt64Property>(Property);
                const double Minimum = Unsigned ? 0.0 : -FMath::Pow(2.0, Bits - 1);
                const double Maximum = Unsigned ? FMath::Pow(2.0, Bits) - 1.0 : FMath::Pow(2.0, Bits - 1) - 1.0;
                if (Number != FMath::FloorToDouble(Number) || Number < Minimum || Number > Maximum
                    || Number < -9007199254740991.0 || Number > 9007199254740991.0)
                    return Fail(TEXT("invalid_property_value"), Location + TEXT(": integer is outside the property's exact JSON numeric range"));
            }
            else if (CastField<FFloatProperty>(Property) && !FMath::IsFinite(static_cast<float>(Number)))
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": number overflows a float property"));
            return true;
        }
        if (CastField<FBoolProperty>(Property))
            return Value->Type == EJson::Boolean || Fail(TEXT("invalid_property_value"), Location + TEXT(": JSON boolean is required"));
        if (CastField<FStrProperty>(Property) || CastField<FNameProperty>(Property) || CastField<FTextProperty>(Property))
            return Value->Type == EJson::String || Fail(TEXT("invalid_property_value"), Location + TEXT(": JSON string is required"));
        if (CastField<FSoftObjectProperty>(Property) || CastField<FSoftClassProperty>(Property))
        {
            if (Value->Type != EJson::String) return Fail(TEXT("invalid_property_value"), Location + TEXT(": soft references require an object path string"));
            FDefaultConstructedPropertyElement Temporary(Property);
            const TCHAR* End = Property->ImportText_Direct(*Value->AsString(), Temporary.GetObjAddress(), nullptr, PPF_None);
            if (!End || *End != TEXT('\0')) return Fail(TEXT("invalid_property_value"), Location + TEXT(": soft reference literal is invalid"));
            const FString Literal = Value->AsString();
            const FSoftObjectPtr& Reference = FSoftObjectProperty::GetPropertyValue(Temporary.GetObjAddress());
            if (!Literal.IsEmpty() && !Literal.Equals(TEXT("None"), ESearchCase::IgnoreCase) && !Reference.GetUniqueID().IsValid())
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": soft reference path is invalid"));
            return true;
        }
        if (FObjectPropertyBase* ObjectProperty = CastField<FObjectPropertyBase>(Property))
        {
            if (Value->Type != EJson::String) return Fail(TEXT("invalid_property_value"), Location + TEXT(": object references require an exact object path string"));
            UObject* Referenced = LoadObject<UObject>(nullptr, *Value->AsString());
            if (!Referenced || !Referenced->IsA(ObjectProperty->PropertyClass))
                return Fail(TEXT("invalid_property_value"), Location + TEXT(": object did not load as the property's declared class"));
            return true;
        }
        if (FStructProperty* StructProperty = CastField<FStructProperty>(Property))
        {
            if (Value->Type != EJson::Object) return Fail(TEXT("invalid_property_value"), Location + TEXT(": struct fields require a JSON object"));
            for (const auto& Field : Value->AsObject()->Values)
            {
                FProperty* Child = StructProperty->Struct->FindPropertyByName(FName(*Field.Key));
                if (!Child) return Fail(TEXT("property_unavailable"), Location + TEXT(".") + Field.Key + TEXT(": struct field is absent"));
                if (!JsonProperty(Child, Field.Value, Location + TEXT(".") + Field.Key)) return false;
            }
            return true;
        }
        return Fail(TEXT("unsupported_property_type"), Location + TEXT(": property type is not supported by this authoring schema"));
    }

    bool DataAsset(const TSharedPtr<FJsonObject>& Spec, UPackage* Package, const FString& Name, UObject*& Out)
    {
        FString ClassPath;
        if (!String(Spec, TEXT("class"), ClassPath)) return false;
        UClass* Target = Class(ClassPath);
        if (!Target || !Target->IsChildOf(UDataAsset::StaticClass()) || Target->HasAnyClassFlags(CLASS_Abstract))
            return Fail(TEXT("invalid_data_class"), TEXT("Data asset class must be a concrete UDataAsset descendant"));
        Out = NewObject<UObject>(Package, Target, FName(*Name), RF_Public | RF_Standalone | RF_Transactional);
        const TSharedPtr<FJsonObject>* Properties = nullptr;
        if (!Spec->TryGetObjectField(TEXT("properties"), Properties)) return Fail(TEXT("invalid_spec"), TEXT("Data asset properties object is required"));
        for (const auto& Pair : (*Properties)->Values)
        {
            FProperty* Property = Target->FindPropertyByName(FName(*Pair.Key));
            if (!Property || Property->HasAnyPropertyFlags(CPF_Transient | CPF_Deprecated | CPF_EditorOnly)
                || !Property->HasAnyPropertyFlags(CPF_Edit | CPF_BlueprintVisible))
                return Fail(TEXT("property_unavailable"), FString::Printf(TEXT("Property '%s' is absent or not editable runtime data"), *Pair.Key));
        }
        for (const auto& Pair : (*Properties)->Values)
            if (!JsonProperty(Target->FindPropertyByName(FName(*Pair.Key)), Pair.Value, Pair.Key)) return false;
        TMap<FString, TSharedPtr<FJsonValue>> Normalized;
        for (const auto& Pair : (*Properties)->Values)
        {
            Normalized.Add(Pair.Key, NormalizeProperty(Target->FindPropertyByName(FName(*Pair.Key)), Pair.Value));
        }
        FText Reason;
        if (!FJsonObjectConverter::JsonAttributesToUStruct(Normalized, Target, Out,
            0, CPF_Transient | CPF_Deprecated | CPF_EditorOnly, false, &Reason))
            return Fail(TEXT("property_import_failed"), Reason.ToString());
        Out->PostEditChange();
        FAssetRegistryModule::AssetCreated(Out);
        return true;
    }

    bool Asset(const TSharedPtr<FJsonObject>& Spec)
    {
        FString Kind, Path;
        if (!String(Spec, TEXT("kind"), Kind) || !String(Spec, TEXT("path"), Path)) return false;
        CurrentAsset = Path;
        FString PackageName, Name, Filename;
        if (!ResolveAsset(Path, PackageName, Name, Filename)) return false;
        UPackage* Package = CreatePackage(*PackageName);
        TStrongObjectPtr<UPackage> PackageLifetime(Package);
        Package->SetFlags(RF_Transactional);
        TSharedRef<FJsonObject> Report = Object();
        Report->SetStringField(TEXT("kind"), Kind);
        Report->SetStringField(TEXT("path"), Path);
        Report->SetStringField(TEXT("output_file"), Filename);
        Report->SetBoolField(TEXT("saved"), false);
        AssetReports.Add(MakeShared<FJsonValueObject>(Report));
        UObject* Created = nullptr;
        bool Valid = false;
        if (Kind == TEXT("blueprint")) Valid = Blueprint(Spec, Package, Name, Created, Report);
        else if (Kind == TEXT("data_asset")) Valid = DataAsset(Spec, Package, Name, Created);
        else return Fail(TEXT("unsupported_asset_kind"), TEXT("Asset kind must be blueprint or data_asset"));
        if (!Valid) return false;
        Package->MarkPackageDirty();
        PendingAssets.Emplace(Created);
        PendingFilenames.Add(Filename);
        PendingReports.Add(Report);
        return true;
    }

    bool Save()
    {
        for (int32 Index = 0; Index < PendingAssets.Num(); ++Index)
        {
            UObject* Asset = PendingAssets[Index].Get();
            const FString& Filename = PendingFilenames[Index];
            CurrentAsset = PendingReports[Index]->GetStringField(TEXT("path"));
            IFileManager::Get().MakeDirectory(*FPaths::GetPath(Filename), true);
            FSavePackageArgs Args;
            Args.TopLevelFlags = RF_Public | RF_Standalone;
            Args.SaveFlags = SAVE_None;
            Args.bSlowTask = false;
            if (!UPackage::SavePackage(Asset->GetOutermost(), Asset, *Filename, Args))
                return Fail(TEXT("asset_save_failed"), FString::Printf(TEXT("Saving '%s' failed; preceding outputs remain identified"), *Filename));
            PendingReports[Index]->SetBoolField(TEXT("saved"), true);
            TSharedRef<FJsonObject> Output = Object();
            Output->SetStringField(TEXT("path"), Filename);
            Output->SetStringField(TEXT("asset"), PendingReports[Index]->GetStringField(TEXT("path")));
            Outputs.Add(MakeShared<FJsonValueObject>(Output));
        }
        return true;
    }
};
}

USN2MCPAuthoringCommandlet::USN2MCPAuthoringCommandlet()
{
    IsClient = false;
    IsEditor = true;
    IsServer = false;
    LogToConsole = true;
    ShowErrorCount = true;
}

int32 USN2MCPAuthoringCommandlet::Main(const FString& Params)
{
    FString InspectClass, InspectReport;
    if (FParse::Value(*Params, TEXT("InspectClass="), InspectClass))
    {
        if (!FParse::Value(*Params, TEXT("Report="), InspectReport) || FPaths::IsRelative(InspectReport))
        {
            UE_LOG(LogSN2MCPAuthoring, Error, TEXT("Class inspection requires -Report=<absolute JSON>"));
            return 1;
        }
        FAuthoring Inspection;
        bool InspectNoCompile = true;
        FString InspectNoCompileValue;
        if (FParse::Value(*Params, TEXT("InspectNoCompile="), InspectNoCompileValue))
        {
            if (InspectNoCompileValue == TEXT("true")) InspectNoCompile = true;
            else if (InspectNoCompileValue == TEXT("false")) InspectNoCompile = false;
            else
            {
                UE_LOG(LogSN2MCPAuthoring, Error, TEXT("InspectNoCompile requires true or false"));
                return 1;
            }
        }
        const uint32 InspectLoadFlags = InspectNoCompile ? LOAD_DisableCompileOnLoad : LOAD_None;
        UClass* LoadedClass = Inspection.Class(InspectClass, InspectLoadFlags);
        TSharedRef<FJsonObject> Report = Object();
        Report->SetNumberField(TEXT("schema"), 1);
        Report->SetStringField(TEXT("kind"), TEXT("sn2_compiled_class_inspection_report"));
        Report->SetStringField(TEXT("class_path"), InspectClass);
        Report->SetBoolField(TEXT("compile_on_load_disabled"), InspectNoCompile);
        Report->SetNumberField(TEXT("load_flags"), InspectLoadFlags);
        Report->SetStringField(TEXT("editor_config_file"), GEditorIni);
        Report->SetStringField(TEXT("state"), LoadedClass ? TEXT("loaded") : TEXT("failed"));
        Report->SetArrayField(TEXT("errors"), Inspection.Errors);
        bool SkipBytecode = false;
        const bool HasSkipConfig = GConfig->GetBool(TEXT("StructSerialization"), TEXT("SkipByteCodeSerialization"), SkipBytecode, GEditorIni);
        Report->SetBoolField(TEXT("skip_bytecode_config_present"), HasSkipConfig);
        Report->SetBoolField(TEXT("skip_bytecode_serialization"), SkipBytecode);
        TArray<TSharedPtr<FJsonValue>> Functions;
        if (LoadedClass)
        {
            Report->SetStringField(TEXT("loaded_class_path"), LoadedClass->GetPathName());
            Report->SetBoolField(TEXT("cooked_for_editor"), LoadedClass->GetOutermost()->bIsCookedForEditor);
            Report->SetStringField(TEXT("package_file"), LoadedClass->GetOutermost()->GetLoadedPath().GetLocalFullPath());
            if (UBlueprint* Blueprint = Cast<UBlueprint>(LoadedClass->ClassGeneratedBy))
            {
                Report->SetNumberField(TEXT("function_graph_count"), Blueprint->FunctionGraphs.Num());
                Report->SetNumberField(TEXT("ubergraph_count"), Blueprint->UbergraphPages.Num());
                Report->SetNumberField(TEXT("declared_variable_count"), Blueprint->NewVariables.Num());
                Report->SetBoolField(TEXT("has_been_regenerated"), Blueprint->bHasBeenRegenerated);
                TArray<TSharedPtr<FJsonValue>> GraphNames;
                for (UEdGraph* Graph : Blueprint->FunctionGraphs)
                    GraphNames.Add(MakeShared<FJsonValueString>(Graph->GetName()));
                Report->SetArrayField(TEXT("function_graphs"), GraphNames);
                TArray<TSharedPtr<FJsonValue>> ResultPins;
                for (UEdGraph* Graph : Blueprint->FunctionGraphs)
                {
                    for (UEdGraphNode* Node : Graph->Nodes)
                    {
                        UK2Node_FunctionResult* Result = Cast<UK2Node_FunctionResult>(Node);
                        if (!Result) continue;
                        for (UEdGraphPin* Pin : Result->Pins)
                        {
                            if (Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec) continue;
                            TSharedRef<FJsonObject> Item = Object();
                            Item->SetStringField(TEXT("function"), Graph->GetName());
                            Item->SetStringField(TEXT("pin"), Pin->PinName.ToString());
                            Item->SetStringField(TEXT("default"), Pin->GetDefaultAsString());
                            Item->SetStringField(TEXT("autogenerated_default"), Pin->AutogeneratedDefaultValue);
                            Item->SetNumberField(TEXT("links"), Pin->LinkedTo.Num());
                            for (const TSharedPtr<FUserPinInfo>& UserPin : Result->UserDefinedPins)
                            {
                                if (UserPin->PinName == Pin->PinName && UserPin->DesiredPinDirection == Pin->Direction)
                                    Item->SetStringField(TEXT("user_defined_default"), UserPin->PinDefaultValue);
                            }
                            ResultPins.Add(MakeShared<FJsonValueObject>(Item));
                        }
                    }
                }
                Report->SetArrayField(TEXT("result_pins"), ResultPins);
            }
            for (TFieldIterator<UFunction> It(LoadedClass, EFieldIteratorFlags::ExcludeSuper); It; ++It)
            {
                UFunction* Function = *It;
                TSharedRef<FJsonObject> Item = Object();
                Item->SetStringField(TEXT("name"), Function->GetName());
                Item->SetStringField(TEXT("owner"), Function->GetOuterUClass()->GetPathName());
                Item->SetNumberField(TEXT("flags"), static_cast<uint32>(Function->FunctionFlags));
                Item->SetNumberField(TEXT("script_bytes"), Function->Script.Num());
                Item->SetStringField(TEXT("script_hex"), BytesToHex(Function->Script.GetData(), Function->Script.Num()));
                Functions.Add(MakeShared<FJsonValueObject>(Item));
            }
        }
        Report->SetArrayField(TEXT("functions"), Functions);
        FString Serialized;
        FJsonSerializer::Serialize(Report, TJsonWriterFactory<>::Create(&Serialized));
        IFileManager::Get().MakeDirectory(*FPaths::GetPath(InspectReport), true);
        if (!FFileHelper::SaveStringToFile(Serialized, *InspectReport, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
        {
            UE_LOG(LogSN2MCPAuthoring, Error, TEXT("Inspection report could not be written: %s"), *InspectReport);
            return 1;
        }
        return LoadedClass ? 0 : 1;
    }
    FString SpecPath, ReportPath, PluginRoot;
    if (!FParse::Value(*Params, TEXT("Spec="), SpecPath) || !FParse::Value(*Params, TEXT("Report="), ReportPath)
        || !FParse::Value(*Params, TEXT("PluginRoot="), PluginRoot))
    {
        UE_LOG(LogSN2MCPAuthoring, Error, TEXT("-Spec=<absolute JSON> -Report=<absolute JSON> -PluginRoot=<captured content plugin> are required"));
        return 1;
    }
    if (FPaths::IsRelative(SpecPath) || FPaths::IsRelative(ReportPath) || FPaths::IsRelative(PluginRoot))
    {
        UE_LOG(LogSN2MCPAuthoring, Error, TEXT("All authoring paths must be absolute"));
        return 1;
    }
    FAuthoring Run;
    Run.PluginRoot = FPaths::ConvertRelativePathToFull(PluginRoot);
    FPaths::NormalizeDirectoryName(Run.PluginRoot);
    Run.ContentRoot = FPaths::Combine(Run.PluginRoot, TEXT("Content"));
    FPaths::NormalizeDirectoryName(Run.ContentRoot);
    FString Raw;
    TSharedPtr<FJsonObject> Spec;
    bool Valid = FFileHelper::LoadFileToString(Raw, *SpecPath) && FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Raw), Spec);
    if (!Valid) Run.Fail(TEXT("invalid_spec"), TEXT("Specification JSON could not be read"));
    double Schema = 0;
    if (Valid && (!Spec->HasTypedField<EJson::Number>(TEXT("schema")) || !Spec->TryGetNumberField(TEXT("schema"), Schema) || Schema != 1)) Valid = Run.Fail(TEXT("invalid_schema"), TEXT("Authoring schema must be 1"));
    if (Valid) Valid = Run.String(Spec, TEXT("plugin_id"), Run.PluginId);
    if (Valid)
    {
        if (Run.PluginId.Contains(TEXT("/")) || Run.PluginId.Contains(TEXT("\\")) || Run.PluginId.Contains(TEXT("."))
            || !IFileManager::Get().FileExists(*FPaths::Combine(Run.PluginRoot, Run.PluginId + TEXT(".uplugin"))))
            Valid = Run.Fail(TEXT("invalid_plugin"), TEXT("Selected plugin root must contain its exact descriptor"));
    }
    if (Valid)
    {
        FPackageName::RegisterMountPoint(TEXT("/") + Run.PluginId + TEXT("/"), Run.ContentRoot + TEXT("/"));
        const TArray<TSharedPtr<FJsonValue>>* Assets = nullptr;
        Valid = Run.Array(Spec, TEXT("assets"), Assets);
        if (Valid && Assets->IsEmpty()) Valid = Run.Fail(TEXT("empty_assets"), TEXT("At least one authored asset is required"));
        if (Valid) for (const auto& Value : *Assets)
        {
            if (!Value.IsValid() || Value->Type != EJson::Object)
            {
                Valid = Run.Fail(TEXT("invalid_asset_spec"), TEXT("Each asset specification must be an object"));
                break;
            }
            if (!Run.Asset(Value->AsObject())) { Valid = false; break; }
        }
        if (Valid) Valid = Run.Save();
    }
    TSharedRef<FJsonObject> Report = Object();
    Report->SetNumberField(TEXT("schema"), 1);
    Report->SetStringField(TEXT("kind"), TEXT("sn2_content_authoring_report"));
    Report->SetStringField(TEXT("state"), Valid ? TEXT("succeeded") : TEXT("failed"));
    Report->SetStringField(TEXT("plugin_id"), Run.PluginId);
    Report->SetStringField(TEXT("spec_path"), SpecPath);
    Report->SetArrayField(TEXT("errors"), Run.Errors);
    Report->SetArrayField(TEXT("assets"), Run.AssetReports);
    Report->SetArrayField(TEXT("outputs"), Run.Outputs);
    FString Serialized;
    FJsonSerializer::Serialize(Report, TJsonWriterFactory<>::Create(&Serialized));
    IFileManager::Get().MakeDirectory(*FPaths::GetPath(ReportPath), true);
    if (!FFileHelper::SaveStringToFile(Serialized, *ReportPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
    {
        UE_LOG(LogSN2MCPAuthoring, Error, TEXT("Authoring report could not be written: %s"), *ReportPath);
        return 1;
    }
    UE_LOG(LogSN2MCPAuthoring, Display, TEXT("Authoring %s: %d saved assets; report=%s"), Valid ? TEXT("succeeded") : TEXT("failed"), Run.Outputs.Num(), *ReportPath);
    return Valid ? 0 : 1;
}
