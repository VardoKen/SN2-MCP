// Только собственная мировая подсистема. FHitResult остаётся локальным AS-значением.
UCLASS()
class UG1RMCPHitResultWrapperProbe : UScriptWorldSubsystem
{
    UPROPERTY()
    int CallsCount = 0;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 251;
    }

    UFUNCTION(BlueprintCallable)
    bool HasWorld()
    {
        return GetWorld() != nullptr;
    }

    UFUNCTION(BlueprintCallable)
    bool TraceImpactPoint(FVector Start, FVector End, const TArray<AActor>&in ActorsToIgnore, FVector&out ImpactPoint)
    {
        CallsCount++;
        if (GetWorld() == nullptr)
        {
            ImpactPoint = FVector::ZeroVector;
            return false;
        }
        FHitResult HitResult = FHitResult();
        ETraceTypeQuery TraceType = UCollisionProfile::ConvertToTraceType(ECollisionChannel::ECC_Visibility);
        bool Hit = System::LineTraceSingle(Start, End, TraceType, false, ActorsToIgnore, EDrawDebugTrace::None, HitResult, true);
        ImpactPoint = Hit ? HitResult.ImpactPoint : FVector::ZeroVector;
        return Hit;
    }

    UFUNCTION(BlueprintCallable)
    FVector ReadDefaultImpactPoint()
    {
        CallsCount++;
        FHitResult HitResult = FHitResult();
        return HitResult.ImpactPoint;
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
