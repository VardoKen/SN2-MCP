// Только собственный CDO. Массив строится игровым allocator через AngelScript.
UCLASS()
class UG1RMCPPODReadProbe : UObject
{
    UPROPERTY()
    TArray<int> Numbers;

    UPROPERTY()
    TArray<int> EmptyNumbers;

    UPROPERTY()
    TArray<FName> Names;
    default Names.Add(n"Entry");
    default Names.Add(n"");

    UPROPERTY()
    FVector StoredVector = FVector(1.25f, -2.5f, 3.75f);

    UPROPERTY()
    FRotator StoredRotation;
    default StoredRotation = FRotator(10.0f, -20.0f, 30.0f);

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 101;
    }

    UFUNCTION(BlueprintCallable)
    int ReadNumberCount()
    {
        return Numbers.Num();
    }

    UFUNCTION(BlueprintCallable)
    int ReadEmptyCount()
    {
        return EmptyNumbers.Num();
    }

    UFUNCTION(BlueprintCallable)
    int ReadNameCount()
    {
        return Names.Num();
    }

    UFUNCTION(BlueprintCallable)
    FName ReadNameAt(int Index)
    {
        if (Index < 0 || Index >= Names.Num())
        {
            return n"";
        }
        return Names[Index];
    }

    UFUNCTION(BlueprintCallable)
    int ReadNumberAt(int Index)
    {
        if (Index < 0 || Index >= Numbers.Num())
        {
            return -1000000;
        }
        return Numbers[Index];
    }

    UFUNCTION(BlueprintCallable)
    void SetupFixture()
    {
        Numbers.Empty();
        for (int i = 0; i < 205; ++i)
        {
            Numbers.Add(i * 3 - 7);
        }
    }

    UFUNCTION(BlueprintCallable)
    void ClearFixture()
    {
        Numbers.Empty();
    }

    UFUNCTION(BlueprintCallable)
    float ReadVectorX()
    {
        return StoredVector.X;
    }

    UFUNCTION(BlueprintCallable)
    float ReadVectorY()
    {
        return StoredVector.Y;
    }

    UFUNCTION(BlueprintCallable)
    float ReadVectorZ()
    {
        return StoredVector.Z;
    }

    UFUNCTION(BlueprintCallable)
    float ReadPitch()
    {
        return StoredRotation.Pitch;
    }

    UFUNCTION(BlueprintCallable)
    float ReadYaw()
    {
        return StoredRotation.Yaw;
    }

    UFUNCTION(BlueprintCallable)
    float ReadRoll()
    {
        return StoredRotation.Roll;
    }
}
