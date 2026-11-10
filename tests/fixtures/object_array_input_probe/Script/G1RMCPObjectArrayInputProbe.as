// Только собственный CDO. Массив и его элементы не изменяются и не сохраняются.
UCLASS()
class UG1RMCPObjectArrayInputProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 221;
    }

    UFUNCTION(BlueprintCallable)
    int ReadCount(const TArray<UObject>&in Input)
    {
        CallsCount++;
        return Input.Num();
    }

    UFUNCTION(BlueprintCallable)
    int ReadNullCount(const TArray<UObject>&in Input)
    {
        CallsCount++;
        int NullCount = 0;
        for (int Index = 0; Index < Input.Num(); Index++)
        {
            if (Input[Index] == nullptr)
                NullCount++;
        }
        return NullCount;
    }

    UFUNCTION(BlueprintCallable)
    bool ContainsSelf(const TArray<UObject>&in Input)
    {
        CallsCount++;
        for (int Index = 0; Index < Input.Num(); Index++)
        {
            if (Input[Index] == this)
                return true;
        }
        return false;
    }

    UFUNCTION(BlueprintCallable)
    FString ReadMixedEcho(const TArray<UObject>&in Input, const FString&in Text)
    {
        CallsCount++;
        return Text;
    }

    UFUNCTION(BlueprintCallable)
    FString ReadArrayMarker(const TArray<UObject>&in Input)
    {
        CallsCount++;
        return "mixed-array";
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
