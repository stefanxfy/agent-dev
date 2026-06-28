"""Read debug expander from failed test page state."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright

def main():
    # Read the latest failed screenshot via Playwright by recreating the state
    # Just look at the test artifacts dir for trace files
    trace_dir = Path(__file__).resolve().parent / "reports" / "traces"
    print(f"Looking for traces in {trace_dir}")
    for p in sorted(trace_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[:2]:
        print(f"Trace: {p}")

if __name__ == "__main__":
    main()