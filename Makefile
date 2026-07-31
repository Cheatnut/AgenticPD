.PHONY: test check

# Local-only regression tests; no LLM, ORFS, or network access.
test:
	@test -d tests || { echo "Local tests/ directory is unavailable."; exit 2; }
	python3 -m unittest discover -s tests -v

# Repository-tracked pure Python verification; no LLM, ORFS, or network access.
check:
	python3 schemas/trial.py
	python3 orchestrator.py
	python3 multi_agent_gwtw_orchestrator.py
	python3 main.py --help >/dev/null
	python3 multi_agent_gwtw.py --help >/dev/null
	python3 tools/session_visualize.py --help >/dev/null
