// Изолированная проверка собственных исходников, без игровой механики.
UCLASS()
class UG1RMCPPatchProbe : UObject
{
    UFUNCTION(BlueprintCallable)
    int ReadPatchMarker()
    {
        return 10;
    }
}
