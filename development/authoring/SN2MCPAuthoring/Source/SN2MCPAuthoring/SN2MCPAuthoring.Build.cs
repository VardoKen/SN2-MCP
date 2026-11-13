using UnrealBuildTool;

public class SN2MCPAuthoring : ModuleRules
{
    public SN2MCPAuthoring(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PrivateDependencyModuleNames.AddRange(new[] {
            "Core", "CoreUObject", "Engine", "UnrealEd", "BlueprintGraph",
            "KismetCompiler", "AssetRegistry", "Json", "JsonUtilities"
        });
    }
}
