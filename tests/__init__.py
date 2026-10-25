# -*- coding: utf-8 -*-
"""栖语桌面版测试包。"""

# ---- 测试数据隔离：必须放在任何项目模块 import 之前 ----
# unittest discover 会先导入 test_channels_tools（字母序在前），它间接 import memory，
# 而 memory/pathutil 是在 import 时就读 QIYU_DATA_DIR 绑定数据目录的。若不在这里提前
# 设好，memory 会绑到真实的 data/，测试数据就写进真实目录了（此前已污染过一次）。
# 用 setdefault：尊重外部显式设置的 QIYU_DATA_DIR，只在没设时兜底。
import os
import atexit
import shutil
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="qiyu_test_")
os.environ.setdefault("QIYU_DATA_DIR", _TEST_DATA_DIR)


@atexit.register
def _cleanup_test_data_dir():
    """进程退出时清掉临时数据目录，别在系统临时区留一堆垃圾。"""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
