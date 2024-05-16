# Adapted from: https://github.com/openai/human-eval/blob/master/human_eval/evaluation.py

import itertools
import math
import os
import subprocess
import psutil
from collections import defaultdict
from typing import Dict, List, Union

import numpy as np
import tqdm
from loguru import logger
from utils import cleanup_file, cwpu

from data import HUMAN_EVAL, read_problems, stream_jsonl, write_jsonl

count = 0
class ParseError(Exception):
    pass


def find_index_or_last(lst, k):
    try:
        return lst.index(k)
    except ValueError:
        return len(lst) - 1


def parse(result, result_type, true):
    """
    Parse COBOL value according to Python type
    """
    try:
        match result_type:
            case "Bool":
                return parse_bool(result[0])

            case "Int":
                return parse_int(result[0])

            case "Float":
                return parse_float(result[0])

            case "String":
                return parse_string(result[0])

            case {"List": "Int"}:
                parsed_result = [parse_int(x) for x in result]
                return parsed_result[: len(true)]

            case {"List": "Float"}:
                parsed_result = [parse_float(x) for x in result]
                return parsed_result[: len(true)]

            case {"List": "String"}:
                parsed_result = [parse_string(x) for x in result]
                return parsed_result[: len(true)]

            case _:
                raise ParseError("Invalid result type: ", result_type)
    except Exception as e:
        raise ParseError(f"Result {result} of type {result_type} failed with: {e}")


def parse_bool(res: str) -> bool:
    if res.strip() == "1":
        return True
    return False


def parse_int(res: str) -> int:
    res = res.strip()
    if res.startswith("p") or res.startswith("y"):
        return -int(res[1:])
    return int(res)


def parse_float(res: str) -> float:
    res = res.strip()
    if res.startswith("p") or res.startswith("y"):
        res = res[1:]
        return -float(res)
    return float(res)


def parse_string(res: str) -> str:
    return res.strip()


def is_equal(result_type, result, true):
    match result_type:
        case "Float":
            return math.isclose(result, true, abs_tol=0.001)
        case {"List": "Float"}:
            return all(math.isclose(r, t, abs_tol=0.001) for r, t in zip(result, true))
        case _:
            return result == true


def cmd(command, timeout=None):
    """
    Executes a shell command with an optional timeout and ensures complete termination of all subprocesses.
    """
    # Start the subprocess
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode != 0:
            logger.warning(f"Command failed with error: {stderr.decode()}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"Command timed out: {command}")
        # Use psutil to kill all child processes as well
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        return False


def exec(name, path, call_path, timeout=30) -> bool:
    """
    Compile and execute a COBOL program with a timeout.
    """
    # Convert Windows path to WSL path
    wsl_path = path.replace("C:\\", "/mnt/c/").replace("\\", "/")
    wsl_call_path = call_path.replace("C:\\", "/mnt/c/").replace("\\", "/")

    if not cmd(f"wsl cobc -w -fformat=variable -x {wsl_call_path} {wsl_path}", timeout=timeout):
        logger.warning(f"Compile error for {path}")
        return False

    # Uncomment and modify the following lines to execute the program using WSL
    if not cmd(f"wsl ./call_{name}", timeout=timeout):
        logger.warning(f"Runtime error for {path}")
        return False

    return True


def check_correctness(problem: Dict, completion: str, base_path: str) -> Dict:
    """
    Check the correctness of a single completion
    """
    name, tests = problem["entry_point"], problem["tests"]
    global count
    count += 1
    print(count)
    path, call_path = (
        f"{base_path}/solutions/{name}.cbl",
        f"{base_path}/callers/call_{name}.cbl"
    )
    result_path = f"{name.upper().replace('_', '-')}.TXT"
    os.makedirs(os.path.dirname(cwpu(path)), exist_ok=True)
    os.makedirs(os.path.dirname(cwpu(call_path)), exist_ok=True)

    with open(path, "w", encoding='utf-8') as f:  # Specify UTF-8 encoding here
        f.write(completion)

    passed, trues, results, compiled = [], [], [], []
    for test in tests:
        true = eval(test["result"]["value"])
        if isinstance(true, tuple):  # convert tuples to list
            true = list(true)

        trues.append(true)
        passed.append(False)
        results.append(None)
        compiled.append(False)

        with open(call_path, "w", encoding='utf-8') as f:  # Specify UTF-8 encoding here
            f.write(test["test"])

        try:
            if exec(name, path, call_path):
                compiled[-1] = True

                with open(result_path, encoding='utf-8') as f:  # Specify UTF-8 encoding here
                
                    result = f.readlines()

                if result:
                    type_ = test["result"]["type_"]
                    parsed_result = parse(result, type_, true)
                    passed[-1] = is_equal(type_, parsed_result, true)
                    results[-1] = parsed_result
        except Exception as e:
            logger.error(f"Eval {name} failed with: {e}")
        finally:
            cleanup_file(f"call_{name}")
            cleanup_file(result_path)

    return {
        "all_passed": all(passed),
        "passed": passed,
        "results": results,
        "trues": trues,
        "compiled": compiled,
    }


def estimate_pass_at_k(
    num_samples: Union[int, List[int], np.ndarray],
    num_correct: Union[List[int], np.ndarray],
    k: int,
) -> np.ndarray:
    """
    Estimates pass@k of each problem and returns them in an array.
    """

    def estimator(n: int, c: int, k: int) -> float:
        """
        Calculates 1 - comb(n - c, k) / comb(n, k).
        """
        if n - c < k:
            return 1.0
        return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))

    if isinstance(num_samples, int):
        num_samples_it = itertools.repeat(num_samples, len(num_correct))
    else:
        assert len(num_samples) == len(num_correct)
        num_samples_it = iter(num_samples)

    return np.array(
        [estimator(int(n), int(c), k) for n, c in zip(num_samples_it, num_correct)]
    )


