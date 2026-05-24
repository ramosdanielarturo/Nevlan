"""
Test script to verify tool call cleanup mechanism
"""
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brain.agent import agent

def test_cleanup_incomplete_tool_calls():
    """Test that incomplete tool calls are properly cleaned up"""
    
    # Simulate a scenario where agent has tool_calls but no responses
    agent.history = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "test command"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_test_123",
                    "type": "function",
                    "function": {"name": "web_navigate", "arguments": '{"url": "https://example.com"}'}
                },
                {
                    "id": "call_test_456",
                    "type": "function",
                    "function": {"name": "web_get_info", "arguments": '{}'}
                }
            ]
        }
    ]
    
    print("Before cleanup:")
    print(f"History length: {len(agent.history)}")
    for i, msg in enumerate(agent.history):
        print(f"  [{i}] role={msg.get('role')}, tool_calls={len(msg.get('tool_calls', []))}, tool_call_id={msg.get('tool_call_id', 'N/A')}")
    
    # Run cleanup
    agent._cleanup_incomplete_tool_calls()
    
    print("\nAfter cleanup:")
    print(f"History length: {len(agent.history)}")
    for i, msg in enumerate(agent.history):
        print(f"  [{i}] role={msg.get('role')}, tool_calls={len(msg.get('tool_calls', []))}, tool_call_id={msg.get('tool_call_id', 'N/A')}")
    
    # Verify that tool responses were added
    tool_responses = [msg for msg in agent.history if msg.get("role") == "tool"]
    assert len(tool_responses) == 2, f"Expected 2 tool responses, got {len(tool_responses)}"
    
    tool_call_ids = {resp["tool_call_id"] for resp in tool_responses}
    assert "call_test_123" in tool_call_ids, "Missing response for call_test_123"
    assert "call_test_456" in tool_call_ids, "Missing response for call_test_456"
    
    print("\n[PASS] Test passed: All incomplete tool calls were cleaned up")

def test_partial_cleanup():
    """Test cleanup when some tool calls have responses"""
    
    agent.history = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "test command"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "tool1", "arguments": '{}'}
                },
                {
                    "id": "call_def",
                    "type": "function",
                    "function": {"name": "tool2", "arguments": '{}'}
                }
            ]
        },
        # Only one tool response exists
        {"role": "tool", "tool_call_id": "call_abc", "content": "Success"}
    ]
    
    print("\n" + "="*60)
    print("Test: Partial cleanup (one response missing)")
    print("="*60)
    
    print("\nBefore cleanup:")
    print(f"History length: {len(agent.history)}")
    
    agent._cleanup_incomplete_tool_calls()
    
    print("\nAfter cleanup:")
    print(f"History length: {len(agent.history)}")
    
    tool_responses = [msg for msg in agent.history if msg.get("role") == "tool"]
    assert len(tool_responses) == 2, f"Expected 2 tool responses, got {len(tool_responses)}"
    
    print("\n[PASS] Test passed: Missing tool call was cleaned up")

def test_no_cleanup_needed():
    """Test that cleanup doesn't add responses when all are present"""
    
    agent.history = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "test command"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_xyz", "type": "function", "function": {"name": "tool1", "arguments": '{}'}}
            ]
        },
        {"role": "tool", "tool_call_id": "call_xyz", "content": "Success"}
    ]
    
    print("\n" + "="*60)
    print("Test: No cleanup needed (all responses present)")
    print("="*60)
    
    initial_length = len(agent.history)
    agent._cleanup_incomplete_tool_calls()
    
    assert len(agent.history) == initial_length, "History should not change when all responses are present"
    
    print("\n[PASS] Test passed: No unnecessary cleanup performed")

if __name__ == "__main__":
    test_cleanup_incomplete_tool_calls()
    test_partial_cleanup()
    test_no_cleanup_needed()
    
    print("\n" + "="*60)
    print("[SUCCESS] All tests passed!")
    print("="*60)
