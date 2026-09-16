#!/usr/bin/env python3
"""
Real MapIO Graph Tool Execution Evaluator
Runs simple_camio_llm's ACTUAL graph logic (src.graph.Graph and PromptFormatter)
against real map models (models/new_york/new_york.json) using local LLM endpoint.
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.error

# Ensure explore/simple_camio_llm is on python path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from src.graph import Graph
from src.utils import Coords
from src.llm.prompt_formatter import PromptFormatter

def load_map_model(model_path):
    with open(model_path, "r", encoding="utf-8") as f:
        return json.load(f)

def dummy_on_route(route):
    pass

def send_chat_completion(base_url, model, messages, tools, timeout=120):
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data_bytes,
        headers={"Content-Type": "application/json", "User-Agent": "RealCamioEval/1.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def resolve_working_server(server_url):
    candidate_urls = [server_url]
    
    # Extract port and path
    from urllib.parse import urlparse
    parsed = urlparse(server_url)
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path if parsed.path else "/v1"
    scheme = parsed.scheme or "http"
    
    # Try WSL Gateway IP if inside WSL
    if os.path.exists("/proc/version"):
        try:
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower():
                    # Read default gateway
                    with open("/proc/net/route") as rf:
                        for line in rf:
                            fields = line.strip().split()
                            if fields[1] == '00000000':
                                import socket, struct
                                gw_ip = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
                                candidate_urls.append(f"{scheme}://{gw_ip}{port}{path}")
                                break
                    candidate_urls.append(f"{scheme}://127.0.0.1{port}{path}")
                    candidate_urls.append(f"{scheme}://host.docker.internal{port}{path}")
        except Exception:
            pass
            
    for url in candidate_urls:
        try:
            probe_url = f"{url.rstrip('/')}/models"
            req = urllib.request.Request(probe_url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                models_info = json.loads(resp.read().decode("utf-8")).get("data", [])
                available = [m.get("id") for m in models_info]
                print(f"[OK] Connected to LLM server at '{url}'. Models: {available}")
                return url, available
        except Exception:
            continue
            
    return server_url, []

def main():
    parser = argparse.ArgumentParser(description="Evaluate Real MapIO Tool Execution on Actual Maps")
    parser.add_argument("--map", default="models/new_york/new_york.json", help="Path to map json model")
    parser.add_argument("--prompt", default="res/prompt_en.yaml", help="Path to prompt yaml file")
    parser.add_argument("--server", default="http://localhost:11434/v1", help="OpenAI-compatible server URL")
    parser.add_argument("--model", default="auto", help="LLM model name")
    parser.add_argument("--question", default="what shops or restaurants are near 5th avenue?", help="User question")
    args = parser.parse_args()

    map_file = os.path.join(script_dir, args.map) if not os.path.isabs(args.map) else args.map
    prompt_file = os.path.join(script_dir, args.prompt) if not os.path.isabs(args.prompt) else args.prompt

    # Check server models with WSL gateway fallback
    server_url, available_models = resolve_working_server(args.server)
    model_name = args.model
    if model_name == "auto" or model_name not in available_models:
        if available_models:
            model_name = available_models[0]
            print(f"Auto-selected model: {model_name}")

    print(f"Loading REAL map model: {map_file}...")
    map_data = load_map_model(map_file)
    graph = Graph(map_data["graph"], dummy_on_route)

    # Window graph elements for system prompt generation (prevents 30k token context overflow)
    # Full graph remains intact for real tool execution math
    all_nodes = graph.nodes
    all_edges = graph.edges
    all_pois = graph.pois

    graph.nodes = all_nodes[:30]
    graph.edges = all_edges[:30]
    graph.pois = all_pois[:25]

    formatter = PromptFormatter(prompt_file, graph)
    context = {"name": map_data.get("name", "New York")}
    sys_message = formatter.get_main_prompt(context)
    user_message = formatter.get_user_message(args.question, position=None)

    # Restore full graph for accurate tool execution calculations
    graph.nodes = all_nodes
    graph.edges = all_edges
    graph.pois = all_pois
    
    messages = [sys_message, user_message]
    tools = formatter.get_tool_calls()

    print(f"\nSending question to LLM: '{args.question}'...")
    print(f"Active tools offered: {len(tools)}")
    
    round_num = 1
    max_rounds = 4
    final_text = ""
    while round_num <= max_rounds:
        print(f"\n[Round {round_num}: Sending request to LLM...]")
        response = send_chat_completion(server_url, model_name, messages, tools=tools)
        choice = response.get("choices", [{}])[0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content") or ""

        if content:
            final_text += content + "\n"

        clean_msg = {
            "role": "assistant",
            "content": content,
        }
        if tool_calls:
            clean_msg["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{round_num}_{i}"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]
        messages.append(clean_msg)

        if not tool_calls:
            print("[LLM Finished Tool Loop]")
            break

        for i, tc in enumerate(tool_calls):
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            tc_id = tc.get("id", f"call_{round_num}_{i}")
            print(f"  Tool Call: {fn_name}")
            print(f"  Arguments: {fn_args}")

            class FunctionMock:
                def __init__(self, name, arguments):
                    self.name = name
                    self.arguments = arguments

            class ToolCallMock:
                def __init__(self, call_id, name, arguments):
                    self.id = call_id
                    self.type = "function"
                    self.function = FunctionMock(name, arguments)

            tool_call_obj = ToolCallMock(
                call_id=tc_id,
                name=fn_name,
                arguments=fn_args
            )

            print("  [Executing Graph Tool on Real Map Data...]")
            tool_result_param = formatter.handle_tool_call(tool_call_obj)
            tool_content = str(tool_result_param.get("content", ""))
            print(f"  Tool Result: {tool_content}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_content,
            })

        round_num += 1

    print("\n" + "="*80)
    print("FINAL REAL NARRATION RESULT:")
    print("="*80)
    print(final_text.strip())
    print("="*80)

if __name__ == "__main__":
    main()
