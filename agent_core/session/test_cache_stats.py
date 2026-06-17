"""
Cache Stats Sidecar 测试
覆盖 SessionStorage.read_cache_stats / write_cache_stats / list_sessions 集成

不依赖 pytest，使用纯 unittest 风格 + __main__ 入口
"""
import sys
import os
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, "/Users/fanyunxu/Desktop/myproject/agent-dev")

from agent_core.session.storage import SessionStorage
from agent_core.session.manager import SessionManager


class _MockUsage:
    """模拟 UsageStats：只需要 cached_tokens 和 input_tokens"""
    def __init__(self, cached=0, input_tokens=0):
        self.cached_tokens = cached
        self.input_tokens = input_tokens


def _ok(name, got, expected):
    """极简断言"""
    if got == expected:
        print(f"  ✅ {name}: {got}")
        return True
    else:
        print(f"  ❌ {name}: got={got}, expected={expected}")
        return False


def test_read_empty(tmpdir):
    """读取不存在的 sidecar → 全 0"""
    print("\n🧪 test_read_empty")
    p = Path(tmpdir) / "abc.jsonl"
    p.touch()  # 只有 jsonl，没 sidecar
    s = SessionStorage.read_cache_stats(p)
    ok = True
    ok &= _ok("cached_tokens=0", s["cached_tokens"], 0)
    ok &= _ok("input_tokens=0", s["input_tokens"], 0)
    ok &= _ok("total_calls=0", s["total_calls"], 0)
    ok &= _ok("hit_rate=0.0", s["hit_rate"], 0.0)
    return ok


def test_write_then_read(tmpdir):
    """写入一次后读取"""
    print("\n🧪 test_write_then_read")
    p = Path(tmpdir) / "abc.jsonl"
    p.touch()
    SessionStorage.write_cache_stats(p, _MockUsage(cached=100, input_tokens=1000))
    s = SessionStorage.read_cache_stats(p)
    ok = True
    ok &= _ok("cached_tokens=100", s["cached_tokens"], 100)
    ok &= _ok("input_tokens=1000", s["input_tokens"], 1000)
    ok &= _ok("total_calls=1", s["total_calls"], 1)
    ok &= _ok("hit_rate=0.1", round(s["hit_rate"], 4), 0.1)
    ok &= _ok("last_updated exists", s.get("last_updated") is not None, True)
    return ok


def test_accumulate(tmpdir):
    """多次写入应累加（不依赖 SessionManager，避免 LLM 标题生成的额外调用）"""
    print("\n🧪 test_accumulate")
    p = Path(tmpdir) / "abc.jsonl"
    p.touch()
    # 第 1 次：建 cache，cached=0
    SessionStorage.write_cache_stats(p, _MockUsage(cached=0, input_tokens=20000))
    # 第 2 次：99% 命中
    SessionStorage.write_cache_stats(p, _MockUsage(cached=18112, input_tokens=18129))
    # 第 3 次：99% 命中
    SessionStorage.write_cache_stats(p, _MockUsage(cached=9000, input_tokens=9005))
    s = SessionStorage.read_cache_stats(p)
    ok = True
    ok &= _ok("total_calls=3", s["total_calls"], 3)
    ok &= _ok("input_tokens=20000+18129+9005", s["input_tokens"], 20000+18129+9005)
    ok &= _ok("cached_tokens=0+18112+9000", s["cached_tokens"], 0+18112+9000)
    expected_hit = (0+18112+9000) / (20000+18129+9005)
    ok &= _ok(f"hit_rate≈{expected_hit:.4f}", round(s["hit_rate"], 4), round(expected_hit, 4))
    return ok


def test_delete_removes_sidecar(tmpdir):
    """删除会话应同步删除 sidecar"""
    print("\n🧪 test_delete_removes_sidecar")
    p = Path(tmpdir) / "abc.jsonl"
    p.touch()
    SessionStorage.write_cache_stats(p, _MockUsage(cached=100, input_tokens=1000))
    cache_p = SessionStorage._cache_stats_path(p)
    assert cache_p.exists(), "sidecar 应存在"
    SessionManager.delete_session("abc", data_dir=tmpdir)
    ok = _ok("jsonl 已删除", p.exists(), False)
    ok &= _ok("sidecar 也删除", cache_p.exists(), False)
    return ok


