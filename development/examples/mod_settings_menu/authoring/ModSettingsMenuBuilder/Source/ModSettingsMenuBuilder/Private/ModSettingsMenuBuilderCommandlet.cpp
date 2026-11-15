#include "ModSettingsMenuBuilderCommandlet.h"
#include "ModSettingsMenuStyle.h"

#include "Blueprint/WidgetBlueprintGeneratedClass.h"
#include "Blueprint/WidgetBlueprintLibrary.h"
#include "Blueprint/WidgetTree.h"
#include "Blueprint/UserWidget.h"
#include "Components/CanvasPanel.h"
#include "Components/VerticalBox.h"
#include "Components/Widget.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"
#include "Engine/Blueprint.h"
#include "HAL/FileManager.h"
#include "K2Node_AddDelegate.h"
#include "K2Node_CallArrayFunction.h"
#include "K2Node_CallFunction.h"
#include "K2Node_CallParentFunction.h"
#include "K2Node_ClassDynamicCast.h"
#include "K2Node_CreateDelegate.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_Event.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_GetArrayItem.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Kismet/KismetArrayLibrary.h"
#include "Kismet/KismetMathLibrary.h"
#include "Logging/TokenizedMessage.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/SavePackage.h"
#include "UObject/EnumProperty.h"
#include "UObject/StrongObjectPtr.h"
#include "UObject/UnrealType.h"
#include "WidgetBlueprint.h"

DEFINE_LOG_CATEGORY_STATIC(LogModSettingsMenuBuilder, Log, All);

namespace
{
// Graph construction follows the existing SN2MCPAuthoring commandlet's K2
// creator/schema/compiler/save idioms. This worker belongs to this example mod.
constexpr const TCHAR* PluginId = TEXT("SN2ModSettingsTest");
const FName ButtonMember(TEXT("ModSettingsButton"));
const FName AddedMember(TEXT("ModSettingsInjected"));
const FName FocusMember(TEXT("ReturnFocusTarget"));
const FName BackMember(TEXT("ModSettingsBack"));
const FName ExtensionMember(TEXT("ModMenuExtension"));
const FName PageMember(TEXT("ModSettingsPage"));
constexpr const TCHAR* ExtensionPath = TEXT("/SN2ModSettingsTest/UI/WBP_ModMenuExtension.WBP_ModMenuExtension_C");

struct FBuilder
{
    FString PluginRoot;
    TSharedRef<FJsonObject> Report = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> Errors;
    TArray<TSharedPtr<FJsonValue>> Assets;
    TArray<TStrongObjectPtr<UWidgetBlueprint>> Pending;
    const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();
    UClass* Parent = nullptr;
    UClass* LobbyClass = nullptr;
    UClass* ButtonClass = nullptr;
    UClass* PageParent = nullptr;
    FObjectProperty* ButtonsProperty = nullptr;
    FMulticastDelegateProperty* Click = nullptr;
    UFunction* TextSetter = nullptr;
    FName TextParameter;
    bool Valid = true;
    int32 NodePosition = 0;

    bool Fail(const FString& Code, const FString& Message)
    {
        auto Error = MakeShared<FJsonObject>();
        Error->SetStringField(TEXT("code"), Code);
        Error->SetStringField(TEXT("message"), Message);
        Errors.Add(MakeShared<FJsonValueObject>(Error));
        UE_LOG(LogModSettingsMenuBuilder, Error, TEXT("%s: %s"), *Code, *Message);
        Valid = false;
        return false;
    }

    UClass* LoadClass(const FString& Path)
    {
        UClass* Class = LoadObject<UClass>(nullptr, *Path);
        if (!Class || Class->GetPathName() != Path)
            Fail(TEXT("class_unavailable"), TEXT("The exact class did not load: ") + Path);
        return Class;
    }

    UWidgetTree* Tree(UClass* Class)
    {
        auto* Generated = Cast<UWidgetBlueprintGeneratedClass>(Class);
        auto* Owner = Generated ? Generated->FindWidgetTreeOwningClass() : nullptr;
        UWidgetTree* Result = Owner ? Owner->GetWidgetTreeArchetype() : nullptr;
        if (!Result || !Result->RootWidget)
            Fail(TEXT("widget_tree_unavailable"), TEXT("A nonempty stock widget tree is required: ") + Class->GetPathName());
        return Result;
    }

    UFunction* FindTextSetter(UClass* Class, FName& Parameter)
    {
        const TArray<FName> Preferred = {TEXT("SetButtonText"), TEXT("SetText"), TEXT("SetButtonLabel")};
        for (FName Name : Preferred)
        {
            UFunction* Function = Class->FindFunctionByName(Name);
            if (!Function || !Function->HasAnyFunctionFlags(FUNC_BlueprintCallable)
                || Function->HasAnyFunctionFlags(FUNC_Static | FUNC_EditorOnly) || Function->HasMetaData(TEXT("DevelopmentOnly"))) continue;
            int32 InputCount = 0;
            FTextProperty* Text = nullptr;
            for (TFieldIterator<FProperty> It(Function); It; ++It)
            {
                FProperty* Property = *It;
                if (!Property->HasAnyPropertyFlags(CPF_Parm) || Property->HasAnyPropertyFlags(CPF_ReturnParm)) continue;
                if (Property->HasAnyPropertyFlags(CPF_OutParm) && !Property->HasAnyPropertyFlags(CPF_ConstParm)) { InputCount += 100; continue; }
                ++InputCount;
                Text = CastField<FTextProperty>(Property);
            }
            if (InputCount == 1 && Text) { Parameter = Text->GetFName(); return Function; }
        }
        return nullptr;
    }

