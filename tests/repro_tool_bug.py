"""
Reproduction Script for OpenAI Tool Call 400 Error
--------------------------------------------------
Simulates a race condition where a user message arrives while the agent is executing tools,
causing the tool result to be appended AFTER the user message, breaking the OpenAI invariant.
"""
import sys
import os
import json
import threading
import time

# Mock dependencies
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock Logger
class MockLogger:
    def info(self, msg): print(f"[INFO] {msg}")
    def warning(self, msg): print(f"[WARN] {msg}")
    def error(self, msg): print(f"[ERR] {msg}")
    def debug(self, msg): print(f"[DEBUG] {msg}")

import app.core.logger
app.core.logger.log = MockLogger()

# Mock Registry and LLM
from app.skills.registry import registry
from app.services.llm.factory import get_llm_provider
from app.contracts.tool_call import ToolCall
from app.contracts.tool_result import ToolResult

# Monkey patch LLM to return a tool call
class MockLLM:
    def generate_reply(self, history, tools):
        # If the last message is user "trigger_bug", return a tool call
        last = history[-1]
        if last["role"] == "user" and "trigger_bug" in last["content"]:
            # Returns a mock object similar to what the real LLM returns
            class MockResponse:
                content = None
                class ToolCallObj:
                    id = "call_123"
                    name = "mock_tool"
                    arguments = {}
                tool_calls = [ToolCallObj()]
            return MockResponse()
        
        # Otherwise normal response
        class NormalResponse:
            content = "Hello"
            tool_calls = []
        return NormalResponse()

import app.services.llm.factory
app.services.llm.factory.get_llm_provider = lambda: MockLLM()

# Mock Registry Execute to simulate delay
def mock_execute(call):
    time.sleep(1.0) # Simulate slow tool
    return ToolResult(call_id=call.id, result="Success")

registry.execute = mock_execute

# Fix ToolDef mock
class MockToolDef:
    def __init__(self, name):
        self.name = name
        self.description = "Mock description"
        self.fn = lambda: None

registry._tools = {"mock_tool": MockToolDef("mock_tool")}

# Import Agent
from app.brain.agent import Agent

def test_race_condition():
    agent = Agent()
    
    # Thread 1: Triggers a tool call
    def run_agent():
        print("T1: Starting agent request (will trigger tool)")
        agent.process_user_input("trigger_bug")
        print("T1: Finished")

    # Thread 2: Sends a user message while T1 is busy
    def run_interruption():
        time.sleep(0.2) # Wait for T1 to get the tool call and start "executing"
        print("T2: Sending interruption")
        agent.process_user_input("interruption")
        print("T2: Finished")

    t1 = threading.Thread(target=run_agent)
    t2 = threading.Thread(target=run_interruption)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    # Analyze History
    print("\n--- Final History ---")
    for i, msg in enumerate(agent.history):
        role = msg.get("role")
        content = str(msg.get("content"))[:50]
        tcs = msg.get("tool_calls", [])
        tc_ids = [tc["id"] for tc in tcs] if tcs else ""
        print(f"{i}: {role} | {content} | {tc_ids}")

    # Verification Logic
    # We expect: system, user(trigger), assistant(tool_call), tool(result), user(interrupt), assistant(hello)
    # Buggy state: system, user(trigger), assistant(tool_call), user(interrupt), tool(result)...
    
    # Scan for invalid sequence
    for i, msg in enumerate(agent.history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Next message MUST be tool
            if i + 1 >= len(agent.history):
                print("FAIL: Assistant with tools at end of history")
                return
            
            next_msg = agent.history[i+1]
            if next_msg.get("role") != "tool":
                print(f"FAIL: Found assistant(tool_calls) followed by {next_msg.get('role')}")
                print("This reproduces the OpenAI 400 error.")
                return

    print("PASS: History looks correct (Invariant maintained)")

if __name__ == "__main__":
    test_race_condition()