def evaluate_functional_correctness(
    base_path: str,
    k: List[int] = [1, 10, 100],
    problem_file: str = HUMAN_EVAL,
):
    """
    Evaluates the functional correctness of generated samples, and writes
    results to f"{sample_file}_results.jsonl.gz"
    """
    problems = read_problems(problem_file)
    print(problems)
    print(len(problems))
    print("ok")

    sample_file = f"{base_path}/samples.jsonl"
    solutions_path = f"{base_path}/solutions"
    calls_path = f"{base_path}/callers"
    os.makedirs(solutions_path, exist_ok=True)
    os.makedirs(calls_path, exist_ok=True)

    n_samples = 0
    results = defaultdict(list)

    logger.info("Reading samples...")
    for sample in list(stream_jsonl(sample_file)):
        id_, task_id, completion = (
            sample["sample_id"],
            sample["task_id"],
            sample["completion"],
        )
        correct = check_correctness(problems[task_id], completion, base_path)

        n_samples += 1
        results[task_id].append((id_, correct))

    # Calculate pass@k.
    total, correct = [], []
    for result in results.values():
        result.sort()
        passed = [r[1]["all_passed"] for r in result]
        total.append(len(passed))
        correct.append(sum(passed))
    total = np.array(total)
    correct = np.array(correct)

    ks = k
    pass_at_k = {
        f"pass@{k}": estimate_pass_at_k(total, correct, k).mean()
        for k in ks
        if (total >= k).all()
    }

    total = 0
    passed = 0
    compiled = 0

    for result in results.values():
        for r in result:
            total += len(r[1]["passed"])
            passed += sum(r[1]["passed"])
            compiled += sum(r[1]["compiled"])

    logger.info(f"Total tests: {total}, Passed: {passed}, Compiled: {compiled}")

    # Finally, save the results in one file:
    def combine_results():
        for sample in stream_jsonl(sample_file):
            task_id = sample["task_id"]
            result = results[task_id].pop(0)
            sample["trues"] = result[1]["trues"]
            sample["passed"] = result[1]["passed"]
            sample["results"] = result[1]["results"]
            sample["compiled"] = result[1]["compiled"]
            sample["all_passed"] = result[1]["all_passed"]
            yield sample

    out_file = sample_file + "_results.jsonl"
    logger.info(f"Writing results to {out_file}...")
    write_jsonl(out_file, tqdm.tqdm(combine_results(), total=n_samples))

    return pass_at_k