    bool Discover(const FString& ParentPath, const FString& LobbyPath, const FString& ButtonName)
    {
        Parent = LoadClass(ParentPath);
        LobbyClass = LoadClass(LobbyPath);
        if (!Parent || !LobbyClass || !Parent->IsChildOf(UUserWidget::StaticClass())) return false;
        UWidgetTree* ParentTree = Tree(Parent);
        UWidgetTree* LobbyTree = Tree(LobbyClass);
        if (!ParentTree || !LobbyTree) return false;
        TArray<UWidget*> Widgets;
        ParentTree->GetAllWidgets(Widgets);
        TArray<TSharedPtr<FJsonValue>> Inventory;
        for (UWidget* Widget : Widgets)
        {
            auto Item = MakeShared<FJsonObject>();
            Item->SetStringField(TEXT("name"), Widget->GetName());
            Item->SetStringField(TEXT("class"), Widget->GetClass()->GetPathName());
            Item->SetStringField(TEXT("path"), Widget->GetPathName());
            Inventory.Add(MakeShared<FJsonValueObject>(Item));
        }
        Report->SetArrayField(TEXT("main_screen_widget_inventory"), Inventory);
        ButtonsProperty = FindFProperty<FObjectProperty>(LobbyClass, TEXT("MainButtons"));
        UWidget* Buttons = LobbyTree->FindWidget(TEXT("MainButtons"));
        UWidget* StockButton = LobbyTree->FindWidget(FName(*ButtonName));
        if (!ButtonsProperty || !Buttons || !Buttons->IsA(UVerticalBox::StaticClass())
            || !ButtonsProperty->PropertyClass->IsChildOf(UVerticalBox::StaticClass()))
            return Fail(TEXT("main_buttons_unavailable"), TEXT("MainButtons must be a reflected stock VerticalBox widget"));
        if (!StockButton || !StockButton->IsA(UUserWidget::StaticClass()))
            return Fail(TEXT("settings_button_unavailable"), TEXT("The requested stock Settings button must be a UserWidget: ") + ButtonName);
        ButtonClass = StockButton->GetClass();
        Click = FindFProperty<FMulticastDelegateProperty>(ButtonClass, TEXT("OnButtonBaseClicked"));
        if (!Click || !Click->SignatureFunction)
            return Fail(TEXT("button_click_delegate_unavailable"), TEXT("The stock button requires its exact reflected OnButtonBaseClicked signature"));
        TextSetter = FindTextSetter(ButtonClass, TextParameter);
        if (!TextSetter)
        {
            TArray<TSharedPtr<FJsonValue>> Functions;
            for (TFieldIterator<UFunction> It(ButtonClass); It; ++It)
                Functions.Add(MakeShared<FJsonValueString>((*It)->GetPathName()));
            Report->SetArrayField(TEXT("stock_button_functions"), Functions);
            return Fail(TEXT("stock_button_text_setter_unavailable"), TEXT("No confirmed single-FText callable SetButtonText/SetText/SetButtonLabel exists on the stock class"));
        }
        auto Discovery = MakeShared<FJsonObject>();
        Discovery->SetStringField(TEXT("parent_class"), Parent->GetPathName());
        Discovery->SetStringField(TEXT("lobby_resolution"), TEXT("GetAllWidgetsOfClass from child Tick; exactly one live widget; TopLevelOnly=false"));
        Discovery->SetStringField(TEXT("lobby_class"), LobbyClass->GetPathName());
        Discovery->SetStringField(TEXT("buttons_property"), ButtonsProperty->GetName());
        Discovery->SetStringField(TEXT("stock_button"), StockButton->GetPathName());
        Discovery->SetStringField(TEXT("stock_button_class"), ButtonClass->GetPathName());
        Discovery->SetStringField(TEXT("click_delegate"), Click->GetPathName());
        Discovery->SetStringField(TEXT("click_signature"), Click->SignatureFunction->GetPathName());
        Discovery->SetStringField(TEXT("text_setter"), TextSetter->GetPathName());
        Discovery->SetStringField(TEXT("text_parameter"), TextParameter.ToString());
        Report->SetObjectField(TEXT("menu_discovery"), Discovery);
        return true;
    }

    template<typename T, typename Setup> T* Node(UEdGraph* Graph, Setup Initialize)
    {
        FGraphNodeCreator<T> Creator(*Graph);
        T* Result = Creator.CreateNode(false);
        Initialize(Result);
        Creator.Finalize();
        Result->NodePosX = (NodePosition++ % 8) * 320;
        Result->NodePosY = (NodePosition / 8) * 240;
        return Result;
    }
    template<typename T> T* Node(UEdGraph* Graph) { return Node<T>(Graph, [](T*) {}); }

    UEdGraphPin* Pin(UEdGraphNode* NodeValue, FName Name, EEdGraphPinDirection Direction)
    {
        UEdGraphPin* Result = NodeValue ? NodeValue->FindPin(Name, Direction) : nullptr;
        if (!Result) Fail(TEXT("graph_pin_unavailable"), FString::Printf(TEXT("Missing pin %s on %s"), *Name.ToString(), NodeValue ? *NodeValue->GetClass()->GetName() : TEXT("null")));
        return Result;
    }
    bool Link(UEdGraphPin* From, UEdGraphPin* To)
    {
        if (!From || !To) return false;
        if (!Schema->TryCreateConnection(From, To))
            return Fail(TEXT("graph_connection_invalid"), From->GetOwningNode()->GetName() + TEXT(".") + From->PinName.ToString() + TEXT(" -> ") + To->GetOwningNode()->GetName() + TEXT(".") + To->PinName.ToString());
        return true;
    }
    UEdGraphPin* Exec(UEdGraphNode* Value) { return Pin(Value, UEdGraphSchema_K2::PN_Execute, EGPD_Input); }
    UEdGraphPin* Then(UEdGraphNode* Value) { return Pin(Value, UEdGraphSchema_K2::PN_Then, EGPD_Output); }

    UK2Node_CallFunction* Call(UEdGraph* Graph, UFunction* Function)
    {
        if (!Function) { Fail(TEXT("function_unavailable"), TEXT("A requested reflected function is unavailable")); return nullptr; }
        return Node<UK2Node_CallFunction>(Graph, [Function](auto* Value) { Value->SetFromFunction(Function); });
    }
    UK2Node_CallFunction* Call(UEdGraph* Graph, UClass* Class, FName Name)
    {
        return Call(Graph, Class ? Class->FindFunctionByName(Name) : nullptr);
    }
    UK2Node_VariableGet* Get(UEdGraph* Graph, FProperty* Property, bool Self)
    {
        if (!Property) { Fail(TEXT("widget_property_unavailable"), TEXT("The selected reflected widget property is unavailable")); return nullptr; }
        return Node<UK2Node_VariableGet>(Graph, [Property, Self](auto* Value) { Value->SetFromProperty(Property, Self, Property->GetOwnerClass()); });
    }
    UK2Node_VariableGet* Get(UEdGraph* Graph, FName Name)
    {
        return Node<UK2Node_VariableGet>(Graph, [Name](auto* Value) { Value->VariableReference.SetSelfMember(Name); });
    }
    UK2Node_VariableSet* Set(UEdGraph* Graph, FName Name)
    {
        return Node<UK2Node_VariableSet>(Graph, [Name](auto* Value) { Value->VariableReference.SetSelfMember(Name); });
    }
    UK2Node_VariableSet* Set(UEdGraph* Graph, FProperty* Property)
    {
        if (!Property) { Fail(TEXT("widget_property_unavailable"), TEXT("The selected reflected widget property is unavailable")); return nullptr; }
        return Node<UK2Node_VariableSet>(Graph, [Property](auto* Value) { Value->SetFromProperty(Property, false, Property->GetOwnerClass()); });
    }
    UEdGraphPin* Value(UEdGraphNode* NodeValue, FName Name) { return Pin(NodeValue, Name, EGPD_Output); }
    UEdGraphPin* Target(UEdGraphNode* NodeValue) { return Pin(NodeValue, UEdGraphSchema_K2::PN_Self, EGPD_Input); }
    UEdGraphPin* TypedValue(UEdGraph* Graph, UClass* Class, UEdGraphPin* Source)
    {
        auto* CastNode = Node<UK2Node_DynamicCast>(Graph, [Class](auto* Value) { Value->TargetType = Class; Value->SetPurity(true); });
        Link(Source, CastNode->GetCastSourcePin());
        return CastNode->GetCastResultPin();
    }
    UK2Node_CallFunction* CreateWidget(UEdGraph* Graph, UClass* Class)
    {
        auto* Create = Call(Graph, UWidgetBlueprintLibrary::StaticClass(), TEXT("Create"));
        if (auto* ClassPin = Pin(Create, TEXT("WidgetType"), EGPD_Input)) Schema->TrySetDefaultObject(*ClassPin, Class);
        auto* Player = Call(Graph, UUserWidget::StaticClass(), TEXT("GetOwningPlayer"));
        Link(Player ? Player->GetReturnValuePin() : nullptr, Pin(Create, TEXT("OwningPlayer"), EGPD_Input));
        return Create;
    }

