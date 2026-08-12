import os
import queue
import signal
import subprocess
import threading
import time


WORKSPACE = "/ssd/deecamp/cellnotes/step_only_workspace"

PYTHON_EXEC = "/ssd/deecamp/cellnotes/micromamba/envs/snapatac2/bin/python"

SHELL_TIMEOUT = int(os.environ.get("STEP_ONLY_SHELL_TIMEOUT", "7200"))
SHELL_QUIET_TIMEOUT = int(os.environ.get("STEP_ONLY_SHELL_QUIET_TIMEOUT", "600"))
PYTHON_TIMEOUT = int(os.environ.get("STEP_ONLY_PYTHON_TIMEOUT", "43200"))
PYTHON_QUIET_TIMEOUT = int(os.environ.get("STEP_ONLY_PYTHON_QUIET_TIMEOUT", "1800"))
HEARTBEAT_SECONDS = int(os.environ.get("STEP_ONLY_HEARTBEAT_SECONDS", "300"))
FATAL_OUTPUT_PATTERNS = [
    "Please sort fragment file by barcodes",
    "pyo3_runtime.PanicException",
    "panicked at snapatac2-core",
]


ENV = {
    **os.environ,
    "PYTHONUNBUFFERED": "1"
}


def _drain_queue(q, sentinel, chunks, process, reader):
    deadline = time.monotonic() + 5

    while time.monotonic() < deadline:
        try:
            item = q.get(timeout=0.2)
        except queue.Empty:
            if not reader.is_alive():
                break
            continue

        if item is sentinel:
            break

        chunks.append(item)
        print(item, end="", flush=True)

    reader.join(timeout=1)

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _kill_process(process, q, sentinel, chunks, reader):
    try:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
    except ProcessLookupError:
        pass
    finally:
        _drain_queue(
            q,
            sentinel,
            chunks,
            process,
            reader
        )


def _run_streaming(command, *, shell, cwd, env, timeout, quiet_timeout, label):
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=(os.name == "posix")
    )

    stdout_chunks = []
    q = queue.Queue()
    sentinel = object()

    def pump():
        try:
            if process.stdout is None:
                return

            for line in iter(process.stdout.readline, ""):
                q.put(line)
        finally:
            q.put(sentinel)

    reader = threading.Thread(
        target=pump,
        daemon=True
    )
    reader.start()

    start = time.monotonic()
    last_output = start
    next_heartbeat = start + HEARTBEAT_SECONDS

    while True:
        now = time.monotonic()
        elapsed = now - start

        if timeout is not None and elapsed >= timeout:
            print(
                f"[{label}] timed out after {timeout}s; terminating pid={process.pid}",
                flush=True
            )
            _kill_process(
                process,
                q,
                sentinel,
                stdout_chunks,
                reader
            )
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output="".join(stdout_chunks)
            )

        wait_time = min(
            1.0,
            max(0.1, timeout - elapsed) if timeout is not None else 1.0
        )

        try:
            item = q.get(timeout=wait_time)
        except queue.Empty:
            if process.poll() is not None and not reader.is_alive():
                break

            now = time.monotonic()

            if quiet_timeout is not None and now - last_output >= quiet_timeout:
                print(
                    f"[{label}] no output for {quiet_timeout}s; terminating pid={process.pid}",
                    flush=True
                )
                _kill_process(
                    process,
                    q,
                    sentinel,
                    stdout_chunks,
                    reader
                )
                raise TimeoutError(
                    f"{label} produced no output for {quiet_timeout} seconds"
                ) from None

            if now >= next_heartbeat:
                print(
                    f"[{label}] still running (pid={process.pid})",
                    flush=True
                )
                next_heartbeat = now + HEARTBEAT_SECONDS

            continue

        if item is sentinel:
            if process.poll() is not None:
                break
            continue

        stdout_chunks.append(item)
        print(item, end="", flush=True)
        last_output = time.monotonic()

        for pattern in FATAL_OUTPUT_PATTERNS:
            if pattern in item:
                print(
                    f"[{label}] fatal output pattern detected: {pattern}",
                    flush=True
                )
                _kill_process(
                    process,
                    q,
                    sentinel,
                    stdout_chunks,
                    reader
                )
                raise RuntimeError(
                    f"{label} failed: {pattern}\n\n"
                    + "".join(stdout_chunks)
                )

    reader.join(timeout=1)

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass

    return {
        "stdout": "".join(stdout_chunks),
        "stderr": "",
        "returncode": process.returncode
    }


def run_shell(command):
    return _run_streaming(
        command,
        shell=True,
        cwd=WORKSPACE,
        env=ENV,
        timeout=SHELL_TIMEOUT,
        quiet_timeout=SHELL_QUIET_TIMEOUT,
        label="shell"
    )


def run_python(code, timeout=PYTHON_TIMEOUT):
    """
    Run generated python code with live output.
    """

    print("=" * 60, flush=True)
    print("[Agent] Starting python subprocess", flush=True)
    print(f"[Agent] Timeout: {timeout}s", flush=True)
    print("=" * 60, flush=True)

    result = _run_streaming(
        [
            PYTHON_EXEC,
            "-u",
            "-c",
            code
        ],
        shell=False,
        cwd=WORKSPACE,
        env=ENV,
        timeout=timeout,
        quiet_timeout=PYTHON_QUIET_TIMEOUT,
        label="python"
    )

    print("=" * 60, flush=True)
    print("[Agent] Python subprocess finished", flush=True)
    print(f"[Agent] returncode={result['returncode']}", flush=True)
    print("=" * 60, flush=True)

    if result["returncode"] != 0:
        raise RuntimeError(
            "Python subprocess failed with return code "
            f"{result['returncode']}\n\n{result['stdout']}"
        )

    return result
