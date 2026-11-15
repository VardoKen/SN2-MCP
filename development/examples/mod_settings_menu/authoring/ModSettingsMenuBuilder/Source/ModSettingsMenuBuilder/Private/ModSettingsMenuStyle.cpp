#include "ModSettingsMenuStyle.h"

#include "Blueprint/UserWidget.h"
#include "Blueprint/WidgetBlueprintGeneratedClass.h"
#include "Blueprint/WidgetTree.h"
#include "Components/Border.h"
#include "Components/CanvasPanel.h"
#include "Components/CanvasPanelSlot.h"
#include "Components/PanelSlot.h"
#include "Components/PanelWidget.h"
#include "Components/TextBlock.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/UnrealType.h"
#include "WidgetBlueprint.h"

namespace ModSettingsMenuStyle
{
namespace
{
bool Fail(const FString& Code, const FString& Message, TSharedRef<FJsonObject> Report,
          TArray<TSharedPtr<FJsonValue>>& Errors)
{
    TSharedRef<FJsonObject> Error = MakeShared<FJsonObject>();
    Error->SetStringField(TEXT("code"), Code);
    Error->SetStringField(TEXT("message"), Message);
    Errors.Add(MakeShared<FJsonValueObject>(Error));
    Report->SetBoolField(TEXT("style_ready"), false);
    return false;
}

TSharedRef<FJsonObject> Describe(UWidget* Widget)
{
    TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
    Entry->SetStringField(TEXT("name"), Widget->GetName());
    Entry->SetStringField(TEXT("path"), Widget->GetPathName());
    Entry->SetStringField(TEXT("class"), Widget->GetClass()->GetPathName());
    Entry->SetStringField(TEXT("parent"), Widget->GetParent() ? Widget->GetParent()->GetPathName() : TEXT(""));
    Entry->SetStringField(TEXT("slot"), Widget->Slot ? Widget->Slot->GetPathName() : TEXT(""));
    Entry->SetStringField(TEXT("slot_class"), Widget->Slot ? Widget->Slot->GetClass()->GetPathName() : TEXT(""));
    if (UCanvasPanelSlot* Slot = Cast<UCanvasPanelSlot>(Widget->Slot))
    {
        TSharedRef<FJsonObject> Layout = MakeShared<FJsonObject>();
        const FAnchors Anchors = Slot->GetAnchors();
        const FMargin Offsets = Slot->GetOffsets();
        const FVector2D Alignment = Slot->GetAlignment();
        Layout->SetNumberField(TEXT("anchor_min_x"), Anchors.Minimum.X);
        Layout->SetNumberField(TEXT("anchor_min_y"), Anchors.Minimum.Y);
        Layout->SetNumberField(TEXT("anchor_max_x"), Anchors.Maximum.X);
        Layout->SetNumberField(TEXT("anchor_max_y"), Anchors.Maximum.Y);
        Layout->SetNumberField(TEXT("left"), Offsets.Left);
        Layout->SetNumberField(TEXT("top"), Offsets.Top);
        Layout->SetNumberField(TEXT("right"), Offsets.Right);
        Layout->SetNumberField(TEXT("bottom"), Offsets.Bottom);
        Layout->SetNumberField(TEXT("alignment_x"), Alignment.X);
        Layout->SetNumberField(TEXT("alignment_y"), Alignment.Y);
        Layout->SetBoolField(TEXT("auto_size"), Slot->GetAutoSize());
        Layout->SetNumberField(TEXT("z_order"), Slot->GetZOrder());
        Entry->SetObjectField(TEXT("canvas_layout"), Layout);
    }
    return Entry;
}

UWidget* Select(const TArray<UWidget*>& Widgets, const FString& ExactName,
                const TArray<FString>& Hints, TFunctionRef<bool(UWidget*)> Eligible,
                const FString& Role, TSharedRef<FJsonObject> Report,
                TArray<TSharedPtr<FJsonValue>>& Errors)
{
    TArray<UWidget*> Matches;
    for (UWidget* Widget : Widgets)
    {
        if (!Widget || !Eligible(Widget))
        {
            continue;
        }
        bool bMatches = !ExactName.IsEmpty() && Widget->GetName().Equals(ExactName, ESearchCase::CaseSensitive);
        if (ExactName.IsEmpty())
        {
            for (const FString& Hint : Hints)
            {
                bMatches |= Widget->GetName().Contains(Hint, ESearchCase::IgnoreCase);
            }
        }
        if (bMatches)
        {
            Matches.Add(Widget);
        }
    }
    if (Matches.Num() != 1)
    {
        Fail(TEXT("settings_style_selection_ambiguous"),
             FString::Printf(TEXT("Expected one eligible %s widget; found %d. Inspect settings_widget_inventory and supply its exact name."),
                             *Role, Matches.Num()), Report, Errors);
        return nullptr;
    }
    return Matches[0];
}

void ResetTemplate(UWidget* Widget)
{
    Widget->ClearFlags(RF_ArchetypeObject | RF_DefaultSubObject | RF_Public | RF_Standalone);
    Widget->SetFlags(RF_Transactional);
    Widget->Slot = nullptr;
    // The selected chrome supplies appearance, not an instance's bindings to
    // the original Settings screen. Runtime class bindings remain unchanged.
    for (TFieldIterator<FProperty> It(Widget->GetClass()); It; ++It)
    {
        FProperty* Property = *It;
        for (int32 Index = 0; Index < Property->ArrayDim; ++Index)
        {
            if (FMulticastDelegateProperty* Multicast = CastField<FMulticastDelegateProperty>(Property))
                Multicast->ClearDelegate(Widget, Property->ContainerPtrToValuePtr<void>(Widget, Index));
            else if (CastField<FDelegateProperty>(Property))
                Property->ClearValue_InContainer(Widget, Index);
        }
    }
    if (FArrayProperty* Bindings = FindFProperty<FArrayProperty>(Widget->GetClass(), TEXT("NativeBindings")))
        Bindings->ClearValue_InContainer(Widget);
    if (FObjectPropertyBase* Navigation = FindFProperty<FObjectPropertyBase>(Widget->GetClass(), TEXT("Navigation")))
        Navigation->SetObjectPropertyValue_InContainer(Widget, nullptr);
}

void CopyCanvasLayout(UCanvasPanelSlot* Dest, const UWidget* Source)
{
    if (const UCanvasPanelSlot* Original = Cast<UCanvasPanelSlot>(Source->Slot))
    {
        Dest->SetAnchors(Original->GetAnchors());
        Dest->SetOffsets(Original->GetOffsets());
        Dest->SetAlignment(Original->GetAlignment());
        Dest->SetAutoSize(Original->GetAutoSize());
        Dest->SetZOrder(Original->GetZOrder());
    }
}
}

bool Discover(UClass* SettingsClass, const FString& BackgroundName, const FString& TitleName,
              const FString& BackName, FSelection& Out, TSharedRef<FJsonObject> Report,
              TArray<TSharedPtr<FJsonValue>>& Errors)
{
    Out = FSelection{};
    UWidgetTree* Tree = nullptr;
    UClass* TreeClass = nullptr;
    for (UClass* Class = SettingsClass; Class; Class = Class->GetSuperClass())
    {
        if (UWidgetBlueprintGeneratedClass* WidgetClass = Cast<UWidgetBlueprintGeneratedClass>(Class))
        {
            WidgetClass->ConditionalPostLoad();
            UWidgetTree* Candidate = WidgetClass->GetWidgetTreeArchetype();
            if (Candidate && Candidate->RootWidget)
            {
                Tree = Candidate;
                TreeClass = Class;
                break;
            }
        }
    }
    if (!Tree)
    {
        return Fail(TEXT("settings_style_tree_missing"), TEXT("The selected settings class has no widget tree archetype."), Report, Errors);
    }
    Report->SetStringField(TEXT("settings_tree_class"), TreeClass->GetPathName());
    Report->SetStringField(TEXT("settings_tree"), Tree->GetPathName());
    TArray<UWidget*> Widgets;
    Tree->GetAllWidgets(Widgets);
    TArray<TSharedPtr<FJsonValue>> Inventory;
    for (UWidget* Widget : Widgets)
    {
        if (Widget)
        {
            Inventory.Add(MakeShared<FJsonValueObject>(Describe(Widget)));
        }
    }
    Report->SetArrayField(TEXT("settings_widget_inventory"), Inventory);
    Out.Background = Select(Widgets, BackgroundName, {TEXT("background"), TEXT("bg")},
                            [](UWidget*) { return true; }, TEXT("background"), Report, Errors);
    Out.Title = Cast<UTextBlock>(Select(Widgets, TitleName, {TEXT("title"), TEXT("header")},
                                     [](UWidget* Widget) { return Widget->IsA<UTextBlock>(); },
                                     TEXT("title"), Report, Errors));
    Out.Back = Cast<UUserWidget>(Select(Widgets, BackName, {TEXT("back"), TEXT("cancel")},
                                       [](UWidget* Widget) { return Widget->IsA<UUserWidget>(); },
                                       TEXT("back"), Report, Errors));
    if (!Out.Background || !Out.Title || !Out.Back)
    {
        return false;
    }
    if (Out.Background == Out.Title || Out.Background == Out.Back
        || static_cast<UWidget*>(Out.Title) == static_cast<UWidget*>(Out.Back))
    {
        return Fail(TEXT("settings_style_roles_overlap"), TEXT("The background, title and back control must be distinct widgets."), Report, Errors);
    }
    Out.BackClass = Out.Back->GetClass();
    FMulticastDelegateProperty* Delegate = FindFProperty<FMulticastDelegateProperty>(Out.BackClass, TEXT("OnButtonBaseClicked"));
    if (!Delegate || !Delegate->SignatureFunction)
    {
        return Fail(TEXT("settings_back_delegate_missing"), TEXT("The selected back widget does not expose OnButtonBaseClicked with a reflected signature."), Report, Errors);
    }
    TArray<TSharedPtr<FJsonValue>> Parameters;
    bool bSignatureValid = true;
    int32 ParameterCount = 0;
    for (TFieldIterator<FProperty> It(Delegate->SignatureFunction); It; ++It)
    {
        FProperty* Property = *It;
        if (!Property->HasAnyPropertyFlags(CPF_Parm))
        {
            continue;
        }
        ++ParameterCount;
        TSharedRef<FJsonObject> Param = MakeShared<FJsonObject>();
        Param->SetStringField(TEXT("name"), Property->GetName());
        Param->SetStringField(TEXT("type"), Property->GetClass()->GetName());
        Param->SetStringField(TEXT("flags"), FString::Printf(TEXT("0x%016llx"), static_cast<unsigned long long>(Property->GetPropertyFlags())));
        const FObjectPropertyBase* Object = CastField<FObjectPropertyBase>(Property);
        Param->SetStringField(TEXT("class"), Object && Object->PropertyClass ? Object->PropertyClass->GetPathName() : TEXT(""));
        bSignatureValid &= Property->GetFName() == FName(TEXT("Button")) && Object && Object->PropertyClass
            && Object->PropertyClass->GetPathName() == TEXT("/Script/CommonUI.CommonButtonBase")
            && !Property->HasAnyPropertyFlags(CPF_ReturnParm | CPF_OutParm | CPF_ReferenceParm);
        Parameters.Add(MakeShared<FJsonValueObject>(Param));
    }
    Report->SetStringField(TEXT("settings_back_delegate"), Delegate->GetPathName());
    Report->SetStringField(TEXT("settings_back_signature"), Delegate->SignatureFunction->GetPathName());
    Report->SetArrayField(TEXT("settings_back_parameters"), Parameters);
    if (!bSignatureValid || ParameterCount != 1)
    {
        return Fail(TEXT("settings_back_signature_changed"), TEXT("OnButtonBaseClicked does not have the expected single Button CommonButtonBase input."), Report, Errors);
    }
    TSharedRef<FJsonObject> Selection = MakeShared<FJsonObject>();
    Selection->SetObjectField(TEXT("background"), Describe(Out.Background));
    Selection->SetObjectField(TEXT("title"), Describe(Out.Title));
    Selection->SetObjectField(TEXT("back"), Describe(Out.Back));
    Report->SetObjectField(TEXT("style_selection"), Selection);
    Report->SetBoolField(TEXT("style_ready"), true);
    return true;
}

bool Build(UWidgetBlueprint* Page, const FSelection& Style, FName BackWidgetName,
           TSharedRef<FJsonObject> Report, TArray<TSharedPtr<FJsonValue>>& Errors)
{
    if (!Page || !Page->WidgetTree || Page->WidgetTree->RootWidget || !Style.Background || !Style.Title || !Style.Back
        || BackWidgetName.IsNone())
    {
        return Fail(TEXT("settings_page_build_invalid"), TEXT("An empty page widget tree, complete selected style and named back widget are required."), Report, Errors);
    }
    if (UPanelWidget* BackgroundPanel = Cast<UPanelWidget>(Style.Background))
    {
        if (!BackgroundPanel->IsA<UBorder>() && BackgroundPanel->GetChildrenCount() > 0)
        {
            return Fail(TEXT("settings_background_contains_controls"), TEXT("The selected non-Border background contains children; copying it would copy settings controls."), Report, Errors);
        }
    }
    if (Style.Background->IsA<UUserWidget>())
    {
        return Fail(TEXT("settings_background_nested_widget"), TEXT("A UserWidget background may contain a foreign widget tree; select a primitive background or Border."), Report, Errors);
    }
    UWidgetTree* Tree = Page->WidgetTree;
    UCanvasPanel* Root = Tree->ConstructWidget<UCanvasPanel>(UCanvasPanel::StaticClass(), TEXT("ModSettingsRoot"));
    const EObjectFlags CopyFlags = RF_AllFlags & ~(RF_ArchetypeObject | RF_DefaultSubObject | RF_Public | RF_Standalone);
    UWidget* Background = Cast<UWidget>(StaticDuplicateObject(Style.Background, Tree, TEXT("ModSettingsBackground"), CopyFlags));
    UTextBlock* Title = Cast<UTextBlock>(StaticDuplicateObject(Style.Title, Tree, TEXT("ModSettingsTitle"), CopyFlags));
    UUserWidget* Back = Cast<UUserWidget>(StaticDuplicateObject(Style.Back, Tree, BackWidgetName, CopyFlags));
    if (!Root || !Background || !Title || !Back)
    {
        return Fail(TEXT("settings_page_duplicate_failed"), TEXT("The engine could not duplicate the selected page chrome."), Report, Errors);
    }
    ResetTemplate(Background);
    ResetTemplate(Title);
    ResetTemplate(Back);
    if (UBorder* Border = Cast<UBorder>(Background))
    {
        Border->ClearChildren();
    }
    Back->bIsVariable = true;
    Title->SetText(FText::FromString(TEXT("Настройки модов")));
    Tree->RootWidget = Root;
    UCanvasPanelSlot* BackgroundSlot = Root->AddChildToCanvas(Background);
    UCanvasPanelSlot* TitleSlot = Root->AddChildToCanvas(Title);
    UCanvasPanelSlot* BackSlot = Root->AddChildToCanvas(Back);
    if (!BackgroundSlot || !TitleSlot || !BackSlot)
    {
        return Fail(TEXT("settings_page_slot_failed"), TEXT("The engine could not attach copied chrome to the page CanvasPanel."), Report, Errors);
    }
    BackgroundSlot->SetAnchors(FAnchors(0.f, 0.f, 1.f, 1.f));
    BackgroundSlot->SetOffsets(FMargin(0.f));
    BackgroundSlot->SetAlignment(FVector2D::ZeroVector);
    BackgroundSlot->SetAutoSize(false);
    BackgroundSlot->SetZOrder(0);
    CopyCanvasLayout(TitleSlot, Style.Title);
    if (!Cast<UCanvasPanelSlot>(Style.Title->Slot))
    {
        TitleSlot->SetAnchors(FAnchors(.5f, 0.f));
        TitleSlot->SetOffsets(FMargin(0.f, 48.f, 800.f, 80.f));
        TitleSlot->SetAlignment(FVector2D(.5f, 0.f));
        TitleSlot->SetAutoSize(true);
    }
    TitleSlot->SetZOrder(1);
    CopyCanvasLayout(BackSlot, Style.Back);
    if (!Cast<UCanvasPanelSlot>(Style.Back->Slot))
    {
        BackSlot->SetAnchors(FAnchors(0.f, 1.f));
        BackSlot->SetOffsets(FMargin(48.f, -48.f, 320.f, 80.f));
        BackSlot->SetAlignment(FVector2D(0.f, 1.f));
        BackSlot->SetAutoSize(true);
    }
    BackSlot->SetZOrder(2);
    TSharedRef<FJsonObject> Chrome = MakeShared<FJsonObject>();
    Chrome->SetObjectField(TEXT("root"), Describe(Root));
    Chrome->SetObjectField(TEXT("background"), Describe(Background));
    Chrome->SetObjectField(TEXT("title"), Describe(Title));
    Chrome->SetObjectField(TEXT("back"), Describe(Back));
    Chrome->SetStringField(TEXT("background_layout"), TEXT("explicit_full_canvas"));
    Chrome->SetStringField(TEXT("title_layout"), Cast<UCanvasPanelSlot>(Style.Title->Slot) ? TEXT("copied_stock_canvas") : TEXT("explicit_top_center"));
    Chrome->SetStringField(TEXT("back_layout"), Cast<UCanvasPanelSlot>(Style.Back->Slot) ? TEXT("copied_stock_canvas") : TEXT("explicit_bottom_left"));
    Chrome->SetBoolField(TEXT("settings_controls_copied"), false);
    Report->SetObjectField(TEXT("page_chrome"), Chrome);
    return true;
}
}
