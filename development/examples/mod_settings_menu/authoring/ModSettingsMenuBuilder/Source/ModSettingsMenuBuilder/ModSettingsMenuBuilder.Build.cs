using UnrealBuildTool;

public class ModSettingsMenuBuilder : ModuleRules
{
    public ModSettingsMenuBuilder(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PrivateDependencyModuleNames.AddRange(new[] {
            "Core", "CoreUObject", "Engine", "UnrealEd", "Kismet",
            "BlueprintGraph", "KismetCompiler", "UMG", "UMGEditor",
            "Json", "JsonUtilities"
        });
    }
}
