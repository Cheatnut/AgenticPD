.PHONY: test check

# 阶段 A 纯 Python 测试；不触发 LLM、ORFS 或网络访问。
test:
	python3 -m unittest discover -s tests -v

# 最小验证入口（本地与 CI 均可用）。
check: test
