# ArthurOS

**Sistema Operativo de Agentes con Arquitectura Enterprise.**

ArthurOS is not just a collection of scripts; it is a sophisticated, agentic operating system designed to automate complex tasks, manage workflows, and continuously learn from user interactions.

## Mission
To create a seamless, voice-activated, and autonomous environment where agents (roles) collaborate to solve problems, write code, and manage the system itself, mimicking a high-level enterprise team.

## Architecture
The system is built on a modular architecture where different "Roles" (Agents) handle specific domains:

*   **Arthur_Prime**: Senior Product Manager. Orchestrates tasks, manages the backlog, and ensures strategic alignment.
*   **Arthur_Scribe**: Documentation Lead. Maintains `README.md`, `CHANGELOG.md`, and the Critical `LECCIONES_APRENDIDAS.md`.
*   **Arthur_Dev**: Senior Software Engineer. Handles implementation, refactoring, and code quality.
*   **Arthur_QA**: Quality Assurance & Security. Verifies code, runs tests, and ensures security compliance.
*   **Arthur_UI**: Frontend Specialist. Ensures the "10 Million Dollar Look" and seamless user experience.

## Getting Started

### Prerequisites
- Python 3.10+ (3.11 recommended for CI parity)
- Chrome Browser (for live browser automation)
- API keys only if you use cloud LLM features (configure in `.env`, never commit secrets)

### Installation

Dependencies live in `pyproject.toml` (single source of truth). There is no `requirements.txt`.

**CI / local minimum** (Ubuntu gate, headless tests):

```bash
python -m pip install -U pip setuptools wheel
pip install -e ".[ci]"
pytest tests/unit/test_fase7_release_benchmark.py -q
python scripts/validate_fase7_certified_release.py
```

**Desktop** (UI, voice, browser automation — not for CI):

```bash
pip install -e ".[desktop]"
playwright install chromium   # once
```

**Full dev on Windows** (CI + desktop + Windows hooks + optional LLM):

```bash
pip install -e ".[ci,desktop,windows,llm]"
playwright install chromium
```

Configure `.env` from `.env.example`, then run: `python main.py`

## Documentation
- [Lecciones Aprendidas](LECCIONES_APRENDIDAS.md): The system's long-term memory.
- [Changelog](CHANGELOG.md): History of changes.
- [Contributing](CONTRIBUTING.md): Guidelines for development and role behavior.