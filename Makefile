.PHONY: test check

# Local-only regression tests; no LLM, ORFS, or network access.
test:
	@test -d tests || { echo "Local tests/ directory is unavailable."; exit 2; }
	python3 -m unittest discover -s tests -v

# Repository-tracked pure Python verification; no LLM, ORFS, or network access.
check:
	python3 -m compileall -q core storage agents search gwtw orfs tools
	python3 main.py --help >/dev/null
	python3 multi_agent_gwtw.py --help >/dev/null
	python3 tools/trial_inspect.py --help >/dev/null
	python3 tools/trial_reproduce.py --help >/dev/null
	python3 tools/clean.py --help >/dev/null
	python3 -m tools.session_visualize --help >/dev/null
	python3 tools/checkpoint_fork_verify.py --help >/dev/null
