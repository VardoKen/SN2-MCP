// Только собственный CDO. Const-ref вход не изменяется и возвращается по значению.
UCLASS()
class UG1RMCPConstVectorProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UPROPERTY()
    FVector StoredVector = FVector(1.25f, -2.5f, 3.75f);

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 181;
    }

    UFUNCTION(BlueprintCallable)
    FVector EchoConstVector(const FVector&in Input)
    {
        CallsCount += 1;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    FVector GetVector()
    {
        return StoredVector;
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
