import os
import subprocess
from dataclasses import dataclass

from loguru import logger


@dataclass
class Model:
    name: str
    temp: float = 0.0
    samples_per_task: int = 1
    tokenizer: str = None
    prefix_token: str = None
    suffix_token: str = None
    middle_token: str = None
    eos_token: str = None


def cmd(cmd: str) -> bool:
    process = subprocess.run(
        cmd,
        shell=True,
        text=True,
        capture_output=True,
        timeout=15,
    )

    if process.stderr:
        logger.warning(f"Err: {process.stderr}")
        logger.warning(f"Return code: {process.returncode}")
    return process.returncode == 0


def cleanup_dylib(name: str):
    try:
        os.remove(f"{name}.dylib")
    except FileNotFoundError:
        logger.warning(f"File {name}.dylib not found")


def cleanup_file(name: str):
    try:
        os.remove(f"{name}")
    except FileNotFoundError:
        logger.warning(f"Exe {name} not found")

def cwpu(path: str) -> str:
    """
    Convert a Windows file path to a Unix file path.

    Args:
    path (str): The Windows file path to convert.

    Returns:
    str: The converted Unix file path.
    """
    return path.replace('C:', '/mnt/c')
