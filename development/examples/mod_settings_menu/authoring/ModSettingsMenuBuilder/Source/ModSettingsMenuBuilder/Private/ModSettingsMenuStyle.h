#pragma once

#include "CoreMinimal.h"

class FJsonObject;
class FJsonValue;
class UClass;
class UTextBlock;
class UUserWidget;
class UWidget;
class UWidgetBlueprint;

namespace ModSettingsMenuStyle
{
struct FSelection
{
    UWidget* Background = nullptr;
    UTextBlock* Title = nullptr;
    UUserWidget* Back = nullptr;
    UClass* BackClass = nullptr;
};

bool Discover(UClass* SettingsClass, const FString& BackgroundName, const FString& TitleName,
              const FString& BackName, FSelection& Out, TSharedRef<FJsonObject> Report,
              TArray<TSharedPtr<FJsonValue>>& Errors);
bool Build(UWidgetBlueprint* Page, const FSelection& Style, FName BackWidgetName,
           TSharedRef<FJsonObject> Report, TArray<TSharedPtr<FJsonValue>>& Errors);
}
