#pragma once

#include "Commandlets/Commandlet.h"
#include "SN2MCPAuthoringCommandlet.generated.h"

UCLASS()
class SN2MCPAUTHORING_API USN2MCPAuthoringCommandlet : public UCommandlet
{
    GENERATED_BODY()
public:
    USN2MCPAuthoringCommandlet();
    virtual int32 Main(const FString& Params) override;
};
