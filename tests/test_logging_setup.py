"""
agent_core.logging_setup 测试 —— 按小时切分的文件日志 + 幂等。

setup_logging 改的是全局 root logger,每个 case 跑完必须还原 root handlers,
否则会污染其他测试(尤其那些断 caplog 的 memory 测试)。
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from agent_core.logging_setup import setup_logging, _FILE_HANDLER_TAG


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """快照 + 还原 root logger 状态,避免 setup_logging 污染全局。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    # 清空,让每个 case 从干净状态开始
    root.handlers = []
    try:
        yield
    finally:
        # 关掉本 case 新建的 handler(释放文件句柄),再还原
        for h in list(root.handlers):
            if h not in saved_handlers:
                h.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _file_handlers(root):
    return [h for h in root.handlers if getattr(h, _FILE_HANDLER_TAG, False)]


def test_creates_timed_rotating_file_handler(tmp_path):
    """1. setup_logging(log_dir=tmp) → root 有一个按小时切分的文件 handler"""
    root = setup_logging(level=logging.INFO, log_dir=tmp_path)

    fhs = _file_handlers(root)
    assert len(fhs) == 1, f"应有 1 个带 tag 的文件 handler,实际 {len(fhs)}"
    fh = fhs[0]
    assert isinstance(fh, TimedRotatingFileHandler)
    assert fh.when == "H"  # 按小时
    assert fh.interval == 3600  # when=H → interval 秒数
    assert fh.baseFilename == str(tmp_path / "agent.log")


def test_writes_log_line_to_file(tmp_path):
    """2. 写一条 INFO → flush → agent.log 文件存在且含该行"""
    setup_logging(level=logging.INFO, log_dir=tmp_path)

    logging.getLogger("test.x").info("hello-logfile-marker")
    for h in logging.getLogger().handlers:
        h.flush()

    log_file = tmp_path / "agent.log"
    assert log_file.exists(), "agent.log 应被创建"
    content = log_file.read_text(encoding="utf-8")
    assert "hello-logfile-marker" in content


def test_idempotent_no_duplicate_handlers(tmp_path):
    """3. 连续调两次(模拟 Streamlit rerun)→ 第二次不新增任何 handler

    不硬断 console handler 数量 —— pytest 会注入自己的 LogCaptureHandler
    (StreamHandler 子类),干扰精确计数。改测"幂等"这个真正在意的属性:
    第二次调用后 handler 总数不变,且文件 handler 始终只有 1 个。
    """
    root = logging.getLogger()
    setup_logging(level=logging.INFO, log_dir=tmp_path)
    n_after_first = len(root.handlers)

    setup_logging(level=logging.INFO, log_dir=tmp_path)
    n_after_second = len(root.handlers)

    assert n_after_second == n_after_first, "第二次调用不应新增 handler"
    assert len(_file_handlers(root)) == 1, "重复调用不应堆叠文件 handler"


def test_debug_silences_noisy_libs(tmp_path):
    """4. DEBUG 模式 → 第三方库 logger 降到 WARNING"""
    setup_logging(level=logging.DEBUG, log_dir=tmp_path)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("urllib3").level == logging.WARNING
