"""Runs generated operations inside a throwaway container and prints results as JSON.

/work/input.json: {"session": {...}, "calls": [{"op": "module_name", "params": {...}}, ...]}
Calls run in order, so a reversible write can be followed by its undo; execution stops at the first failure.
"""
import importlib
import json
import sys
import traceback

sys.path[:0] = ["/work", "/opt/runner"]
import runtime  # noqa: E402

MAX_OUTPUT = 20_000


def main():
    with open("/work/input.json") as f:
        inp = json.load(f)
    results = []
    for call in inp["calls"]:
        entry = {"op": call["op"]}
        try:
            mod = importlib.import_module(call["op"])
            out = mod.run(inp["session"], **(call.get("params") or {}))
            text = json.dumps(out, default=str)
            entry.update(ok=True, output_type=type(out).__name__, truncated=len(text) > MAX_OUTPUT,
                         output_keys=sorted(out) if isinstance(out, dict) else None,
                         output=json.loads(text) if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT])
        except runtime.OperationError as e:
            entry.update(ok=False, error_code=e.code, error=str(e))
        except Exception:
            entry.update(ok=False, error_code="code_error", error=traceback.format_exc()[-2500:])
        entry["http"] = dict(runtime.LAST)
        results.append(entry)
        if not entry["ok"]:
            break  # never run an undo after a failed do: it could reverse the user's own prior state
    print(json.dumps(results, default=str))


if __name__ == "__main__":
    main()