def test_list_sessions_includes_cache_stats(tmpdir):
    """list_sessions 返回应包含 cache_stats 字段"""
    print("\n🧪 test_list_sessions_includes_cache_stats")
    # 创建 2 个会话，1 个有 cache
    m1 = SessionManager(data_dir=tmpdir)
    m1.add_user_message("first")
    m1.flush()
    SessionStorage.write_cache_stats(
        Path(tmpdir) / f"{m1.session_id}.jsonl",
        _MockUsage(cached=9000, input_tokens=10000),
    )

    m2 = SessionManager(data_dir=tmpdir)
    m2.add_user_message("second")
    m2.flush()
    # m2 无 cache

    sessions = SessionStorage.list_sessions(data_dir=tmpdir)
    ok = True
    ok &= _ok("会话数=2", len(sessions), 2)
    # 找到 m1 和 m2
    by_id = {s["session_id"]: s for s in sessions}
    ok &= _ok("m1 在列表", m1.session_id in by_id, True)
    ok &= _ok("m2 在列表", m2.session_id in by_id, True)
    # m1 有 cache stats
    m1_stats = by_id[m1.session_id].get("cache_stats", {})
    ok &= _ok("m1 cached_tokens=9000", m1_stats.get("cached_tokens"), 9000)
    ok &= _ok("m1 hit_rate=0.9", m1_stats.get("hit_rate"), 0.9)
    # m2 是空 stats
    m2_stats = by_id[m2.session_id].get("cache_stats", {})
    ok &= _ok("m2 cached_tokens=0", m2_stats.get("cached_tokens"), 0)
    return ok


def test_sidecar_path(tmpdir):
    """sidecar 路径推导正确"""
    print("\n🧪 test_sidecar_path")
    p = Path(tmpdir) / "session_xyz.jsonl"
    expected = Path(tmpdir) / "session_xyz.cache.json"
    got = SessionStorage._cache_stats_path(p)
    return _ok(f"sidecar 路径", str(got), str(expected))


def test_corrupted_sidecar_returns_empty(tmpdir):
    """损坏的 sidecar 不应崩溃，返回空 stats"""
    print("\n🧪 test_corrupted_sidecar_returns_empty")
    p = Path(tmpdir) / "abc.jsonl"
    p.touch()
    cache_p = p.with_suffix(".cache.json")
    with open(cache_p, "w") as f:
        f.write("这不是合法 JSON {{{")
    s = SessionStorage.read_cache_stats(p)
    ok = True
    ok &= _ok("cached_tokens=0", s["cached_tokens"], 0)
    ok &= _ok("input_tokens=0", s["input_tokens"], 0)
    return ok


def main():
    # 每个测试独立 tmpdir，避免状态泄漏
    tests = [
        ("read_empty", test_read_empty),
        ("write_then_read", test_write_then_read),
        ("accumulate", test_accumulate),
        ("delete_removes_sidecar", test_delete_removes_sidecar),
        ("list_sessions_includes_cache_stats", test_list_sessions_includes_cache_stats),
        ("sidecar_path", test_sidecar_path),
        ("corrupted_sidecar_returns_empty", test_corrupted_sidecar_returns_empty),
    ]

    results = []
    for name, fn in tests:
        tmpdir = tempfile.mkdtemp(prefix=f"cache_stats_test_{name}_")
        try:
            results.append((name, fn(tmpdir)))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"📊 {passed}/{total} 测试通过")
    for name, ok in results:
        marker = "✅" if ok else "❌"
        print(f"   {marker} {name}")
    if passed == total:
        print("🎉 全部通过！")
        sys.exit(0)
    else:
        print(f"❌ {total-passed} 个失败")
        sys.exit(1)


if __name__ == "__main__":
    main()