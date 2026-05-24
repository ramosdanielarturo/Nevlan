import sys
import os
import time

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.skills.tools import web
from app.core.logger import log

def test_browser_recovery():
    print("Testing Browser Recovery...")
    
    # 1. Open Browser
    print("1. Opening Browser and navigating to Google...")
    try:
        web.web_navigate("test_1", "https://www.google.com")
        print("   Navigation successful.")
    except Exception as e:
        print(f"   Failed to open browser: {e}")
        return

    # 2. Simulate Crash (Kill Chrome)
    print("\n2. Simulating Chrome Crash (Force Kill)...")
    try:
        # Accessing internal function for testing
        web._force_kill_chrome()
        print("   Chrome killed.")
    except Exception as e:
        print(f"   Failed to kill chrome: {e}")
        return
        
    # Wait a moment for process to really die
    time.sleep(2)

    # 3. Try to navigate again
    print("\n3. Attempting navigation after crash (Should Auto-Recover)...")
    try:
        # This should trigger the "Connect timeout -> Force Kill (again) -> Relaunch" logic
        # OR "Connect error -> Relaunch" logic
        result = web.web_navigate("test_2", "https://www.example.com")
        
        if result.success:
            print("   RECOVERY SUCCESSFUL! Navigated to example.com")
            print(f"   Result: {result.result}")
        else:
            print(f"   Recovery Failed: {result.error}")
            
    except Exception as e:
        print(f"   Exception during recovery: {e}")

if __name__ == "__main__":
    test_browser_recovery()
