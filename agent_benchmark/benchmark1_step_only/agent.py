import base64
import json
import os
import time
import traceback
from datetime import datetime

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from tools import run_shell, run_python


# ============================================================
# CLIENT
# ============================================================

client = OpenAI(
    api_key=os.environ["STEPFUN_API_KEY"],
    base_url="https://api.stepfun.com/v1",
    timeout=120
)


MODEL = "step-3.5-flash"
MODEL_MIN_INTERVAL = int(os.environ.get("STEP_ONLY_MODEL_MIN_INTERVAL", "8"))
MODEL_MAX_RETRIES = int(os.environ.get("STEP_ONLY_MODEL_MAX_RETRIES", "20"))
MODEL_RATE_LIMIT_SLEEP = int(os.environ.get("STEP_ONLY_MODEL_RATE_LIMIT_SLEEP", "70"))
MODEL_RETRY_SLEEP = int(os.environ.get("STEP_ONLY_MODEL_RETRY_SLEEP", "15"))
TOOL_RESULT_MAX_CHARS = int(os.environ.get("STEP_ONLY_TOOL_RESULT_MAX_CHARS", "20000"))
last_model_call_at = 0.0


# ============================================================
# TOOLS
# ============================================================

tools = [

{
"type": "function",
"function": {
"name": "run_shell",
"description": """
Execute shell commands in the workspace.

Available software:
SnapATAC2
MACS3

Do not access CellNoteAgent resources.
""",
"parameters": {
"type": "object",
"properties": {
"command": {
"type": "string"
}
},
"required": ["command"]
}
}
},


{
"type": "function",
"function": {
"name": "run_python",
"description": """
Execute Python code.

Available packages:
SnapATAC2
Scanpy
AnnData
MACS3

Use this for bioinformatics analysis.
Save results to the requested output directory.
""",
"parameters": {
"type": "object",
"properties": {
"code": {
"type": "string"
}
},
"required": ["code"]
}
}
}

]


# ============================================================
# FIXED SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are a bioinformatics analysis agent.

You must actually perform the task.

You have:
- shell tool
- python tool

Allowed:
- SnapATAC2
- MACS3
- Scanpy
- AnnData

Forbidden:
- cell_note_agent/
- CellNoteAgent scripts/
- CellNoteAgent skills
- previous QC summaries
- previous logs
- previous outputs

