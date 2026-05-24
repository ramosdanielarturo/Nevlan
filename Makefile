test:
	pytest tests/
run:
	python main.py
fase0-gate:
	python scripts/run_golden_mission.py --mode dry-run
	pytest tests/unit/test_fase0_runtime_convergence.py -q
golden-runner:
	python scripts/run_golden_mission.py --mode dry-run
fase7-gate:
	python scripts/run_fase7_benchmark.py --suite default --mode dry-run --runs 5 --report json
	pytest tests/unit/test_fase7_release_benchmark.py -q
	python scripts/validate_fase7_certified_release.py
fase7-benchmark:
	python scripts/run_fase7_benchmark.py --suite default --mode live --runs 5 --report json
fase7-certified:
	python scripts/validate_fase7_certified_release.py
fase5-gate:
	python scripts/run_fase5_gate.py --replays 3 --report json
	pytest tests/unit/test_fase5_deterministic_runtime.py -q