import subprocess
import sys
from typing import List
from pathlib import Path
import datetime

def run_script(script_name: str, logs_dir: Path) -> bool:
    """
    Executes a Python script using the current Python interpreter and logs output to a file.

    Args:
        script_name (str): The name of the script to run.
        logs_dir (Path): Path to the directory where logs should be saved.

    Returns:
        bool: True if the script runs successfully, False otherwise.
    """
    print(f"Running {script_name}...")

    # Prepare log file path (one log file per script run, timestamped)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"{script_name}_{timestamp}.log"

    try:
        # Base command
        cmd = [sys.executable, script_name]

        # Special case: master_cleanup.py must always be run with --yes
        if script_name == "master_cleanup.py":
            cmd.append("--yes")

        # Open the log file and direct both stdout and stderr into it
        with open(log_file, "w", encoding="utf-8") as f:
            process = subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,  # Merge stderr into the same log file
                text=True,
                check=True
            )

        print(f"{script_name} completed successfully. Logs: {log_file}")
        return True

    except subprocess.CalledProcessError:
        print(f"Error running {script_name}. Check logs: {log_file}")
        return False


def main():
    """
    Main function to run a list of Python scripts sequentially.
    """
    # Ensure logs directory exists
    logs_dir = Path("LOGS")
    logs_dir.mkdir(exist_ok=True)

    scripts: List[str] = [
        "master_cleanup.py",
        "netbox_manufacturer.py",
        "netbox_rack.py",
        "netbox_device.py",
        "netbox_module.py"
    ]

    for script in scripts:
        if not run_script(script, logs_dir):
            print(f"Execution stopped due to an error in {script}")
            break
    else:
        print("All scripts executed successfully.")


if __name__ == "__main__":
    main()
