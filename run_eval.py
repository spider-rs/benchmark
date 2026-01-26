"""Main benchmark evaluation script."""

import base64, hashlib, json
from pathlib import Path
from cryptography.fernet import Fernet

TASKS_FILE = Path(__file__).parent / "BU_Bench_V1.enc"


def load_tasks() -> list[dict]:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    encrypted = base64.b64decode(TASKS_FILE.read_text())
    return json.loads(Fernet(key).decrypt(encrypted))


def main():
    tasks = load_tasks()

    # Debug: print first 3 tasks - remove later
    print(f"Loaded {len(tasks)} tasks")
    for task in tasks[:3]:
        print("-" * 60)
        print(json.dumps(task, indent=2))


if __name__ == "__main__":
    main()
