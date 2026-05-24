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
- Python 3.10+
- Chrome Browser (for browser automation tasks)
- Valid API Keys (configured in `.env`)

### Installation
1.  Clone the repository.
2.  Install dependencies: `pip install -r requirements.txt`
3.  Configure `.env` using `.env.example`.
4.  Run the system: `python main.py`

## Documentation
- [Lecciones Aprendidas](LECCIONES_APRENDIDAS.md): The system's long-term memory.
- [Changelog](CHANGELOG.md): History of changes.
- [Contributing](CONTRIBUTING.md): Guidelines for development and role behavior.