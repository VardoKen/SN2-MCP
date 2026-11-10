// Собственная запись меню инструментов; окно закрыто, игровой мир не изменяется.
UCLASS()
class UG1RMCPIngameLifecycleProbe : UIngameTool
{
    default Category = "G1RMCP";
    default MenuPath = "G1RMCP.Lifecycle";
    default Title = "G1R MCP Lifecycle Probe";
    default bIsWindowOpen = false;

    UPROPERTY()
    int InitializeCount = 0;

    UFUNCTION(BlueprintOverride)
    void Initialize()
    {
        InitializeCount += 1;
    }

    UFUNCTION(BlueprintOverride)
    void DrawImGuiWindow()
    {
    }

    UFUNCTION(BlueprintCallable)
    int ReadInitializeCount()
    {
        return InitializeCount;
    }

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 91;
    }
}
