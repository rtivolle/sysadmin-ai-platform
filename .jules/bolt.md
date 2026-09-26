## 2026-09-24 - Pre-compiling Regexes and Fast-Pathing JSON Parsing in Agent Loops
**Learning:** Re-compiling regular expressions on every agent turn and running regex search/sub routines before direct `json.loads` parsing adds significant per-turn latency in the ReAct reasoning parser.
**Action:** Always pre-compile regexes at the module level for parsing functions and attempt direct JSON parsing (`s[0] in ('{', '[')`) before running fallback regex sanitization or repair heuristics.
