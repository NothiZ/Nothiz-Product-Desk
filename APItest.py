# -*- coding: utf-8 -*-
"""连通性测试：python APItest.py deepseek | gemini（Key 从环境变量或 .env 读取）"""
import sys

import llm_client

llm_client.set_provider(sys.argv[1] if len(sys.argv) > 1 else "deepseek")
print(llm_client.call_llm("只回复 OK"))
