
import threading
import time
from app.skills.tools.web import _get_page, web_navigate, web_get_info

def worker_task(worker_id):
    print(f"Worker {worker_id}: Getting page...")
    page = _get_page()
    if not page:
        print(f"Worker {worker_id}: Failed to get page")
        return

    print(f"Worker {worker_id}: Got page. Title: {page.title()}")
    
    # Simulate work
    time.sleep(1)

def test_web_persistence():
    print("🌐 Testing Web Persistence across Threads...")
    
    # 1. Main Thread Navigation
    print("Main: Navigating to example.com")
    web_navigate("test_call_1", "https://example.com")
    
    # 2. Worker Thread Access
    t1 = threading.Thread(target=worker_task, args=(1,))
    t1.start()
    t1.join()
    
    # 3. Another Worker Thread Access
    t2 = threading.Thread(target=worker_task, args=(2,))
    t2.start()
    t2.join()
    
    # 4. Verify URL hasn't changed (should still be example.com)
    page = _get_page()
    if "Example Domain" in page.title():
        print("✅ SUCCESS: Browser session persisted across threads.")
    else:
        print(f"❌ FAILURE: Browser lost context. Current title: {page.title()}")

if __name__ == "__main__":
    test_web_persistence()