Rules:
1. Inspect input files first.
2. Execute analysis instead of giving plans.
3. Do not fabricate results.
4. Save outputs into the output directory specified in the user prompt.
"""


# ============================================================
# FIXED TASK PROMPTS
#
# Preserve the exact original formal prompts. They are stored as Base64 so the
# published source remains ASCII-only while reproducing the original input.
# ============================================================

T1_PROMPT = base64.b64decode(
    "ClQx77yaTGkyMDIzYQror7fliIbmnpDov5nkuKrkurrnsbsgc2NBVEFDIOaVsOaNrumbhu+8mgovc3NkL2RlZWNhbXAvY2VsbG5vdGVzL0VwaUFnZW50X2RhdGEvTGkyMDIzYS9MaTIwMjNhLWJyYWluX3Rpc3N1ZS9MaTIwMjNhLWJyYWluX3Rpc3N1ZS1jZWxsX2J5X3BlYWsuaDVhZAoK6K+36Ieq5Yqo6K+G5Yir6L6T5YWl57G75Z6L77yM5omn6KGM5qCH5YeGIFFD77yM5bm26L6T5Ye654us56uL5pWw5o2u6ZuG55qEIEdSQ2gzOApjZWxsIMOXIHBlYWsgbWF0cml444CBcGVha3PjgIFiYXJjb2Rlc+OAgVFDIHN1bW1hcnnjgIFkYXRhIGNhcmQg5ZKMIE1BTklGRVNU44CCCuivt+WunumZheaJp+ihjOS7u+WKoe+8jOS4jeimgeWPque7meWHuuiuoeWIkuaIluekuuS+i+S7o+eggeOAggoK6L6T5Ye65qC555uu5b2V77yaCntvdXRwdXRfcm9vdH0KCnJ1biBJRO+8mgp7cnVuX2lkfQo="
).decode("utf-8")


T2_PROMPT = base64.b64decode(
    "ClQy77yaTGkyMDIzYgror7fliIbmnpDov5nkuKrkurrnsbsgc2NBVEFDIOaVsOaNrumbhu+8mgovc3NkL2RlZWNhbXAvY2VsbG5vdGVzL0VwaUFnZW50X2RhdGEvTGkyMDIzYi9MaTIwMjNiLWJyYWluX3Rpc3N1ZS9mcmFnbWVudHNfc3RhbmRhcmRpemVkCgror7foh6rliqjor4bliKvnm67lvZXkuK3nmoTovpPlhaXmlofku7bvvIzmiafooYzmoIflh4YgUUPvvIzku44gZnJhZ21lbnRzIOeUn+aIkOeLrOeriwrmlbDmja7pm4bnmoQgR1JDaDM4IGNlbGwgw5cgcGVhayBtYXRyaXjvvIzlubbovpPlh7ogcGVha3PjgIFiYXJjb2Rlc+OAgVFDIHN1bW1hcnnjgIEKZGF0YSBjYXJkIOWSjCBNQU5JRkVTVOOAguivt+WunumZheaJp+ihjOS7u+WKoe+8jOS4jeimgeWPque7meWHuuiuoeWIkuaIluekuuS+i+S7o+eggeOAggoK6L6T5Ye65qC555uu5b2V77yaCntvdXRwdXRfcm9vdH0KCnJ1biBJRO+8mgp7cnVuX2lkfQo="
).decode("utf-8")


# ============================================================
# TASK CONFIG
# ============================================================

TASKS = [
    {
        "name": "T1_Li2023a",
        "prompt": T1_PROMPT,
        "output_root": (
            "/ssd/deecamp/cellnotes/step_only_workspace/"
            "outputs/Li2023a"
        ),
        "run_id": "Li2023a_run1",
    },
    {
        "name": "T2_Li2023b",
        "prompt": T2_PROMPT,
        "output_root": (
            "/ssd/deecamp/cellnotes/step_only_workspace/"
            "outputs/Li2023b"
        ),
        "run_id": "Li2023b_run1",
    },
]


# ============================================================
# HELPERS
# ============================================================

def banner(text):
    print("\n" + "=" * 80, flush=True)
    print(text, flush=True)
    print("=" * 80 + "\n", flush=True)


def safe_json(obj):
    try:
        return json.dumps(
            compact_for_model(obj),
            ensure_ascii=False,
            default=str
        )
    except Exception:
        return str(obj)


def compact_for_model(obj):
    if isinstance(obj, str):
        if len(obj) <= TOOL_RESULT_MAX_CHARS:
            return obj

        head_chars = min(2000, TOOL_RESULT_MAX_CHARS // 4)
        tail_chars = TOOL_RESULT_MAX_CHARS - head_chars

        return (
            obj[:head_chars]
            + "\n\n...[tool output trimmed for model; full output was printed live]...\n\n"
            + obj[-tail_chars:]
        )

    if isinstance(obj, dict):
        return {
            key: compact_for_model(value)
            for key, value in obj.items()
        }

    if isinstance(obj, list):
        return [
            compact_for_model(value)
            for value in obj
        ]

    return obj


def call_model(messages):
    global last_model_call_at

    for attempt in range(1, MODEL_MAX_RETRIES + 1):

        now = time.monotonic()
        wait = MODEL_MIN_INTERVAL - (now - last_model_call_at)

        if wait > 0:
            print(
                f"[model] sleeping {wait:.1f}s to respect RPM limit",
                flush=True
            )
            time.sleep(wait)

        try:
            last_model_call_at = time.monotonic()

            return client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools
            )

        except RateLimitError as e:
            print(
                f"[model] rate limited, retry {attempt}/{MODEL_MAX_RETRIES}; "
                f"sleeping {MODEL_RATE_LIMIT_SLEEP}s",
                flush=True
            )
            print(
                str(e),
                flush=True
            )
            time.sleep(MODEL_RATE_LIMIT_SLEEP)

        except (APITimeoutError, APIConnectionError) as e:
            print(
                f"[model] API connection/timeout error, retry "
                f"{attempt}/{MODEL_MAX_RETRIES}; sleeping {MODEL_RETRY_SLEEP}s",
                flush=True
            )
            print(
                str(e),
                flush=True
            )
            time.sleep(MODEL_RETRY_SLEEP)

        except APIStatusError as e:
            if e.status_code < 500:
                raise

            print(
                f"[model] API status {e.status_code}, retry "
                f"{attempt}/{MODEL_MAX_RETRIES}; sleeping {MODEL_RETRY_SLEEP}s",
                flush=True
            )
            print(
                str(e),
                flush=True
            )
            time.sleep(MODEL_RETRY_SLEEP)

    raise RuntimeError(
        f"model call failed after {MODEL_MAX_RETRIES} retries"
    )


def execute_tool(call, task_name):

    tool_name = call.function.name

    try:
        args = json.loads(call.function.arguments)
    except Exception as e:

        print(
            f"[{task_name}] failed to parse tool arguments",
            flush=True
        )

        traceback.print_exc()

        return {
            "ok": False,
            "error": str(e)
        }


    # --------------------------------------------------------
    # SHELL
    # --------------------------------------------------------

    if tool_name == "run_shell":

        command = args["command"]

        banner(
            f"[{task_name}] RUN SHELL"
        )

        print(
            "[COMMAND]",
            flush=True
        )

        print(
            command,
            flush=True
        )

        try:

            result = run_shell(
                command
            )

            print(
                "\n[SHELL RESULT]",
                flush=True
            )

            print(
                f"returncode={result['returncode']}",
                flush=True
            )

            return {
                "ok": True,
                "result": result
            }

        except Exception as e:

            print(
                "\n[SHELL ERROR]",
                flush=True
            )

            traceback.print_exc()

            return {
                "ok": False,
                "error": str(e)
            }


    # --------------------------------------------------------
    # PYTHON
    # --------------------------------------------------------

    elif tool_name == "run_python":

        code = args["code"]

        banner(
            f"[{task_name}] RUN PYTHON"
        )

        print(
            "[GENERATED PYTHON CODE BEGIN]",
            flush=True
        )

        print(
            code,
            flush=True
        )

        print(
            "[GENERATED PYTHON CODE END]\n",
            flush=True
        )

        try:

            result = run_python(
                code
            )

            print(
                "\n[PYTHON RESULT]",
                flush=True
            )

            print(
                f"returncode={result['returncode']}",
                flush=True
            )

            return {
                "ok": True,
                "result": result
            }

        except Exception as e:

            print(
                "\n[PYTHON ERROR]",
                flush=True
            )

            print(
                f"{type(e).__name__}: {e}",
                flush=True
            )

            traceback.print_exc()

            return {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)
            }


    else:

        return {
            "ok": False,
            "error": f"Unknown tool: {tool_name}"
        }


# ============================================================
# RUN ONE TASK
# ============================================================

def run_task(task):

    task_name = task["name"]

    output_root = task["output_root"]
    run_id = task["run_id"]

    output_dir = os.path.join(
        output_root,
        run_id
    )

    os.makedirs(
        output_dir,
        exist_ok=True
    )

    banner(
        f"START {task_name}"
    )

    print(
        f"output_root = {output_root}",
        flush=True
    )

    print(
        f"run_id      = {run_id}",
        flush=True
    )

    print(
        f"output_dir  = {output_dir}",
        flush=True
    )


    user_prompt = task["prompt"].format(
        output_root=output_root,
        run_id=run_id
    )


    messages = [

        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },

        {
            "role": "user",
            "content": user_prompt
        }

    ]


    iteration = 0
    max_iterations = 100


    while True:

        iteration += 1

        if iteration > max_iterations:
            raise RuntimeError(
                f"{task_name} exceeded {max_iterations} iterations"
            )


        banner(
            f"[{task_name}] MODEL ITERATION {iteration}"
        )


        response = call_model(messages)


        msg = response.choices[0].message


        # ----------------------------------------------------
        # MODEL FINISHED
        # ----------------------------------------------------

        if not msg.tool_calls:

            banner(
                f"[{task_name}] FINAL RESPONSE"
            )

            print(
                msg.content,
                flush=True
            )

            break


        # ----------------------------------------------------
        # ADD ASSISTANT TOOL CALL MESSAGE
        # ----------------------------------------------------

        messages.append(msg)


        print(
            f"[{task_name}] "
            f"{len(msg.tool_calls)} tool call(s)",
            flush=True
        )


        # ----------------------------------------------------
        # EXECUTE EACH TOOL CALL
        # ----------------------------------------------------

        for call in msg.tool_calls:

            result = execute_tool(
                call,
                task_name
            )


            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": safe_json(result)
                }
            )


    # --------------------------------------------------------
    # OUTPUT CHECK
    # --------------------------------------------------------

    banner(
        f"[{task_name}] OUTPUT CHECK"
    )

    try:

        result = run_shell(
            f"""
