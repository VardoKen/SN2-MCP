// Только собственный CDO. Const-ref вход копируется; компоненты читаются через AS API.
UCLASS()
class UG1RMCPConstTransformProbe : UObject
{
    UPROPERTY()
    int CallsCount = 0;

    UPROPERTY()
    FTransform StoredTransform = FTransform(FRotator(0.0f, 0.0f, 0.0f), FVector(0.0f, 0.0f, 0.0f), FVector(1.0f, 1.0f, 1.0f));

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 201;
    }

    UFUNCTION(BlueprintCallable)
    bool AcceptConstTransform(const FTransform&in Input)
    {
        StoredTransform = Input;
        CallsCount++;
        return true;
    }

    UFUNCTION(BlueprintCallable)
    FVector GetTranslation()
    {
        return StoredTransform.GetLocation();
    }

    UFUNCTION(BlueprintCallable)
    FVector GetScale3D()
    {
        return StoredTransform.GetScale3D();
    }

    UFUNCTION(BlueprintCallable)
    FVector GetRotationEuler()
    {
        FRotator Rotation = StoredTransform.Rotator();
        return FVector(Rotation.Pitch, Rotation.Yaw, Rotation.Roll);
    }

    UFUNCTION(BlueprintCallable)
    int ReadCallsCount()
    {
        return CallsCount;
    }

    UFUNCTION(BlueprintCallable)
    void ResetProbe()
    {
        StoredTransform = FTransform(FRotator(0.0f, 0.0f, 0.0f), FVector(0.0f, 0.0f, 0.0f), FVector(1.0f, 1.0f, 1.0f));
        CallsCount = 0;
    }
}
