// Только собственный CDO. Цвет передаётся по значению; меняется только счётчик.
UCLASS()
class UG1RMCPLinearColorProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 231;
    }

    UFUNCTION(BlueprintCallable)
    float ReadColorR(FLinearColor Input)
    {
        CallsCount++;
        return Input.R;
    }

    UFUNCTION(BlueprintCallable)
    FLinearColor EchoColor(FLinearColor Input)
    {
        CallsCount++;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    FLinearColor MakeStoredColor()
    {
        CallsCount++;
        return FLinearColor(0.25f, -0.5f, 2.0f, 0.125f);
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
