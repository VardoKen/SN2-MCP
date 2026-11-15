#pragma once

#include "Commandlets/Commandlet.h"
#include "ModSettingsMenuBuilderCommandlet.generated.h"

UCLASS()
class MODSETTINGSMENUBUILDER_API UModSettingsMenuBuilderCommandlet : public UCommandlet
{
    GENERATED_BODY()

public:
    explicit UModSettingsMenuBuilderCommandlet(const FObjectInitializer& ObjectInitializer);
    virtual int32 Main(const FString& Params) override;
};
