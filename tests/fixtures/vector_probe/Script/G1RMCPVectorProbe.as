// Только собственный CDO. Echo/Write изменяют лишь проверочный счётчик.
UCLASS()
class UG1RMCPVectorProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UPROPERTY()
    FVector StoredVector = FVector(1.25f, -2.5f, 3.75f);

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 161;
    }

    UFUNCTION(BlueprintCallable)
    FVector EchoVector(FVector Input)
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
    bool WriteVector(FVector&out Output)
    {
        CallsCount += 1;
        Output = StoredVector;
        return true;
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
