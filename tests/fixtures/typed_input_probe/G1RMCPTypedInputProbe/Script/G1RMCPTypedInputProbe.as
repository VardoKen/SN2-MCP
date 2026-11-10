// USTRUCT/UPROPERTY: Modkit Script/GAS/Abilities/Combat/AttackLoop/Base/GA_AttackLoop.as.
// float64: Modkit Script/AI/States/FightAI/CombatState/AIState_Combat.as.
// UFUNCTION and by-value TArray<int>: Modkit CrimeProcessingSubsystem.as, ForgiveCrimesByID.
// Confirm actual StructProperty/ArrayProperty and member metadata through reflection.
// Live verified: dev.103, Steam build 26394867; int/name arrays and scalar struct accepted.
// TArray<FName>: Modkit Script/AI/TauntData.as, AddMontages.
// FName comparisons: Modkit Script/Regions/Traits/QuestRelatedTraits.as; n"" is None.
USTRUCT()
struct FG1RMCPScalarInput
{
    UPROPERTY()
    int Integer = 0;

    UPROPERTY()
    float64 Number = 0.0;

    UPROPERTY()
    bool Flag = false;
}

UCLASS()
class UG1RMCPTypedInputProbe : UObject
{
    // Existing name_probe confirms reflected FName defaults with this name literal.
    UPROPERTY()
    FName SourceName = n"Entry";

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 71;
    }

    // [] -> 0; [1, 2, 3] -> 6; [2147483647, -2147483648] -> -1.
    // Acceptance inputs must not overflow int during intermediate additions.
    UFUNCTION(BlueprintCallable)
    int SumIntArray(TArray<int> Values)
    {
        int Sum = 0;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Sum += Values[Index];
        }
        return Sum;
    }

    // Fresh local int array; Add syntax: Modkit Script/UI/UpscalingQualitySettings.as.
    // false -> []; true -> [2147483647, -2147483648].
    UFUNCTION(BlueprintCallable)
    TArray<int> ReturnIntArray(bool WithValues)
    {
        TArray<int> Result;
        if (WithValues)
        {
            Result.Add(2147483647);
            Result.Add(-2147483647 - 1);
        }
        return Result;
    }

    // Fresh local FName array; false -> []; true -> [Entry, None, Entry].
    UFUNCTION(BlueprintCallable)
    TArray<FName> ReturnNameArray(bool WithValues)
    {
        TArray<FName> Result;
        if (WithValues)
        {
            Result.Add(n"Entry");
            Result.Add(n"");
            Result.Add(n"Entry");
        }
        return Result;
    }

    // Fresh own scalar DTOs; int boundaries, signed fractions and both bool values.
    UFUNCTION(BlueprintCallable)
    TArray<FG1RMCPScalarInput> ReturnScalarArray(bool WithValues)
    {
        TArray<FG1RMCPScalarInput> Result;
        if (WithValues)
        {
            FG1RMCPScalarInput First;
            First.Integer = 2147483647;
            First.Number = 2.75;
            First.Flag = true;
            Result.Add(First);

            FG1RMCPScalarInput Second;
            Second.Integer = -2147483647 - 1;
            Second.Number = -2.75;
            Second.Flag = false;
            Result.Add(Second);
        }
        return Result;
    }

    // Pure output arrays start empty; only their owned parameter buffers change.
    UFUNCTION(BlueprintCallable)
    void WriteIntArray(bool WithValues, TArray<int>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            Values.Add(2147483647);
            Values.Add(-2147483647 - 1);
        }
    }

    UFUNCTION(BlueprintCallable)
    void WriteNameArray(bool WithValues, TArray<FName>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            Values.Add(n"Entry");
            Values.Add(n"");
            Values.Add(n"Entry");
        }
    }

    UFUNCTION(BlueprintCallable)
    void WriteScalarArray(bool WithValues, TArray<FG1RMCPScalarInput>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            FG1RMCPScalarInput First;
            First.Integer = 2147483647;
            First.Number = 2.75;
            First.Flag = true;
            Values.Add(First);

            FG1RMCPScalarInput Second;
            Second.Integer = -2147483647 - 1;
            Second.Number = -2.75;
            Second.Flag = false;
            Values.Add(Second);
        }
    }

    UFUNCTION(BlueprintCallable)
    TArray<FString> EchoStringArray(TArray<FString> Values)
    {
        TArray<FString> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    void WriteStringArray(bool WithValues, TArray<FString>&out Values)
    {
        Values.Empty();
        if (WithValues)
        {
            Values.Add("");
            Values.Add("Entry");
            Values.Add("Line\nnext");
            Values.Add("Name_42");
            Values.Add("Ω");
            Values.Add("Entry");
        }
    }

    UFUNCTION(BlueprintCallable)
    FString WriteAndReturnStringArray(TArray<FString>&out Values)
    {
        Values.Empty();
        Values.Add("Entry");
        Values.Add("");
        return "Entry";
    }

    UFUNCTION(BlueprintCallable)
    void WriteString(bool WithValue, FString&out Value)
    {
        Value = WithValue ? "Entry\nΩ" : "";
    }

    UFUNCTION(BlueprintCallable)
    FString WriteAndReturnStrings(FString Input, FString&out First, FString&out Second)
    {
        First = Input;
        Second = "";
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    FString WriteStringsAndArray(FString&out First, FString&out Second, TArray<FString>&out Values)
    {
        First = "Entry";
        Second = "";
        Values.Empty();
        Values.Add("Entry");
        Values.Add("");
        return "Entry";
    }

    UFUNCTION(BlueprintCallable)
    TArray<int64> EchoInt64Array(TArray<int64> Values)
    {
        TArray<int64> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    TArray<uint32> EchoUInt32Array(TArray<uint32> Values)
    {
        TArray<uint32> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    TArray<uint64> EchoUInt64Array(TArray<uint64> Values)
    {
        TArray<uint64> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    TArray<float32> EchoFloat32Array(TArray<float32> Values)
    {
        TArray<float32> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    TArray<float64> EchoFloat64Array(TArray<float64> Values)
    {
        TArray<float64> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    UFUNCTION(BlueprintCallable)
    TArray<bool> EchoBoolArray(TArray<bool> Values)
    {
        TArray<bool> Result;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Result.Add(Values[Index]);
        }
        return Result;
    }

    // Six pure-output arrays are filled in one call; false checks the empty batch.
    UFUNCTION(BlueprintCallable)
    void WriteNumericArrays(bool WithValues,
                            TArray<int64>&out Int64Values,
                            TArray<uint32>&out UInt32Values,
                            TArray<uint64>&out UInt64Values,
                            TArray<float32>&out Float32Values,
                            TArray<float64>&out Float64Values,
                            TArray<bool>&out BoolValues)
    {
        Int64Values.Empty();
        UInt32Values.Empty();
        UInt64Values.Empty();
        Float32Values.Empty();
        Float64Values.Empty();
        BoolValues.Empty();
        if (WithValues)
        {
            Int64Values.Add(int64(-9223372036854775807 - 1));
            Int64Values.Add(int64(9223372036854775807));
            UInt32Values.Add(uint32(0));
            UInt32Values.Add(uint32(4294967295));
            UInt64Values.Add(uint64(0));
            UInt64Values.Add(uint64(18446744073709551615));
            Float32Values.Add(-2.5f);
            Float32Values.Add(0.0f);
            Float32Values.Add(1.25f);
            Float64Values.Add(-2.5);
            Float64Values.Add(0.0);
            Float64Values.Add(1.25);
            BoolValues.Add(true);
            BoolValues.Add(false);
            BoolValues.Add(true);
        }
    }

    // [] -> 0; [Entry, miss, Entry] -> 304; [None, Entry] -> 211.
    // Read-only marker checks the array length and FName equality; no world access.
    UFUNCTION(BlueprintCallable)
    int NameArrayMarker(TArray<FName> Values)
    {
        int Marker = Values.Num() * 100;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            if (Values[Index] == n"Entry")
            {
                Marker += 2;
            }
            else if (Values[Index] == n"")
            {
                Marker += 9;
            }
        }
        return Marker;
    }

    // Empty -> 0; signed mixed DTOs -> 1000; duplicate negative DTOs -> -18.
    // Reuse the existing scalar checksum; acceptance values avoid integer overflow.
    UFUNCTION(BlueprintCallable)
    int SumScalarArray(TArray<FG1RMCPScalarInput> Values)
    {
        int Marker = 0;
        for (int Index = 0; Index < Values.Num(); Index++)
        {
            Marker += ScalarStructMarker(Values[Index]);
        }
        return Marker;
    }

    // {Integer:7, Number:2.75, Flag:true} -> 1009;
    // {Integer:-7, Number:-2.75, Flag:false} -> -9; zero fields -> 0.
    UFUNCTION(BlueprintCallable)
    int ScalarStructMarker(FG1RMCPScalarInput Value)
    {
        int Marker = Value.Integer + int(Value.Number);
        if (Value.Flag)
        {
            Marker += 1000;
        }
        return Marker;
    }
}
