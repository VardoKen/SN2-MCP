// Только собственный CDO. Входные строки не изменяются и не сохраняются.
UCLASS()
class UG1RMCPStringInputProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 211;
    }

    UFUNCTION(BlueprintCallable)
    int ReadInputLength(const FString&in Input)
    {
        CallsCount++;
        return Input.Len();
    }

    UFUNCTION(BlueprintCallable)
    FString ReadEcho(const FString&in Input)
    {
        CallsCount++;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    FString ConcatInputs(const FString&in Left, const FString&in Right)
    {
        CallsCount++;
        return Left + Right;
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