echo "Output directory:"
echo "{output_dir}"
echo
find "{output_dir}" -maxdepth 3 -type f -printf '%p\\t%s bytes\\n' 2>/dev/null | sort
"""
        )

        print(
            result,
            flush=True
        )

    except Exception:

        traceback.print_exc()


    banner(
        f"FINISHED {task_name}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    banner(
        "STEP-ONLY AGENT START"
    )

    print(
        f"Start time: {datetime.now().isoformat()}",
        flush=True
    )


    failed_tasks = []

    for task in TASKS:

        try:

            run_task(
                task
            )

        except KeyboardInterrupt:

            print(
                "\nInterrupted.",
                flush=True
            )

            raise

        except Exception as e:

            banner(
                f"FAILED {task['name']}"
            )

            print(
                f"{type(e).__name__}: {e}",
                flush=True
            )

            traceback.print_exc()

            # Continue with T2 even if T1 fails.
            failed_tasks.append(task["name"])
            continue


    if failed_tasks:
        banner(
            "TASKS FINISHED WITH FAILURES"
        )

        print(
            "failed tasks: " + ", ".join(failed_tasks),
            flush=True
        )

        raise SystemExit(1)

    banner(
        "ALL TASKS FINISHED"
    )


if __name__ == "__main__":
    main()