    UWidgetBlueprint* Blueprint(const TCHAR* Name, UClass* Super)
    {
        const FString PackageName = FString::Printf(TEXT("/%s/UI/%s"), PluginId, Name);
        const FString Filename = FPaths::Combine(PluginRoot, TEXT("Content/UI"), FString(Name) + TEXT(".uasset"));
        if (FindObject<UPackage>(nullptr, *PackageName) || IFileManager::Get().FileExists(*Filename))
        { Fail(TEXT("asset_exists"), TEXT("The builder requires an isolated destination: ") + Filename); return nullptr; }
        auto* Package = CreatePackage(*PackageName);
        auto* Result = Cast<UWidgetBlueprint>(FKismetEditorUtilities::CreateBlueprint(Super, Package, FName(Name), BPTYPE_Normal,
            UWidgetBlueprint::StaticClass(), UWidgetBlueprintGeneratedClass::StaticClass(), TEXT("ModSettingsMenuBuilder")));
        if (!Result || !Result->WidgetTree) { Fail(TEXT("widget_blueprint_creation_failed"), PackageName); return nullptr; }
        Pending.Emplace(Result);
        Result->WidgetTree->RootWidget = nullptr;
        return Result;
    }
    bool Member(UWidgetBlueprint* BP, FName Name, UClass* Class)
    {
        FEdGraphPinType Type;
        Type.PinCategory = UEdGraphSchema_K2::PC_Object;
        Type.PinSubCategoryObject = Class;
        if (!FBlueprintEditorUtils::AddMemberVariable(BP, Name, Type)) return Fail(TEXT("member_creation_failed"), Name.ToString());
        return true;
    }
    UEdGraph* EventGraph(UWidgetBlueprint* BP)
    {
        UEdGraph* Graph = BP->UbergraphPages.Num() ? BP->UbergraphPages[0].Get() : nullptr;
        if (!Graph)
        {
            Graph = FBlueprintEditorUtils::CreateNewGraph(BP, TEXT("EventGraph"), UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
            FBlueprintEditorUtils::AddUbergraphPage(BP, Graph);
        }
        TArray<TObjectPtr<UEdGraphNode>> Existing = Graph->Nodes;
        for (UEdGraphNode* Value : Existing) Graph->RemoveNode(Value);
        return Graph;
    }
    UK2Node_Event* Construct(UEdGraph* Graph, UClass* Super)
    {
        UFunction* Function = Super->FindFunctionByName(TEXT("Construct"));
        if (!Function) { Fail(TEXT("construct_event_unavailable"), Super->GetPathName()); return nullptr; }
        return Node<UK2Node_Event>(Graph, [Function](auto* Value) { Value->EventReference.SetExternalMember(Function->GetFName(), Function->GetOwnerClass()); Value->bOverrideFunction = true; });
    }
    struct FFunction { UEdGraph* Graph; UK2Node_FunctionEntry* Entry; UK2Node_FunctionResult* Result; };
    FFunction Function(UWidgetBlueprint* BP, FName Name, UFunction* Signature)
    {
        UEdGraph* Graph = FBlueprintEditorUtils::CreateNewGraph(BP, Name, UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
        FBlueprintEditorUtils::AddFunctionGraph<UFunction>(BP, Graph, true, Signature);
        UK2Node_FunctionEntry* Entry = nullptr;
        UK2Node_FunctionResult* Result = nullptr;
        for (UEdGraphNode* Value : Graph->Nodes)
        {
            if (auto* Found = Cast<UK2Node_FunctionEntry>(Value)) Entry = Found;
            if (auto* Found = Cast<UK2Node_FunctionResult>(Value)) Result = Found;
        }
        if (!Entry) Fail(TEXT("function_entry_unavailable"), Name.ToString());
        else { Entry->FunctionReference.SetSelfMember(Name); Entry->SetExtraFlags(FUNC_Public | FUNC_BlueprintCallable); }
        if (Result && Entry) Result->FunctionReference = Entry->FunctionReference;
        if (!Result)
            Result = Node<UK2Node_FunctionResult>(Graph, [Entry](auto* Value) { if (Entry) Value->FunctionReference = Entry->FunctionReference; Value->bIsEditable = true; });
        return {Graph, Entry, Result};
    }
    UK2Node_AddDelegate* Bind(UEdGraph* Graph, FMulticastDelegateProperty* Delegate, UEdGraphPin* Button, FName Handler)
    {
        auto* Add = Node<UK2Node_AddDelegate>(Graph, [Delegate](auto* Value) { Value->SetFromProperty(Delegate, false, Delegate->GetOwnerClass()); });
        auto* Event = Node<UK2Node_CreateDelegate>(Graph);
        Link(Button, Target(Add));
        Link(Event->GetDelegateOutPin(), Add->GetDelegatePin());
        Event->SetFunction(Handler);
        return Add;
    }
    bool Compile(UWidgetBlueprint* BP)
    {
        if (!Valid) return false;
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        FCompilerResultsLog Log;
        Log.SetSourcePath(BP->GetPathName());
        FKismetEditorUtilities::CompileBlueprint(BP, EBlueprintCompileOptions::None, &Log);
        auto Item = MakeShared<FJsonObject>();
        Item->SetStringField(TEXT("asset"), BP->GetPathName());
        Item->SetStringField(TEXT("parent"), BP->ParentClass->GetPathName());
        Item->SetBoolField(TEXT("own_widget_tree"), BP->WidgetTree && BP->WidgetTree->RootWidget);
        Item->SetNumberField(TEXT("errors"), Log.NumErrors);
        Item->SetNumberField(TEXT("warnings"), Log.NumWarnings);
        TArray<TSharedPtr<FJsonValue>> Messages;
        for (const auto& Message : Log.Messages) Messages.Add(MakeShared<FJsonValueString>(Message->ToText().ToString()));
        Item->SetArrayField(TEXT("messages"), Messages);
        TArray<TSharedPtr<FJsonValue>> Functions;
        if (BP->GeneratedClass) for (TFieldIterator<UFunction> It(BP->GeneratedClass, EFieldIteratorFlags::ExcludeSuper); It; ++It)
        {
            auto Body = MakeShared<FJsonObject>();
            Body->SetStringField(TEXT("name"), It->GetName());
            Body->SetNumberField(TEXT("script_bytes"), It->Script.Num());
            Functions.Add(MakeShared<FJsonValueObject>(Body));
        }
        Item->SetArrayField(TEXT("functions"), Functions);
        Assets.Add(MakeShared<FJsonValueObject>(Item));
        if (Log.NumErrors || BP->Status == BS_Error || !BP->GeneratedClass)
            return Fail(TEXT("blueprint_compile_failed"), BP->GetPathName());
        return true;
    }

    bool Page(UWidgetBlueprint* BP, const ModSettingsMenuStyle::FSelection& Style)
    {
        if (!Member(BP, FocusMember, UWidget::StaticClass())) return false;
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);
        auto* BackClick = FindFProperty<FMulticastDelegateProperty>(Style.BackClass, TEXT("OnButtonBaseClicked"));
        if (!BackClick || !BackClick->SignatureFunction) return Fail(TEXT("settings_back_delegate_unavailable"), TEXT("The selected back widget click signature became unavailable"));
        auto Close = Function(BP, TEXT("CloseModSettings"), BackClick->SignatureFunction);
        auto* Deactivate = Call(Close.Graph, PageParent, TEXT("DeactivateWidget"));
        auto* Remove = Call(Close.Graph, UWidget::StaticClass(), TEXT("RemoveFromParent"));
        auto* Focus = Call(Close.Graph, UWidget::StaticClass(), TEXT("SetFocus"));
        auto* FocusValue = Get(Close.Graph, FocusMember);
        Link(Value(FocusValue, FocusMember), Target(Focus));
        Link(Then(Close.Entry), Exec(Deactivate)); Link(Then(Deactivate), Exec(Remove));
        Link(Then(Remove), Exec(Focus)); Link(Then(Focus), Exec(Close.Result));
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);
        UEdGraph* Graph = EventGraph(BP);
        auto* Event = Construct(Graph, PageParent);
        auto* Back = Get(Graph, BackMember);
        FName BackTextParameter;
        UFunction* BackTextSetter = FindTextSetter(Style.BackClass, BackTextParameter);
        if (!BackTextSetter) return Fail(TEXT("back_text_setter_unavailable"), TEXT("The selected stock Back class requires a confirmed single-FText callable setter"));
        auto* BackLabel = Call(Graph, BackTextSetter);
        Link(Value(Back, BackMember), Target(BackLabel));
        if (auto* Text = Pin(BackLabel, BackTextParameter, EGPD_Input)) Schema->TrySetDefaultText(*Text, FText::FromString(TEXT("Назад")));
        auto* BindBack = Bind(Graph, BackClick, Value(Back, BackMember), TEXT("CloseModSettings"));
        Link(Then(Event), Exec(BackLabel));
        Link(Then(BackLabel), Exec(BindBack));
        Report->SetStringField(TEXT("back_text_setter"), BackTextSetter->GetPathName());
        Report->SetStringField(TEXT("back_text_parameter"), BackTextParameter.ToString());
        auto* FocusBack = Call(Graph, UWidget::StaticClass(), TEXT("SetFocus"));
        Link(Value(Back, BackMember), Target(FocusBack));
        Link(Then(BindBack), Exec(FocusBack));
        return Compile(BP);
    }

