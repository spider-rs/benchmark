"""Main benchmark evaluation script."""

# Fix for MacOS users using uv without SSL certificate setup
import certifi, os
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import logging
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"  # Must be set before importing browser_use
logging.basicConfig(level=logging.CRITICAL)  # Suppress all logs including shutdown warnings

import asyncio
import base64, hashlib, json, traceback
from datetime import datetime
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from browser_use import Agent, Browser, ChatGoogle
from browser_use.llm import ChatBrowserUse
from judge import construct_judge_messages, JudgementResult

load_dotenv()

# Judge LLM - always use gemini-2.5-flash for consistent judging across all evaluations
JUDGE_LLM = ChatGoogle(model="gemini-2.5-flash", api_key=os.getenv("GOOGLE_API_KEY"))
TASKS_FILE = Path(__file__).parent / "BU_Bench_V1.enc"
MAX_CONCURRENT = 3
TASK_TIMEOUT = 1800 # 30 minutes max per task

# Run parameters (hardcoded for this evaluation)
BROWSER_NAME = "BrowserUseCloud"
AGENT_FRAMEWORK_NAME = "BrowserUse"
AGENT_FRAMEWORK_VERSION = "0.11.5"
MODEL_NAME = "ChatBrowserUse"

# Run naming
RUN_START = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_KEY = f"{AGENT_FRAMEWORK_NAME}_{AGENT_FRAMEWORK_VERSION}_browser_{BROWSER_NAME}_model_{MODEL_NAME}"
RUN_DATA_DIR = Path(__file__).parent / "run_data" / f"{RUN_KEY}_start_at_{RUN_START}"
RESULTS_FILE = Path(__file__).parent / "results" / f"{RUN_KEY}.json"


def encode_screenshots(paths: list[str]) -> list[str]:
    """Encode screenshot files to base64. Skips files that don't exist."""
    result = []
    for p in paths:
        path = Path(p)
        if path.exists():
            result.append(base64.b64encode(path.read_bytes()).decode())
    return result


def load_tasks() -> list[dict]:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    encrypted = base64.b64decode(TASKS_FILE.read_text())
    return json.loads(Fernet(key).decrypt(encrypted))


async def run_task(task: dict, semaphore: asyncio.Semaphore, llm=None, run_data_dir: Path = None) -> dict:
    """Run a single task. Returns result dict with score (0 on failure).
    
    Args:
        llm: LLM to use. Defaults to ChatBrowserUse().
        run_data_dir: Directory for trace output. Defaults to RUN_DATA_DIR.
    """
    run_data_dir = run_data_dir or RUN_DATA_DIR
    async with semaphore:
        try:
            task_id = task.get("task_id", "unknown")
            print(f"Running task: {task_id}")

            # To swap browser: replace with Browser(cdp_url=...) for other providers
            browser = Browser(use_cloud=True, cloud_timeout=30)

            # To swap model: replace ChatBrowserUse() with your LLM (e.g. ChatOpenAI, ChatAnthropic)
            # You can use any OpenAI API compatible model by changing base_url. You can use ollama too. See https://docs.browser-use.com/supported-models for info
            agent = Agent(task=task["confirmed_task"], llm=llm or ChatBrowserUse(), browser=browser)
            
            try:
                agent_history = await asyncio.wait_for(agent.run(), timeout=TASK_TIMEOUT)
            except asyncio.TimeoutError:
                await browser.stop()
                print(f"Task {task_id} timed out after {TASK_TIMEOUT}s")
                return {"task_id": task_id, "score": 0, "steps": 0, "duration": TASK_TIMEOUT, "cost": 0, "error": f"Task timed out after {TASK_TIMEOUT}s"}

            # Collect task metrics from agent history
            steps = agent_history.number_of_steps()
            duration = agent_history.total_duration_seconds()
            cost = agent_history.usage.total_cost if agent_history.usage else 0

            # Collect judge inputs from agent history
            agent_task = task["confirmed_task"]
            final_result = agent_history.final_result() or "Agent did not return a result"
            agent_steps = agent_history.agent_steps()
            ground_truth = task.get("answer")
            screenshots_b64 = encode_screenshots([p for p in agent_history.screenshot_paths() if p is not None])

            # Run judge
            judge_messages = construct_judge_messages(task=agent_task, final_result=final_result, agent_steps=agent_steps, ground_truth=ground_truth, screenshots_b64=screenshots_b64)
            response = await JUDGE_LLM.ainvoke(judge_messages, output_format=JudgementResult)
            judgement: JudgementResult = response.completion

            score = 1 if judgement.verdict else 0
            print(f"Task {task_id} completed: score={score}, verdict={judgement.verdict}")

            # Save trace to run_data/
            run_data_dir.mkdir(parents=True, exist_ok=True)
            trace = {"agent_task": agent_task, "final_result": final_result, "agent_steps": agent_steps, "ground_truth": ground_truth, "screenshots_b64": screenshots_b64}
            metrics = {"steps": steps, "duration": duration, "cost": cost}
            (run_data_dir / f"{task_id}.json").write_text(json.dumps({"agent_trace": trace, "metrics": metrics, "judgement": judgement.model_dump()}, indent=2))

            return {"task_id": task_id, "score": score, "steps": steps, "duration": duration, "cost": cost, "judgement": judgement.model_dump()}

        except Exception as e: # Catch any exception that occurs during task execution
            error_type = type(e).__name__
            error_msg = f"{error_type}: {e}"
            print(f"Task {task.get('task_id', 'unknown')} failed: {error_msg}")
            return {"task_id": task.get("task_id"), "score": 0, "steps": 0, "duration": 0, "cost": 0, "error": error_msg, "traceback": traceback.format_exc()}


async def main():
    tasks, sem = load_tasks(), asyncio.Semaphore(MAX_CONCURRENT) # First 10 tasks only for now
    results = await asyncio.gather(*[run_task(t, sem) for t in tasks])
    
    # Aggregate metrics
    successful = sum(1 for r in results if r.get("score") == 1)
    total_steps = sum(r.get("steps", 0) for r in results)
    total_duration = sum(r.get("duration", 0) for r in results)
    total_cost = sum(r.get("cost", 0) for r in results)

    # Save to official_results (append to existing runs)
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    runs = json.loads(RESULTS_FILE.read_text()) if RESULTS_FILE.exists() else []
    runs.append({"run_start": RUN_START, "tasks_completed": len(results), "tasks_successful": successful, "total_steps": total_steps, "total_duration": total_duration, "total_cost": total_cost})
    RESULTS_FILE.write_text(json.dumps(runs, indent=2))

    print(f"Run complete: {successful}/{len(results)} tasks successful, {total_steps} steps, {total_duration:.1f}s, ${total_cost:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
