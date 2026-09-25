# -*- coding: utf-8 -*-
"""标镜（biaojing）——本地文件解析与证据定位底座（P1）。

产品显示名：标镜。
包含文档解析、本机 OCR、规则筛查与本地工作台；结果为待人工复核的线索。
"""

PRODUCT_NAME = "标镜"
AUTHOR = "刘奇"
VERSION = "0.2.1"

# extract_status 取值（唯一全集，报告与下游按此口径）
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_PENDING_OCR = "pending_ocr"
STATUS_PENDING_CONVERT = "pending_convert"
STATUS_DUPLICATE = "duplicate"
STATUS_UNKNOWN = "unknown"