    bool EnableTick(UWidgetBlueprint* BP, const TCHAR* ReportField)
    {
        auto* Default = Cast<UUserWidget>(BP->GeneratedClass->GetDefaultObject());
        auto* Frequency = FindFProperty<FEnumProperty>(BP->GeneratedClass, TEXT("TickFrequency"));
        if (!Default || !Frequency || !Frequency->GetUnderlyingProperty())
            return Fail(TEXT("tick_frequency_unavailable"), TEXT("The generated widget TickFrequency enum/default object is unavailable: ") + BP->GetPathName());
        Frequency->GetUnderlyingProperty()->SetIntPropertyValue(Frequency->ContainerPtrToValuePtr<void>(Default), static_cast<uint64>(EWidgetTickFrequency::Auto));
        if (Default->GetDesiredTickFrequency() != EWidgetTickFrequency::Auto)
            return Fail(TEXT("tick_frequency_not_enabled"), TEXT("The generated widget did not retain automatic ticking: ") + BP->GetPathName());
        Report->SetStringField(ReportField, TEXT("Auto"));
        return true;
    }

    UK2Node_Event* WidgetEvent(UEdGraph* Graph, UClass* Super, FName Name)
    {
        UFunction* Signature = Super->FindFunctionByName(Name);
        if (!Signature || !Signature->HasAnyFunctionFlags(FUNC_BlueprintEvent))
        { Fail(TEXT("widget_event_unavailable"), Super->GetPathName() + TEXT(".") + Name.ToString()); return nullptr; }
        return Node<UK2Node_Event>(Graph, [Signature](auto* Value) {
            Value->EventReference.SetExternalMember(Signature->GetFName(), Signature->GetOwnerClass());
            Value->bOverrideFunction = true;
        });
    }

    UK2Node_CallParentFunction* Forward(UEdGraph* Graph, UK2Node_Event* Event, UClass* Super, FName Name)
    {
        UFunction* Signature = Super->FindFunctionByName(Name);
        if (!Signature) { Fail(TEXT("parent_event_unavailable"), Super->GetPathName() + TEXT(".") + Name.ToString()); return nullptr; }
        auto* ParentCall = Node<UK2Node_CallParentFunction>(Graph, [Signature](auto* Value) { Value->SetFromFunction(Signature); });
        for (TFieldIterator<FProperty> It(Signature); It; ++It)
            if (It->HasAnyPropertyFlags(CPF_Parm) && !It->HasAnyPropertyFlags(CPF_ReturnParm)
                && !(It->HasAnyPropertyFlags(CPF_OutParm) && !It->HasAnyPropertyFlags(CPF_ConstParm)))
                Link(Value(Event, It->GetFName()), Pin(ParentCall, It->GetFName(), EGPD_Input));
        Link(Then(Event), Exec(ParentCall));
        return ParentCall;
    }

