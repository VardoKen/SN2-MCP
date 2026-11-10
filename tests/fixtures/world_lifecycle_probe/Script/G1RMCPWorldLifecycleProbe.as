// PostInitialize изменяет только свой счётчик; переход карты вызывается явно через MCP.
UCLASS()
class UG1RMCPWorldPostInitializeProbe : UScriptWorldSubsystem
{
    UPROPERTY()
    int PostInitializeCount = 0;

    UFUNCTION(BlueprintOverride)
    void PostInitialize()
    {
        PostInitializeCount += 1;
    }

    UFUNCTION(BlueprintCallable)
    int ReadPostInitializeCount()
    {
        return PostInitializeCount;
    }

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 111;
    }

    UFUNCTION(BlueprintCallable)
    FString GetCurrentMapName()
    {
        return Gameplay::GetCurrentLevelName(true);
    }

    UFUNCTION(BlueprintCallable)
    ELoadingScreenType GetCurrentLoadingScreenType()
    {
        return ULoadingScreenHelperSubsystem::Get().GetCurrentLoadingScreenType();
    }

    UFUNCTION(BlueprintCallable)
    void ReloadCurrentMap()
    {
        ULoadingScreenHelperSubsystem::Get().SetCurrentLoadingScreenType(ELoadingScreenType::BlackScreen);
        Gameplay::OpenLevel(FName(Gameplay::GetCurrentLevelName(true)), true, "");
    }
}
