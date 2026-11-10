// Только собственный CDO; значения не совпадают с индексами перечислителей.
enum EG1RMCPEnumProbeMode
{
    First = 0,
    Second = 1,
    Third = 7
};

USTRUCT()
struct FG1RMCPNestedInner
{
    UPROPERTY()
    int Integer = 0;

    UPROPERTY()
    float64 Number = 0.0;

    UPROPERTY()
    bool Flag = false;
}

USTRUCT()
struct FG1RMCPNestedDTO
{
    UPROPERTY()
    FG1RMCPNestedInner Inner;

    UPROPERTY()
    FName Label = n"";

    UPROPERTY()
    EG1RMCPEnumProbeMode Mode = EG1RMCPEnumProbeMode::First;
}

UCLASS()
class UG1RMCPEnumProbe : UObject
{
    UPROPERTY()
    EG1RMCPEnumProbeMode StoredMode = EG1RMCPEnumProbeMode::First;

    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 81;
    }

    // Own POD fields; reflected size and alignment are checked by the live runner.
    UFUNCTION(BlueprintCallable)
    FG1RMCPNestedDTO EchoNested(FG1RMCPNestedDTO Value)
    {
        return Value;
    }

    UFUNCTION(BlueprintCallable)
    TArray<FG1RMCPNestedDTO> EchoNestedArray(TArray<FG1RMCPNestedDTO> Values)
    {
        TArray<FG1RMCPNestedDTO> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    void WriteNested(FG1RMCPNestedDTO&out Value)
    {
        Value.Inner.Integer = 7;
        Value.Inner.Number = 2.75;
        Value.Inner.Flag = true;
        Value.Label = n"Entry";
        Value.Mode = EG1RMCPEnumProbeMode::Third;
    }

    UFUNCTION(BlueprintCallable)
    void WriteNestedArray(bool WithValues, TArray<FG1RMCPNestedDTO>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            FG1RMCPNestedDTO First;
            First.Inner.Integer = 7;
            First.Inner.Number = 2.75;
            First.Inner.Flag = true;
            First.Label = n"Entry";
            First.Mode = EG1RMCPEnumProbeMode::Third;
            Values.Add(First);

            FG1RMCPNestedDTO Second;
            Second.Inner.Integer = -7;
            Second.Inner.Number = -2.75;
            Second.Inner.Flag = false;
            Second.Label = n"";
            Second.Mode = EG1RMCPEnumProbeMode::First;
            Values.Add(Second);
        }
    }

    // Fresh local enum array; numeric values are First=0, Third=7; duplicates retained.
    UFUNCTION(BlueprintCallable)
    TArray<EG1RMCPEnumProbeMode> ReturnEnumArray(bool WithValues)
    {
        TArray<EG1RMCPEnumProbeMode> Result;
        if (WithValues)
        {
            Result.Add(EG1RMCPEnumProbeMode::First);
            Result.Add(EG1RMCPEnumProbeMode::Third);
            Result.Add(EG1RMCPEnumProbeMode::First);
        }
        return Result;
    }

    // Pure output; duplicates and sparse numeric enum values are retained.
    UFUNCTION(BlueprintCallable)
    void WriteEnumArray(bool WithValues, TArray<EG1RMCPEnumProbeMode>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            Values.Add(EG1RMCPEnumProbeMode::First);
            Values.Add(EG1RMCPEnumProbeMode::Third);
            Values.Add(EG1RMCPEnumProbeMode::First);
        }
    }

    UFUNCTION(BlueprintCallable)
    EG1RMCPEnumProbeMode ReadMode()
    {
        return StoredMode;
    }

    UFUNCTION(BlueprintCallable)
    EG1RMCPEnumProbeMode EchoMode(EG1RMCPEnumProbeMode Input)
    {
        CallsCount += 1;
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    void StoreMode(EG1RMCPEnumProbeMode Input)
    {
        CallsCount += 1;
        StoredMode = Input;
    }

    UFUNCTION(BlueprintCallable)
    void ResetProbe()
    {
        StoredMode = EG1RMCPEnumProbeMode::First;
        CallsCount = 0;
    }
}
