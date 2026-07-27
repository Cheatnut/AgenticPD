.PHONY: test check

# Stage A pure Python tests; no LLM, ORFS, or network access.
test:
	python3 -m unittest discover -s tests -v

# Minimal verification entry point (local and CI).
check: test
