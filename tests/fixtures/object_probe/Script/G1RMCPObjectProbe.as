// Проверочный пакет использует только собственные UObject и явные вызовы.
// Синтаксис сверён с официальными примерами; загрузка требует проверки в игре.
UCLASS()
class UG1RMCPObjectProbe : UObject
{
    UPROPERTY()
    UObject StoredObject = nullptr;

    UPROPERTY()
    UG1RMCPObjectProbe StoredTypedObject = nullptr;

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 41;
    }

    UFUNCTION(BlueprintCallable)
    UObject ReadSelf()
    {
        return this;
    }

    UFUNCTION(BlueprintCallable)
    UObject EchoObject(UObject Input)
    {
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    UG1RMCPObjectProbe EchoTypedObject(UG1RMCPObjectProbe Input)
    {
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    bool IsNullObject(UObject Input)
    {
        return Input == nullptr;
    }

    UFUNCTION(BlueprintCallable)
    void StoreObject(UObject Input)
    {
        StoredObject = Input;
    }

    UFUNCTION(BlueprintCallable)
    void StoreTypedObject(UG1RMCPObjectProbe Input)
    {
        StoredTypedObject = Input;
    }
}

UCLASS()
class UG1RMCPDerivedObjectProbe : UG1RMCPObjectProbe
{
}

UCLASS()
class UG1RMCPOtherObjectProbe : UObject
{
    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 99;
    }
}
