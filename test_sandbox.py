import sys
import os
import io
import time
import json
import traceback
import builtins
import multiprocessing
from typing import List, Dict, Any
from pydantic import BaseModel, Field


class SandboxConfig(BaseModel):
    authorized_imports: List[str] = Field(default_factory=lambda: [
        "math", "math.*",
        "collections", "collections.*",
        "itertools", "re", "json",
        "typing", "typing.*",
        "functools", "operator",
        "heapq", "bisect", "copy",
        "string", "random",
        "datetime", "datetime.*",
        "array", "cmath",
        "ast", "ast.*",
        "pathlib", "pathlib.*",
        "io", "io.*",
        "hashlib", "base64", "uuid",
        "graphlib", "statistics", "csv", "pickle",
        "textwrap", "difflib", "pprint", "time",
    ])
    allowed_directories: List[str] = Field(default_factory=lambda: [
        "/testbed", "/tmp/agent"
    ])
    max_execution_time_seconds: int = 30
    max_memory_mb: int = 512


class FinalAnswerException(BaseException):
    def __init__(self, answer: str):
        self.answer = answer
        super().__init__(answer)


def _is_import_allowed(name: str, authorized_imports: List[str]) -> bool:
    for pattern in authorized_imports:
        if pattern.endswith(".*"):
            base = pattern[:-2]
            if name == base or name.startswith(base + "."):
                return True
        elif name == pattern:
            return True
    return False


def _is_path_allowed(path_str: str, allowed_directories: List[str]) -> bool:
    filepath = os.path.abspath(path_str)
    if filepath.startswith(sys.prefix) or filepath.endswith(".py"):
        return True
    return any(filepath.startswith(os.path.abspath(d)) for d in allowed_directories)


def _secure_worker(code: str, config_dict: dict, pipe_conn, mcp_tools_manual: dict):
    authorized_imports = config_dict.get("authorized_imports", [])
    allowed_directories = config_dict.get("allowed_directories", [])

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    sys.stdout = stdout_capture
    sys.stderr = stderr_capture

    exec_globals: Dict[str, Any] = {}

    def final_answer(answer_string: str):
        raise FinalAnswerException(answer_string)

    exec_globals["final_answer"] = final_answer

    def create_mcp_wrapper(tool_name: str):
        def wrapper(*args, **kwargs):
            sys.stdout = sys.__stdout__
            pipe_conn.send({"type": "mcp_call", "name": tool_name, "args": args, "kwargs": kwargs})
            response = pipe_conn.recv()
            sys.stdout = stdout_capture
            if response.get("status") == "error":
                raise RuntimeError(response["error"])
            return response.get("result")
        return wrapper

    for tool_name in mcp_tools_manual:
        exec_globals[tool_name] = create_mcp_wrapper(tool_name)

    original_import = builtins.__import__

    def secure_import(name, globals=None, locals=None, fromlist=None, level=0):
        if not _is_import_allowed(name, authorized_imports):
            raise ImportError(f"Permission Denied: Module '{name}' is blocked by sandbox policy.")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = secure_import

    def audit_hook(event: str, args):
        if event in ("open", "os.open"):
            if not args or not isinstance(args[0], (str, bytes)):
                return
            path_str = args[0].decode() if isinstance(args[0], bytes) else args[0]
            if not _is_path_allowed(path_str, allowed_directories):
                raise PermissionError(f"Permission Denied: Access to path '{path_str}' is restricted.")

        if event in ("socket.connect", "socket.bind", "os.system", "subprocess.Popen"):
            raise PermissionError(f"Permission Denied: Operation '{event}' is forbidden in this sandbox.")

    sys.addaudithook(audit_hook)

    orig_open = builtins.open

    def restricted_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes)):
            path_str = file.decode() if isinstance(file, bytes) else file
            if not _is_path_allowed(path_str, allowed_directories):
                raise PermissionError(f"Permission Denied: Cannot open '{path_str}'.")
        return orig_open(file, *args, **kwargs)

    builtins.open = restricted_open

    exit_code = 0
    is_final = False
    final_ans_value = ""

    try:
        compiled_code = compile(code, "<sandbox_execution>", "exec")
        exec(compiled_code, exec_globals)
    except FinalAnswerException as fa:
        is_final = True
        final_ans_value = fa.answer
    except (KeyboardInterrupt, SystemExit):
        pipe_conn.send({"type": "propagate_shutdown"})
        return
    except Exception:
        exit_code = 1
        traceback.print_exc(file=sys.stderr)

    pipe_conn.send({
        "type": "execution_result",
        "stdout": stdout_capture.getvalue(),
        "stderr": stderr_capture.getvalue(),
        "exit_code": exit_code,
        "is_final": is_final,
        "final_answer": final_ans_value,
    })


