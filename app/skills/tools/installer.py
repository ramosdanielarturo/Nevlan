from app.skills.registry import registry
from app.contracts.tool_result import ToolResult
from app.services.capabilities.recipe_installer import RecipeInstaller

installer = RecipeInstaller()

@registry.register(name="os.packages_install")
def packages_install(call_id: str, packages: list[str], reason: str) -> ToolResult:
    """
    Instala paquetes permitidos (allowlist) si es necesario.
    Requiere confirmación del usuario (a través de Policy).
    """
    results = []
    failed = []
    
    for pkg in packages:
        if installer.is_installed(pkg):
            results.append(f"{pkg} (already installed)")
            continue
            
        recipe = installer.get_recipe(pkg)
        if not recipe:
            failed.append(f"{pkg} (not in allowlist)")
            continue
            
        # Attempt install
        if installer.install(pkg):
            results.append(f"{pkg} (installed)")
        else:
            failed.append(f"{pkg} (failed)")
            
    if failed:
        return ToolResult(call_id=call_id, success=False, error=f"Could not install: {', '.join(failed)}. Installed: {', '.join(results)}")
        
    return ToolResult(call_id=call_id, result={"message": f"Installed/Verified: {', '.join(results)}", "installed": results})
