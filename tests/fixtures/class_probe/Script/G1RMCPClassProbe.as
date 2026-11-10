// Только собственные классы и явные вызовы; игровые механики не используются.
UCLASS()
class UG1RMCPClassBase : UObject
{
}

UCLASS()
class UG1RMCPClassDerived : UG1RMCPClassBase
{
}

UCLASS()
class UG1RMCPClassOther : UObject
{
}

UCLASS()
class UG1RMCPClassProbe : UObject
{
    UPROPERTY()
    TSubclassOf<UG1RMCPClassBase> StoredTypedClass = UG1RMCPClassBase;

    UPROPERTY()
    UClass StoredAnyClass = UG1RMCPClassBase::StaticClass();

    UFUNCTION(BlueprintCallable)
    int ReadMarker()
    {
        return 61;
    }

    UFUNCTION(BlueprintCallable)
    UClass ReadBaseClass()
    {
        return UG1RMCPClassBase;
    }

    UFUNCTION(BlueprintCallable)
    UClass EchoAnyClass(UClass Input)
    {
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    TSubclassOf<UG1RMCPClassBase> EchoTypedClass(TSubclassOf<UG1RMCPClassBase> Input)
    {
        return Input;
    }

    UFUNCTION(BlueprintCallable)
    void StoreAnyClass(UClass Input)
    {
        StoredAnyClass = Input;
    }

    UFUNCTION(BlueprintCallable)
    void StoreTypedClass(TSubclassOf<UG1RMCPClassBase> Input)
    {
        StoredTypedClass = Input;
    }
}
