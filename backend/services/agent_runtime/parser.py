import re
import json
import yaml
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Literal

@dataclass
class ParsedTurn:
    type: Literal["action", "final_answer", "error"]
    thought: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    final_answer: Optional[str] = None
    error_message: Optional[str] = None

# Optimization: Pre-compile regular expressions at module level to avoid
# repeated compilation overhead on every turn of the agent reasoning loop.
RE_FINAL_ANSWER = re.compile(r"Final Answer:\s*(.*)", flags=re.DOTALL | re.IGNORECASE)
RE_ACTION = re.compile(r"Action:\s*([a-zA-Z0-9_\-]+)", flags=re.IGNORECASE)
RE_THOUGHT_FA = re.compile(r"Thought:\s*(.*?)(?=Final Answer:)", flags=re.DOTALL | re.IGNORECASE)
RE_THOUGHT_ACT = re.compile(r"Thought:\s*(.*?)(?=Action:)", flags=re.DOTALL | re.IGNORECASE)
RE_ACTION_INPUT = re.compile(r"Action Input:\s*(.*)", flags=re.DOTALL | re.IGNORECASE)
RE_CUT_OBSERVATION = re.compile(r"\n(?:Observation|Thought|Final Answer):", flags=re.IGNORECASE)
RE_XML_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", flags=re.DOTALL | re.IGNORECASE)

RE_FENCE_START = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
RE_FENCE_END = re.compile(r"\s*```$")
RE_UNESCAPED_BACKSLASH = re.compile(r'\\([^\/\\bfnrtu"0-9])')
RE_TRAILING_COMMA = re.compile(r',\s*([}\]])')

def clean_and_repair_json(raw_str: str) -> Dict[str, Any]:
    """Applies multiple heuristics to parse potentially malformed JSON."""
    s = raw_str.strip()

    # Optimization: Fast path for well-formed JSON object or array to skip regex and string slicing
    if s and s[0] in ("{", "["):
        try:
            return json.loads(s)
        except Exception:
            pass

    # 1. Strip markdown code fences
    s = RE_FENCE_START.sub("", s)
    s = RE_FENCE_END.sub("", s)
    s = s.strip()

    # 2. Extract substring between outermost braces if extra text exists
    first_brace = s.find("{")
    last_brace = s.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        s = s[first_brace:last_brace + 1]

    # 3. Direct JSON attempt
    try:
        return json.loads(s)
    except Exception:
        pass

    # 4. Repair unescaped regex backslashes (e.g. connect\(\) -> connect\\(\\))
    repaired = RE_UNESCAPED_BACKSLASH.sub(r'\\\\\1', s)
    # 5. Remove trailing commas before closing braces/brackets
    repaired = RE_TRAILING_COMMA.sub(r'\1', repaired)

    try:
        return json.loads(repaired)
    except Exception:
        pass

    # 6. Single quotes to double quotes replacement (if python dict representation)
    try:
        sq_replaced = s.replace("'", '"')
        sq_replaced = RE_TRAILING_COMMA.sub(r'\1', sq_replaced)
        return json.loads(sq_replaced)
    except Exception:
        pass

    # 7. Fallback to YAML safe load (which handles lenient JSON)
    try:
        data = yaml.safe_load(s)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    raise ValueError(f"Could not parse valid JSON from: '{raw_str[:120]}...'")

def parse_model_output(output_text: str, available_tools: List[str]) -> ParsedTurn:
    """Parses model response into an Action, Final Answer, or Error."""
    text = output_text.strip()

    # 1. Check for Final Answer marker
    fa_match = RE_FINAL_ANSWER.search(text)
    act_match = RE_ACTION.search(text)

    if fa_match and (not act_match or fa_match.start() < act_match.start()):
        thought_match = RE_THOUGHT_FA.search(text)
        thought = thought_match.group(1).strip() if thought_match else None
        final_answer = fa_match.group(1).strip()
        return ParsedTurn(type="final_answer", thought=thought, final_answer=final_answer)

    # 2. Check for ReAct Action: <tool> and Action Input: <json>
    if act_match:
        tool_name = act_match.group(1).strip()
        thought_match = RE_THOUGHT_ACT.search(text)
        thought = thought_match.group(1).strip() if thought_match else None

        input_match = RE_ACTION_INPUT.search(text)
        raw_input = input_match.group(1).strip() if input_match else "{}"

        # If Observation or another Thought appears after Action Input, slice it off
        cut_match = RE_CUT_OBSERVATION.search(raw_input)
        if cut_match:
            raw_input = raw_input[:cut_match.start()].strip()

        if tool_name not in available_tools:
            return ParsedTurn(
                type="error",
                thought=thought,
                tool_name=tool_name,
                error_message=f"Tool '{tool_name}' is not in available tools: {available_tools}"
            )

        try:
            tool_args = clean_and_repair_json(raw_input)
            return ParsedTurn(type="action", thought=thought, tool_name=tool_name, tool_args=tool_args)
        except Exception as e:
            return ParsedTurn(type="error", thought=thought, tool_name=tool_name, error_message=str(e))

    # 3. Check for JSON Action block: {"action": "...", "action_input": {...}}
    try:
        data = clean_and_repair_json(text)
        action = data.get("action") or data.get("tool")
        if action and action in available_tools:
            args = data.get("action_input") or data.get("parameters") or {}
            thought = data.get("thought")
            return ParsedTurn(type="action", thought=thought, tool_name=action, tool_args=args)
    except Exception:
        pass

    # 4. Check for XML Tool Call tags: <tool_call>{...}</tool_call>
    xml_match = RE_XML_TOOL_CALL.search(text)
    if xml_match:
        try:
            tc_data = clean_and_repair_json(xml_match.group(1))
            name = tc_data.get("name") or tc_data.get("tool")
            args = tc_data.get("arguments") or tc_data.get("parameters") or {}
            if name in available_tools:
                return ParsedTurn(type="action", tool_name=name, tool_args=args)
        except Exception as e:
            return ParsedTurn(type="error", error_message=str(e))

    # 5. Default fallback: treat entire text as conversational final answer
    return ParsedTurn(type="final_answer", final_answer=text)