def _default_mcp_tools() -> dict:
    return {tool: {} for tool in (
        "read_file", "edit_file", "list_files",
        "search_code", "search_function_or_class_definition_in_code",
        "find_references", "run_tests", "get_patch", "run_command",
    )}


def _dispatch_mcp_call(tool_name: str, args, kwargs, mcp_server_client) -> Any:
    if tool_name == "run_tests":
        return "All tests passed (mock execution)."
    return f"Success calling tool {tool_name}."


def execute_in_sandbox(code_to_run: str, config: SandboxConfig, mcp_server_client=None) -> dict:
    if not code_to_run or not code_to_run.strip():
        return {
            "success": False,
            "stdout": "",
            "stderr": "Error: No valid code block was found in the model's response.",
            "exit_code": -1,
            "is_final": False,
            "final_answer": "",
        }

    mcp_tools_manual = (
        mcp_server_client.tools
        if mcp_server_client and hasattr(mcp_server_client, "tools")
        else _default_mcp_tools()
    )

    parent_conn, child_conn = multiprocessing.Pipe()
    config_dict = config.model_dump()

    worker_process = multiprocessing.Process(
        target=_secure_worker,
        args=(code_to_run, config_dict, child_conn, mcp_tools_manual),
    )

    start_time = time.time()
    worker_process.start()

    stdout, stderr = "", ""
    exit_code = -1
    is_final = False
    final_answer_str = ""

    try:
        while worker_process.is_alive():
            if time.time() - start_time > config.max_execution_time_seconds:
                worker_process.terminate()
                return {
                    "success": False,
                    "stdout": stdout,
                    "stderr": f"Error: Execution hit the timeout of {config.max_execution_time_seconds} seconds.",
                    "exit_code": -1,
                    "is_final": False,
                    "final_answer": "",
                }

            if not parent_conn.poll(0.1):
                continue

            msg = parent_conn.recv()

            if msg["type"] == "propagate_shutdown":
                worker_process.join()
                raise KeyboardInterrupt("System shutdown requested from within sandbox.")

            elif msg["type"] == "mcp_call":
                try:
                    result = _dispatch_mcp_call(
                        msg["name"], msg["args"], msg["kwargs"], mcp_server_client
                    )
                    parent_conn.send({"status": "success", "result": result})
                except Exception as mcp_err:
                    parent_conn.send({"status": "error", "error": str(mcp_err)})

            elif msg["type"] == "execution_result":
                stdout = msg["stdout"]
                stderr = msg["stderr"]
                exit_code = msg["exit_code"]
                is_final = msg["is_final"]
                final_answer_str = msg["final_answer"]
                break

    finally:
        if worker_process.is_alive():
            worker_process.terminate()
        worker_process.join()

    return {
        "success": exit_code == 0,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "is_final": is_final,
        "final_answer": final_answer_str,
    }


def _load_config(path: str) -> SandboxConfig:
    try:
        with open(path) as f:
            return SandboxConfig(**json.load(f))
    except Exception as e:
        print(f"[!] Config error, using defaults: {e}")
        return SandboxConfig()


def main_cli():
    config = SandboxConfig()

    if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
        if os.path.exists(sys.argv[1]):
            config = _load_config(sys.argv[1])
            print(f"[*] Config loaded from {sys.argv[1]}")

    print(f"=== Sandbox CLI (Python {sys.version_info.major}.{sys.version_info.minor}) ===")
    print("[*] Enter Python code below. End with Ctrl+D.")
    print("-" * 60)

    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass

    res = execute_in_sandbox("\n".join(lines), config)

    print(f"\n{'=' * 20} RESULT {'=' * 20}")
    print(f"Exit Code          : {res['exit_code']}")
    print(f"Success            : {res['success']}")
    print(f"Final Answer Called: {res['is_final']} (Value: '{res['final_answer']}')")
    print(f"\n[STDOUT]:\n{res['stdout']}")
    print(f"\n[STDERR]:\n{res['stderr']}")
    print("=" * 49)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main_cli()