    bool Main(UWidgetBlueprint* BP)
    {
        if (!Member(BP, ExtensionMember, UUserWidget::StaticClass())) return false;
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);
        UEdGraph* Graph = EventGraph(BP);
        auto* ConstructEvent = WidgetEvent(Graph, Parent, TEXT("Construct"));
        Forward(Graph, ConstructEvent, Parent, TEXT("Construct"));
        auto* TickEvent = WidgetEvent(Graph, Parent, TEXT("Tick"));
        auto* ParentTick = Forward(Graph, TickEvent, Parent, TEXT("Tick"));
        auto* Extension = Get(Graph, ExtensionMember);
        auto* ValidExtension = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(Value(Extension, ExtensionMember), Pin(ValidExtension, TEXT("Object"), EGPD_Input));
        auto* Existing = Node<UK2Node_IfThenElse>(Graph);
        Link(ValidExtension->GetReturnValuePin(), Pin(Existing, TEXT("Condition"), EGPD_Input));
        Link(Then(ParentTick), Exec(Existing));
        auto* Load = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("LoadClassAsset_Blocking"));
        UEdGraphPin* SoftClass = Pin(Load, TEXT("AssetClass"), EGPD_Input);
        if (!SoftClass || SoftClass->PinType.PinCategory != UEdGraphSchema_K2::PC_SoftClass)
            return Fail(TEXT("extension_soft_reference_invalid"), TEXT("The extension loader requires the actual soft class pin"));
        Schema->TrySetDefaultValue(*SoftClass, ExtensionPath);
        if (SoftClass->DefaultObject || SoftClass->DefaultValue != ExtensionPath
            || !Schema->IsPinDefaultValid(SoftClass, SoftClass->DefaultValue, SoftClass->DefaultObject, SoftClass->DefaultTextValue).IsEmpty())
            return Fail(TEXT("extension_soft_reference_invalid"), TEXT("The extension must remain a soft class path with no object literal"));
        auto* NativeClass = Node<UK2Node_ClassDynamicCast>(Graph, [](auto* Value) {
            Value->TargetType = UUserWidget::StaticClass(); Value->SetPurity(true);
        });
        Link(Load->GetReturnValuePin(), NativeClass->GetCastSourcePin());
        auto* Loaded = Node<UK2Node_IfThenElse>(Graph);
        Link(NativeClass->GetBoolSuccessPin(), Pin(Loaded, TEXT("Condition"), EGPD_Input));
        auto* Create = Call(Graph, UWidgetBlueprintLibrary::StaticClass(), TEXT("Create"));
        Link(NativeClass->GetCastResultPin(), Pin(Create, TEXT("WidgetType"), EGPD_Input));
        auto* Player = Call(Graph, UUserWidget::StaticClass(), TEXT("GetOwningPlayer"));
        Link(Player->GetReturnValuePin(), Pin(Create, TEXT("OwningPlayer"), EGPD_Input));
        auto* Store = Set(Graph, ExtensionMember);
        Link(Create->GetReturnValuePin(), Pin(Store, ExtensionMember, EGPD_Input));
        auto* CreatedValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(Create->GetReturnValuePin(), Pin(CreatedValid, TEXT("Object"), EGPD_Input));
        auto* Created = Node<UK2Node_IfThenElse>(Graph);
        Link(CreatedValid->GetReturnValuePin(), Pin(Created, TEXT("Condition"), EGPD_Input));
        auto* Show = Call(Graph, UUserWidget::StaticClass(), TEXT("AddToViewport"));
        Link(Create->GetReturnValuePin(), Target(Show));
        if (auto* ZOrder = Pin(Show, TEXT("ZOrder"), EGPD_Input)) Schema->TrySetDefaultValue(*ZOrder, TEXT("0"));
        Link(Pin(Existing, TEXT("else"), EGPD_Output), Exec(Load));
        Link(Then(Load), Exec(Loaded)); Link(Then(Loaded), Exec(Create));
        Link(Then(Create), Exec(Store)); Link(Then(Store), Exec(Created)); Link(Then(Created), Exec(Show));
        auto* Destruct = WidgetEvent(Graph, Parent, TEXT("Destruct"));
        auto* Remove = Call(Graph, UWidget::StaticClass(), TEXT("RemoveFromParent"));
        Link(Value(Extension, ExtensionMember), Target(Remove));
        auto* WasCreated = Node<UK2Node_IfThenElse>(Graph);
        Link(ValidExtension->GetReturnValuePin(), Pin(WasCreated, TEXT("Condition"), EGPD_Input));
        auto* ParentDestruct = Forward(Graph, Destruct, Parent, TEXT("Destruct"));
        Link(Then(ParentDestruct), Exec(WasCreated)); Link(Then(WasCreated), Exec(Remove));
        auto* Clear = Set(Graph, ExtensionMember);
        Link(Then(Remove), Exec(Clear));
        if (!Compile(BP) || !EnableTick(BP, TEXT("main_screen_tick_frequency"))) return false;
        Report->SetStringField(TEXT("extension_soft_class_path"), ExtensionPath);
        return VerifyMainReferences(BP, Report);
    }

    bool VerifyMainReferences(UWidgetBlueprint* BP, const TSharedRef<FJsonObject>& TargetReport)
    {
        const TArray<FString> ForbiddenPackages = {
            TEXT("/SN2ModSettingsTest/UI/WBP_ModMenuExtension"),
            TEXT("/SN2ModSettingsTest/UI/WBP_ModSettingsPage"),
            TEXT("/Game/Blueprints/UI/Lobby/WBP_MainLobbyScreen"),
            TEXT("/Game/Blueprints/UI/GenericUIElements/GenericUI_WBP/WBP_GenericButtonBig")
        };
        TArray<TSharedPtr<FJsonValue>> References;
        auto Check = [&](UObject* Reference) {
            if (!Reference) return true;
            const FString Path = Reference->GetPathName();
            References.Add(MakeShared<FJsonValueString>(Path));
            for (const FString& Package : ForbiddenPackages)
                if (Path.StartsWith(Package + TEXT(".")) || Path == Package)
                    return Fail(TEXT("main_screen_hard_dependency"), TEXT("The startup wrapper must defer this direct reference: ") + Path);
            return true;
        };
        for (UObject* Reference : BP->GeneratedClass->ScriptAndPropertyObjectReferences)
            if (!Check(Reference)) return false;
        for (TFieldIterator<UFunction> It(BP->GeneratedClass, EFieldIteratorFlags::ExcludeSuper); It; ++It)
            for (UObject* Reference : It->ScriptAndPropertyObjectReferences)
                if (!Check(Reference)) return false;
        for (TFieldIterator<FObjectPropertyBase> It(BP->GeneratedClass, EFieldIteratorFlags::ExcludeSuper); It; ++It)
            if (!Check(It->PropertyClass)) return false;
        TargetReport->SetArrayField(TEXT("main_screen_direct_script_property_references"), References);
        TargetReport->SetBoolField(TEXT("main_screen_extension_hard_reference"), false);
        return true;
    }

    bool Extension(UWidgetBlueprint* BP, UWidgetBlueprint* PageBP)
    {
        if (!Member(BP, ButtonMember, ButtonClass) || !Member(BP, PageMember, UUserWidget::StaticClass())) return false;
        auto* EmptyRoot = BP->WidgetTree->ConstructWidget<UCanvasPanel>(UCanvasPanel::StaticClass(), TEXT("ModMenuExtensionRoot"));
        EmptyRoot->SetVisibility(ESlateVisibility::HitTestInvisible);
        BP->WidgetTree->RootWidget = EmptyRoot;
        FEdGraphPinType Bool;
        Bool.PinCategory = UEdGraphSchema_K2::PC_Boolean;
        if (!FBlueprintEditorUtils::AddMemberVariable(BP, AddedMember, Bool, TEXT("false"))) return Fail(TEXT("member_creation_failed"), AddedMember.ToString());
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);
        auto Open = Function(BP, TEXT("OpenModSettings"), Click->SignatureFunction);
        auto* NewPage = CreateWidget(Open.Graph, PageBP->GeneratedClass);
        auto* StorePage = Set(Open.Graph, PageMember);
        Link(NewPage->GetReturnValuePin(), Pin(StorePage, PageMember, EGPD_Input));
        UEdGraphPin* PageValue = TypedValue(Open.Graph, PageBP->GeneratedClass, NewPage ? NewPage->GetReturnValuePin() : nullptr);
        auto* FocusProperty = FindFProperty<FObjectProperty>(PageBP->GeneratedClass, FocusMember);
        auto* StoreFocus = Set(Open.Graph, FocusProperty);
        auto* ButtonValue = Get(Open.Graph, ButtonMember);
        Link(PageValue, Target(StoreFocus)); Link(Value(ButtonValue, ButtonMember), Pin(StoreFocus, FocusMember, EGPD_Input));
        auto* Show = Call(Open.Graph, UUserWidget::StaticClass(), TEXT("AddToViewport"));
        Link(PageValue, Target(Show));
        if (auto* ZOrder = Pin(Show, TEXT("ZOrder"), EGPD_Input)) Schema->TrySetDefaultValue(*ZOrder, TEXT("100"));
        auto* Activate = Call(Open.Graph, PageParent, TEXT("ActivateWidget")); Link(PageValue, Target(Activate));
        Link(Then(Open.Entry), Exec(NewPage)); Link(Then(NewPage), Exec(StorePage)); Link(Then(StorePage), Exec(StoreFocus)); Link(Then(StoreFocus), Exec(Show));
        Link(Then(Show), Exec(Activate)); Link(Then(Activate), Exec(Open.Result));
        FKismetEditorUtilities::GenerateBlueprintSkeleton(BP, true);

        UEdGraph* Graph = EventGraph(BP);
        auto* Tick = WidgetEvent(Graph, UUserWidget::StaticClass(), TEXT("Tick"));
        auto* IsAdded = Get(Graph, AddedMember);
        auto* Branch = Node<UK2Node_IfThenElse>(Graph);
        Link(Then(Tick), Exec(Branch));
        Link(Value(IsAdded, AddedMember), Pin(Branch, TEXT("Condition"), EGPD_Input));
        auto* FindLobby = Call(Graph, UWidgetBlueprintLibrary::StaticClass(), TEXT("GetAllWidgetsOfClass"));
        if (auto* ClassPin = Pin(FindLobby, TEXT("WidgetClass"), EGPD_Input)) Schema->TrySetDefaultObject(*ClassPin, LobbyClass);
        if (auto* TopLevel = Pin(FindLobby, TEXT("TopLevelOnly"), EGPD_Input)) Schema->TrySetDefaultValue(*TopLevel, TEXT("false"));
        UFunction* LengthFunction = UKismetArrayLibrary::StaticClass()->FindFunctionByName(TEXT("Array_Length"));
        if (!LengthFunction) return Fail(TEXT("array_length_unavailable"), TEXT("The actual Kismet array length function is unavailable"));
        auto* Length = Node<UK2Node_CallArrayFunction>(Graph, [LengthFunction](auto* Value) { Value->SetFromFunction(LengthFunction); });
        UEdGraphPin* Found = Value(FindLobby, TEXT("FoundWidgets"));
        Link(Found, Pin(Length, TEXT("TargetArray"), EGPD_Input));
        auto* CountOne = Call(Graph, UKismetMathLibrary::StaticClass(), TEXT("EqualEqual_IntInt"));
        Link(Length->GetReturnValuePin(), Pin(CountOne, TEXT("A"), EGPD_Input));
        if (auto* One = Pin(CountOne, TEXT("B"), EGPD_Input)) Schema->TrySetDefaultValue(*One, TEXT("1"));
        auto* Unique = Node<UK2Node_IfThenElse>(Graph);
        Link(CountOne ? CountOne->GetReturnValuePin() : nullptr, Pin(Unique, TEXT("Condition"), EGPD_Input));
        auto* Item = Node<UK2Node_GetArrayItem>(Graph);
        // Object arrays force a value output when linked; the non-exported
        // SetDesiredReturnType method is unnecessary for UObject pointers.
        Link(Found, Item->GetTargetArrayPin());
        if (!Item->GetResultPin() || Item->GetResultPin()->PinType.bIsReference)
            return Fail(TEXT("array_get_reference_unexpected"), TEXT("The object-array Get result must specialize to a value pin"));
        if (auto* Index = Item->GetIndexPin()) Schema->TrySetDefaultValue(*Index, TEXT("0"));
        UEdGraphPin* LobbyValue = TypedValue(Graph, LobbyClass, Item->GetResultPin());
        auto* LobbyValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(LobbyValue, Pin(LobbyValid, TEXT("Object"), EGPD_Input));
        auto* Live = Node<UK2Node_IfThenElse>(Graph);
        Link(LobbyValid ? LobbyValid->GetReturnValuePin() : nullptr, Pin(Live, TEXT("Condition"), EGPD_Input));
        auto* Box = Get(Graph, ButtonsProperty, false); Link(LobbyValue, Target(Box));
        auto* BoxValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(Value(Box, ButtonsProperty->GetFName()), Pin(BoxValid, TEXT("Object"), EGPD_Input));
        auto* Available = Node<UK2Node_IfThenElse>(Graph);
        Link(BoxValid ? BoxValid->GetReturnValuePin() : nullptr, Pin(Available, TEXT("Condition"), EGPD_Input));
        auto* Create = CreateWidget(Graph, ButtonClass);
        UEdGraphPin* NewButton = TypedValue(Graph, ButtonClass, Create ? Create->GetReturnValuePin() : nullptr);
        auto* Store = Set(Graph, ButtonMember); Link(NewButton, Pin(Store, ButtonMember, EGPD_Input));
        auto* Label = Call(Graph, TextSetter); Link(NewButton, Target(Label));
        if (auto* Text = Pin(Label, TextParameter, EGPD_Input)) Schema->TrySetDefaultText(*Text, FText::FromString(TEXT("Настройки модов")));
        auto* AddChild = Call(Graph, UVerticalBox::StaticClass(), TEXT("AddChildToVerticalBox"));
        Link(Value(Box, ButtonsProperty->GetFName()), Target(AddChild)); Link(NewButton, Pin(AddChild, TEXT("Content"), EGPD_Input));
        auto* AddBinding = Bind(Graph, Click, NewButton, TEXT("OpenModSettings"));
        auto* SlotValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(AddChild ? AddChild->GetReturnValuePin() : nullptr, Pin(SlotValid, TEXT("Object"), EGPD_Input));
        auto* Attached = Node<UK2Node_IfThenElse>(Graph);
        Link(SlotValid ? SlotValid->GetReturnValuePin() : nullptr, Pin(Attached, TEXT("Condition"), EGPD_Input));
        auto* Mark = Set(Graph, AddedMember);
        if (auto* MarkPin = Pin(Mark, AddedMember, EGPD_Input)) Schema->TrySetDefaultValue(*MarkPin, TEXT("true"));
        Link(Pin(Branch, TEXT("else"), EGPD_Output), Exec(FindLobby)); Link(Then(FindLobby), Exec(Unique));
        Link(Then(Unique), Exec(Live)); Link(Then(Live), Exec(Available)); Link(Then(Available), Exec(Create)); Link(Then(Create), Exec(Store));
        Link(Then(Store), Exec(Label)); Link(Then(Label), Exec(AddChild)); Link(Then(AddChild), Exec(Attached));
        Link(Then(Attached), Exec(AddBinding));
        Link(Then(AddBinding), Exec(Mark));
        auto* Destruct = WidgetEvent(Graph, UUserWidget::StaticClass(), TEXT("Destruct"));
        auto* SavedPage = Get(Graph, PageMember);
        auto* PageValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(Value(SavedPage, PageMember), Pin(PageValid, TEXT("Object"), EGPD_Input));
        auto* HasPage = Node<UK2Node_IfThenElse>(Graph);
        Link(PageValid->GetReturnValuePin(), Pin(HasPage, TEXT("Condition"), EGPD_Input));
        Link(Then(Destruct), Exec(HasPage));
        auto* DeactivatePage = Call(Graph, PageParent, TEXT("DeactivateWidget"));
        Link(TypedValue(Graph, PageParent, Value(SavedPage, PageMember)), Target(DeactivatePage));
        Link(Then(HasPage), Exec(DeactivatePage));
        auto* RemovePage = Call(Graph, UWidget::StaticClass(), TEXT("RemoveFromParent"));
        Link(Value(SavedPage, PageMember), Target(RemovePage)); Link(Then(DeactivatePage), Exec(RemovePage));
        auto* SavedButton = Get(Graph, ButtonMember);
        auto* ButtonValid = Call(Graph, UKismetSystemLibrary::StaticClass(), TEXT("IsValid"));
        Link(Value(SavedButton, ButtonMember), Pin(ButtonValid, TEXT("Object"), EGPD_Input));
        auto* HasButton = Node<UK2Node_IfThenElse>(Graph);
        Link(ButtonValid->GetReturnValuePin(), Pin(HasButton, TEXT("Condition"), EGPD_Input));
        Link(Pin(HasPage, TEXT("else"), EGPD_Output), Exec(HasButton)); Link(Then(RemovePage), Exec(HasButton));
        auto* RemoveButton = Call(Graph, UWidget::StaticClass(), TEXT("RemoveFromParent"));
        Link(Value(SavedButton, ButtonMember), Target(RemoveButton)); Link(Then(HasButton), Exec(RemoveButton));
        if (!Compile(BP) || !EnableTick(BP, TEXT("menu_extension_tick_frequency"))) return false;
        auto* Default = Cast<UUserWidget>(BP->GeneratedClass->GetDefaultObject());
        Default->SetVisibility(ESlateVisibility::HitTestInvisible);
        Report->SetStringField(TEXT("menu_extension_parent"), UUserWidget::StaticClass()->GetPathName());
        Report->SetStringField(TEXT("menu_extension_root_visibility"), TEXT("HitTestInvisible"));
        return true;
    }

    bool Save()
    {
        if (!Valid) return false;
        TArray<TSharedPtr<FJsonValue>> Outputs;
        for (const auto& Held : Pending)
        {
            UWidgetBlueprint* BP = Held.Get();
            const FString Filename = FPaths::Combine(PluginRoot, TEXT("Content/UI"), BP->GetName() + TEXT(".uasset"));
            IFileManager::Get().MakeDirectory(*FPaths::GetPath(Filename), true);
            FSavePackageArgs Args;
            Args.TopLevelFlags = RF_Public | RF_Standalone;
            Args.SaveFlags = SAVE_None;
            Args.bSlowTask = false;
            BP->GetOutermost()->MarkPackageDirty();
            if (!UPackage::SavePackage(BP->GetOutermost(), BP, *Filename, Args))
                return Fail(TEXT("asset_save_failed"), TEXT("Could not save selected asset: ") + Filename);
            auto Item = MakeShared<FJsonObject>();
            Item->SetStringField(TEXT("asset"), BP->GetPathName());
            Item->SetStringField(TEXT("generated_class"), BP->GeneratedClass->GetPathName());
            Item->SetStringField(TEXT("filename"), Filename);
            Item->SetNumberField(TEXT("bytes"), IFileManager::Get().FileSize(*Filename));
            Outputs.Add(MakeShared<FJsonValueObject>(Item));
            Report->SetArrayField(TEXT("outputs"), Outputs);
        }
        Report->SetBoolField(TEXT("saved"), true);
        return true;
    }

    bool ReloadSaved(const FString& ParentPath, const FString& PageParentPath)
    {
        FPackageName::RegisterMountPoint(TEXT("/") + FString(PluginId) + TEXT("/"), FPaths::Combine(PluginRoot, TEXT("Content/")));
        struct FExpected { const TCHAR* Name; FString ParentPath; bool OwnRoot; bool Ticking; TArray<FName> Functions; };
        const TArray<FExpected> Expected = {
            {TEXT("WBP_ModSettingsPage"), PageParentPath, true, false, {TEXT("CloseModSettings"), TEXT("Construct")}},
            {TEXT("WBP_ModMenuExtension"), UUserWidget::StaticClass()->GetPathName(), true, true, {TEXT("OpenModSettings"), TEXT("Tick"), TEXT("Destruct")}},
            {TEXT("WBP_ModMainScreen"), ParentPath, false, true, {TEXT("Construct"), TEXT("Tick"), TEXT("Destruct")}}
        };
        TArray<TSharedPtr<FJsonValue>> Checks;
        for (const FExpected& Want : Expected)
        {
            const FString Path = FString::Printf(TEXT("/%s/UI/%s.%s"), PluginId, Want.Name, Want.Name);
            const FString Package = FPackageName::ObjectPathToPackageName(Path);
            if (FindObject<UPackage>(nullptr, *Package))
                return Fail(TEXT("reload_requires_fresh_editor"), TEXT("The saved asset package was already resident before this check: ") + Package);
            UWidgetBlueprint* BP = LoadObject<UWidgetBlueprint>(nullptr, *Path);
            if (!BP || !BP->GeneratedClass || BP->GetPathName() != Path)
                return Fail(TEXT("saved_asset_reload_failed"), TEXT("The exact saved WidgetBlueprint and generated class did not load: ") + Path);
            const bool OwnRoot = BP->WidgetTree && BP->WidgetTree->RootWidget;
            if (!BP->ParentClass || BP->ParentClass->GetPathName() != Want.ParentPath || OwnRoot != Want.OwnRoot)
                return Fail(TEXT("saved_widget_composition_changed"), TEXT("The saved parent/root composition differs: ") + Path);
            auto Check = MakeShared<FJsonObject>();
            Check->SetStringField(TEXT("asset"), BP->GetPathName());
            Check->SetStringField(TEXT("generated_class"), BP->GeneratedClass->GetPathName());
            Check->SetStringField(TEXT("parent"), BP->ParentClass->GetPathName());
            Check->SetBoolField(TEXT("own_widget_tree"), OwnRoot);
            if (FString(Want.Name) == TEXT("WBP_ModMainScreen") && !VerifyMainReferences(BP, Check)) return false;
            if (Want.Ticking)
            {
                auto* Default = Cast<UUserWidget>(BP->GeneratedClass->GetDefaultObject());
                if (!Default || Default->GetDesiredTickFrequency() != EWidgetTickFrequency::Auto)
                    return Fail(TEXT("saved_widget_tick_disabled"), TEXT("The saved widget must retain automatic ticking: ") + Path);
                Check->SetStringField(TEXT("tick_frequency"), TEXT("Auto"));
            }
            int32 GraphCount = 0, NodeCount = 0, LinkedPins = 0;
            TArray<UEdGraph*> Graphs;
            for (UEdGraph* Graph : BP->UbergraphPages) Graphs.Add(Graph);
            for (UEdGraph* Graph : BP->FunctionGraphs) Graphs.Add(Graph);
            for (UEdGraph* Graph : Graphs)
            {
                if (!Graph) continue;
                ++GraphCount;
                for (UEdGraphNode* GraphNode : Graph->Nodes)
                {
                    if (!GraphNode) continue;
                    ++NodeCount;
                    for (UEdGraphPin* GraphPin : GraphNode->Pins)
                        if (GraphPin && GraphPin->LinkedTo.Num()) ++LinkedPins;
                }
            }
            Check->SetNumberField(TEXT("graph_count"), GraphCount);
            Check->SetNumberField(TEXT("nodes"), NodeCount);
            Check->SetNumberField(TEXT("pins_with_links"), LinkedPins);
            if (!GraphCount || !NodeCount || !LinkedPins)
                return Fail(TEXT("saved_widget_graphs_missing"), TEXT("The saved source graphs lack nodes/connections: ") + Path);
            TArray<TSharedPtr<FJsonValue>> Functions;
            for (FName Name : Want.Functions)
            {
                UFunction* Function = BP->GeneratedClass->FindFunctionByName(Name);
                if (!Function || Function->GetOuterUClass() != BP->GeneratedClass || !Function->Script.Num())
                    return Fail(TEXT("saved_widget_function_missing"), Path + TEXT(" requires an owned nonempty bytecode function: ") + Name.ToString());
                auto Body = MakeShared<FJsonObject>();
                Body->SetStringField(TEXT("name"), Name.ToString());
                Body->SetBoolField(TEXT("owned"), true);
                Body->SetNumberField(TEXT("script_bytes"), Function->Script.Num());
                Body->SetStringField(TEXT("script_hex"), BytesToHex(Function->Script.GetData(), Function->Script.Num()));
                Functions.Add(MakeShared<FJsonValueObject>(Body));
            }
            Check->SetArrayField(TEXT("functions"), Functions);
            Checks.Add(MakeShared<FJsonValueObject>(Check));
            Report->SetArrayField(TEXT("reload_checks"), Checks);
        }
        Report->SetBoolField(TEXT("saved_assets_reload_verified"), true);
        return true;
    }
};
}

