"""
P1-6 修复：GLM 缓存行为 A/B 测试（精简版）

关键扫描：
1. Prefix 长度对 cache 命中率的影响（500 / 2000 / 5000 chars）
2. Message 数量对 cache 命中率的影响（5 / 20 / 50 msgs）
3. TTL：两次调用间隔 0s / 60s / 300s

每个实验发 2 次相同请求（cold + warm），看 cached_tokens。
精简到 ~20 次调用以控制时间（每次 ~10s）。
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"
def load_api_key():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("ZHIPU_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""

API_KEY = load_api_key()
BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
MODEL = "GLM-4.6"


def make_payload(prefix: str, n_msgs: int) -> dict:
    messages = [{"role": "system", "content": f"[prefix]\n{prefix}\n[/prefix]"}]
    for i in range(n_msgs):
        messages.append({"role": "user", "content": f"Q{i}: tell me a short joke about number {i*7+13}"})
        messages.append({"role": "assistant", "content": f"J{i}: Why did {i*7+13} cross the road? To get to the other side."})
    return {"model": MODEL, "messages": messages, "max_tokens": 16, "stream": False}


def call(payload: dict, timeout: float = 60.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            u = result.get("usage", {})
            return {
                "ok": True,
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "prompt_tokens": u.get("prompt_tokens", 0),
                "cached": u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "err": e.read().decode()[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}


def run(prefix_len: int, n_msgs: int, gap_sec: float) -> dict:
    p = make_payload("x" * prefix_len, n_msgs)
    cold = call(p)
    if cold.get("ok") and gap_sec > 0:
        time.sleep(gap_sec)
    warm = call(p)
    cold_cached = cold.get("cached", 0)
    warm_cached = warm.get("cached", 0)
    warm_prompt = warm.get("prompt_tokens", 0) if warm.get("ok") else 0
    return {
        "prefix_len": prefix_len,
        "n_msgs": n_msgs,
        "gap_sec": gap_sec,
        "cold": cold,
        "warm": warm,
        "warm_cached": warm_cached,
        "warm_prompt": warm_prompt,
        "hit_pct": round((warm_cached / max(warm_prompt, 1)) * 100, 1) if warm.get("ok") else None,
    }


def main():
    print(f"🚀 GLM Cache A/B Test (精简版) - {datetime.now().isoformat()}")
    print(f"Model: {MODEL}")
    results = []

    # 扫描 1: prefix 长度影响（gap=0, n_msgs=10）
    print("\n=== 扫描 1: Prefix 长度 ===")
    for prefix_len in [500, 2000, 5000]:
        r = run(prefix_len, 10, 0)
        results.append(r)
        if r["hit_pct"] is not None:
            print(f"  prefix={prefix_len:>5} → cold_cached={r['cold'].get('cached', 0):>3} "
                  f"warm_cached={r['warm_cached']:>4} hit={r['hit_pct']:>5.1f}% "
                  f"prompt={r['warm_prompt']}")
        else:
            print(f"  prefix={prefix_len:>5} → FAIL: {r['warm']}")

    # 扫描 2: message 数量影响（prefix=1000, gap=0）
    print("\n=== 扫描 2: Message 数量 ===")
    for n_msgs in [5, 20, 50]:
        r = run(1000, n_msgs, 0)
        results.append(r)
        if r["hit_pct"] is not None:
            print(f"  n_msgs={n_msgs:>3} → cold_cached={r['cold'].get('cached', 0):>3} "
                  f"warm_cached={r['warm_cached']:>4} hit={r['hit_pct']:>5.1f}% "
                  f"prompt={r['warm_prompt']}")
        else:
            print(f"  n_msgs={n_msgs:>3} → FAIL: {r['warm']}")

    # 扫描 3: TTL 间隔（prefix=2000, n_msgs=20）
    print("\n=== 扫描 3: TTL 间隔 ===")
    for gap in [0, 60, 300]:
        r = run(2000, 20, gap)
        results.append(r)
        if r["hit_pct"] is not None:
            print(f"  gap={gap:>3}s → cold_cached={r['cold'].get('cached', 0):>3} "
                  f"warm_cached={r['warm_cached']:>4} hit={r['hit_pct']:>5.1f}% "
                  f"prompt={r['warm_prompt']}")
        else:
            print(f"  gap={gap:>3}s → FAIL: {r['warm']}")

    out = Path(__file__).parent / "glm_cache_abtest_results.json"
    out.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "model": MODEL,
        "n_experiments": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 报告: {out}")


if __name__ == "__main__":
    main()
