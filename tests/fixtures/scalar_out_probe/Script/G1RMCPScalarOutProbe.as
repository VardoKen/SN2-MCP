// Только собственный CDO. Семь out-параметров — явная runtime-проба типов.
UCLASS()
class UG1RMCPScalarOutProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 171;
    }

    UFUNCTION(BlueprintCallable)
    bool GetOutputs(bool&out OutBool,
                    int&out OutInt,
                    int64&out OutInt64,
                    uint32&out OutUInt32,
                    uint64&out OutUInt64,
                    float32&out OutFloat32,
                    float&out OutFloat)
    {
        CallsCount += 1;
        OutBool = true;
        OutInt = -7;
        OutInt64 = int64(-9007199254740993);
        OutUInt32 = uint32(4294967295);
        OutUInt64 = uint64(18446744073709551615);
        OutFloat32 = 1.25f;
        OutFloat = -2.5f;
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