UModSettingsMenuBuilderCommandlet::UModSettingsMenuBuilderCommandlet(const FObjectInitializer& ObjectInitializer)
    : Super(ObjectInitializer)
{
    IsClient = false;
    IsEditor = true;
    IsServer = false;
    LogToConsole = true;
    ShowErrorCount = true;
}

int32 UModSettingsMenuBuilderCommandlet::Main(const FString& Params)
{
    FBuilder Builder;
    FString ReportPath;
    if (!FParse::Value(*Params, TEXT("PluginRoot="), Builder.PluginRoot) || !FParse::Value(*Params, TEXT("Report="), ReportPath)
        || FPaths::IsRelative(Builder.PluginRoot) || FPaths::IsRelative(ReportPath))
    {
        UE_LOG(LogModSettingsMenuBuilder, Error, TEXT("-PluginRoot=<absolute SN2ModSettingsTest root> -Report=<absolute JSON> are required"));
        return 1;
    }
    FPaths::NormalizeDirectoryName(Builder.PluginRoot);
    FString ParentPath(TEXT("/Game/Blueprints/UI/WBP_MainScreen.WBP_MainScreen_C"));
    FString LobbyPath(TEXT("/Game/Blueprints/UI/Lobby/WBP_MainLobbyScreen.WBP_MainLobbyScreen_C"));
    FString SettingsPath(TEXT("/Game/Blueprints/UI/Settings2/WBP_Settings2Screen.WBP_Settings2Screen_C"));
    FString PageParentPath(TEXT("/Script/UWECommonUI.FocusActivatableWidget"));
    FString ButtonName(TEXT("SettingsButton")), BackgroundName(TEXT("DarkFade_1")), TitleName(TEXT("SettingTitle")), BackName(TEXT("CancelGraphicsButton"));
    FParse::Value(*Params, TEXT("ParentClass="), ParentPath);
    FParse::Value(*Params, TEXT("MainLobbyClass="), LobbyPath);
    FParse::Value(*Params, TEXT("SettingsClass="), SettingsPath);
    FParse::Value(*Params, TEXT("SettingsButton="), ButtonName);
    FParse::Value(*Params, TEXT("BackgroundWidget="), BackgroundName);
    FParse::Value(*Params, TEXT("TitleWidget="), TitleName);
    FParse::Value(*Params, TEXT("BackWidget="), BackName);
    Builder.Report->SetNumberField(TEXT("schema"), 1);
    Builder.Report->SetStringField(TEXT("kind"), TEXT("sn2_mod_settings_menu_build_report"));
    Builder.Report->SetStringField(TEXT("plugin_id"), PluginId);
    Builder.Report->SetStringField(TEXT("mod_version"), TEXT("0.1.0"));
    Builder.Report->SetBoolField(TEXT("saved"), false);
    Builder.Report->SetBoolField(TEXT("installed"), false);
    Builder.Report->SetBoolField(TEXT("game_executed"), false);
    Builder.Report->SetBoolField(TEXT("no_bridge_dll_verified"), false);
    const bool ReloadOnly = FParse::Param(*Params, TEXT("InspectSaved"));
    Builder.Report->SetBoolField(TEXT("reload_only"), ReloadOnly);
    bool Success = true;
    if (ReloadOnly)
        Success = Builder.ReloadSaved(ParentPath, PageParentPath);
    else
    {
    Success = Builder.Discover(ParentPath, LobbyPath, ButtonName);
    ModSettingsMenuStyle::FSelection Style;
    UClass* SettingsClass = Builder.LoadClass(SettingsPath);
    if (SettingsClass)
        Success = ModSettingsMenuStyle::Discover(SettingsClass, BackgroundName, TitleName, BackName, Style, Builder.Report, Builder.Errors) && Success;
    else Success = false;
    Builder.PageParent = Builder.LoadClass(PageParentPath);
    Success = Success && Builder.PageParent && Builder.PageParent->IsChildOf(UUserWidget::StaticClass());
    const bool InspectOnly = FParse::Param(*Params, TEXT("InspectOnly"));
    if (Success && !InspectOnly)
    {
        FPackageName::RegisterMountPoint(TEXT("/") + FString(PluginId) + TEXT("/"), FPaths::Combine(Builder.PluginRoot, TEXT("Content/")));
        UWidgetBlueprint* Page = Builder.Blueprint(TEXT("WBP_ModSettingsPage"), Builder.PageParent);
        if (Page)
            Success = ModSettingsMenuStyle::Build(Page, Style, BackMember, Builder.Report, Builder.Errors) && Builder.Page(Page, Style);
        else Success = false;
        UWidgetBlueprint* Extension = Success ? Builder.Blueprint(TEXT("WBP_ModMenuExtension"), UUserWidget::StaticClass()) : nullptr;
        Success = Extension && Builder.Extension(Extension, Page);
        UWidgetBlueprint* MainScreen = Success ? Builder.Blueprint(TEXT("WBP_ModMainScreen"), Builder.Parent) : nullptr;
        Success = MainScreen && Builder.Main(MainScreen);
        if (Success) Success = Builder.Save();
    }
    Builder.Report->SetBoolField(TEXT("inspection_only"), InspectOnly);
    }
    Builder.Report->SetStringField(TEXT("state"), Success ? (ReloadOnly ? TEXT("reload_verified") : (FParse::Param(*Params, TEXT("InspectOnly")) ? TEXT("inspected") : TEXT("succeeded"))) : TEXT("failed"));
    if (ReloadOnly) Builder.Report->SetBoolField(TEXT("inspection_only"), false);
    Builder.Report->SetArrayField(TEXT("errors"), Builder.Errors);
    Builder.Report->SetArrayField(TEXT("assets"), Builder.Assets);
    FString Text;
    FJsonSerializer::Serialize(Builder.Report, TJsonWriterFactory<>::Create(&Text));
    IFileManager::Get().MakeDirectory(*FPaths::GetPath(ReportPath), true);
    if (!FFileHelper::SaveStringToFile(Text, *ReportPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
    { UE_LOG(LogModSettingsMenuBuilder, Error, TEXT("The structured report could not be saved: %s"), *ReportPath); return 1; }
    return Success ? 0 : 1;
}
