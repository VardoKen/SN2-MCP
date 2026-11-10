// Проверочный тип: UObject и UFUNCTION сверены с официальными исходниками.
// Компиляция и загрузка этого конкретного класса требуют проверки в игре.
UCLASS()
class UG1RMCPCompileProbe : UObject
{
    UFUNCTION(BlueprintCallable)
    int ReadProbeValue()
    {
        return 1;
    }
}
