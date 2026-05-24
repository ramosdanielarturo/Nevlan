import yaml
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

class RecipeInstaller:
    def __init__(self, config_path: str = "app/services/capabilities/recipes.yaml"):
        self.config = self._load_config(config_path)

    def _load_config(self, path_str: str) -> Dict:
        path = Path(path_str)
        if not path.exists():
            # Try relative to this file
            path = Path(__file__).parent / "recipes.yaml"
            
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return {"allowlist": []}

    def get_recipe(self, name: str) -> Optional[Dict]:
        for recipe in self.config.get("allowlist", []):
            if recipe["name"] == name:
                return recipe
        return None

    def is_installed(self, name: str) -> bool:
        # Basic check: try importing if it's a python package
        recipe = self.get_recipe(name)
        if not recipe:
            return False
            
        if "pip_package" in recipe:
            try:
                pass 
                # Verification logic: pip show or import
                # For now just use pip show to not actually import everything
                res = subprocess.run([sys.executable, "-m", "pip", "show", recipe["pip_package"]], 
                                     capture_output=True)
                return res.returncode == 0
            except:
                return False
        return False

    def install(self, name: str) -> bool:
        recipe = self.get_recipe(name)
        if not recipe:
            print(f"Recipe {name} not found in allowlist.")
            return False
            
        print(f"Installing {name}...")
        
        if "pip_package" in recipe:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", recipe["pip_package"]])
                
                if "post_install" in recipe:
                    subprocess.check_call(recipe["post_install"])
                    
                return True
            except subprocess.CalledProcessError as e:
                print(f"Error installing {name}: {e}")
                return False
                
        return False
