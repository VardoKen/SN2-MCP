// Только собственный объект; EchoName изменяет лишь проверочный счётчик.
UCLASS()
class UG1RMCPNameProbe : UObject
{
    UPROPERTY()
    FName StoredEntry = n"Entry";

    UPROPERTY()
    FName StoredNone = n"";

    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 71;
    }

    UFUNCTION(BlueprintCallable)
    FName ReadEntry()
    {
        return n"Entry";
    }

    UFUNCTION(BlueprintCallable)
    FName ReadNone()
    {
        return n"";
    }

    UFUNCTION(BlueprintCallable)
    FString ReadText()
    {
        return "G1R MCP dev7";
    }

    UFUNCTION(BlueprintCallable)
    FString ReadEmptyText()
    {
        return "";
    }

    UFUNCTION(BlueprintCallable)
    FName EchoName(FName Input)
    {
        CallsCount += 1;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    int ReadCallsCount()
    {
        return CallsCount;
    }

    UFUNCTION(BlueprintCallable)
    void ResetProbe()
    {
        CallsCount = 0;
    }
}
