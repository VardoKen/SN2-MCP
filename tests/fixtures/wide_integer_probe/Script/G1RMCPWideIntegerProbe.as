// Только собственный CDO; Echo-методы изменяют лишь проверочный счётчик.
UCLASS()
class UG1RMCPWideIntegerProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 151;
    }

    UFUNCTION(BlueprintCallable)
    int64 EchoInt64(int64 Input)
    {
        CallsCount += 1;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    uint64 EchoUInt64(uint64 Input)
    {
        CallsCount += 1;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    uint32 EchoUInt32(uint32 Input)
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
