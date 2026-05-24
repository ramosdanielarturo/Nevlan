import os

# Estructura "Unicornio / Commercial Release Ready"
structure = {
    "ArthurOS": [
        # Root Files
        ".gitignore",
        ".env.example",
        ".env",
        "pyproject.toml",
        "pytest.ini",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODEOWNERS",
        "Makefile",
        "main.py",

        # Docs
        "docs/architecture.md",
        "docs/threat_model.md",
        "docs/permissions.md",
        "docs/packaging.md",
        "docs/api.md",
        "docs/adr/0001-event-bus.md",
        "docs/adr/0002-llm-provider-abstraction.md",

        # Var (Local Data)
        "var/.keep",
        "var/logs/.keep",
        "var/audit/.keep",
        "var/memory_db/.keep",
        "var/sandbox/.keep",
        "var/cache/.keep",
        "var/crashes/.keep",

        # Scripts (DevOps)
        "scripts/run_dev.py",
        "scripts/collect_diagnostics.py",
        "scripts/make_release.py",
        "scripts/sign_artifacts.py",
        "scripts/migrate_data.py",

        # Tests
        "tests/conftest.py",
        "tests/unit/.keep",
        "tests/integration/.keep",
        "tests/e2e/.keep",

        # APP Core & Infra
        "app/__init__.py",
        "app/core/__init__.py",
        "app/core/paths.py",
        "app/core/config.py",
        "app/core/logger.py",
        "app/core/exceptions.py",
        "app/core/clock.py",
        "app/core/feature_flags.py",
        "app/core/migrations/__init__.py",
        "app/core/migrations/v0001_init.sql",

        # Contracts
        "app/contracts/__init__.py",
        "app/contracts/intent.py",
        "app/contracts/tool_call.py",
        "app/contracts/tool_result.py",
        "app/contracts/events.py",
        "app/contracts/policies.py",
        "app/contracts/telemetry.py",

        # Security
        "app/security/__init__.py",
        "app/security/policy.py",
        "app/security/confirmations.py",
        "app/security/permission_store.py",
        "app/security/redaction.py",
        "app/security/audit.py",
        "app/security/crypto.py",
        "app/security/network_egress.py",

        # Runtime
        "app/runtime/__init__.py",
        "app/runtime/lifecycle.py",
        "app/runtime/bus.py",
        "app/runtime/scheduler.py",
        "app/runtime/supervisor.py",
        "app/runtime/safe_mode.py",
        "app/runtime/ipc/__init__.py",
        "app/runtime/ipc/protocol.py",
        "app/runtime/ipc/server.py",
        "app/runtime/ipc/client.py",

        # Brain
        "app/brain/__init__.py",
        "app/brain/intent_parser.py",
        "app/brain/agent.py",
        "app/brain/planner.py",
        "app/brain/critic.py",
        "app/brain/memory_manager.py",
        "app/brain/cost_guard.py",

        # Platforms
        "app/platforms/__init__.py",
        "app/platforms/base.py",
        "app/platforms/detect.py",
        "app/platforms/win.py",
        "app/platforms/mac.py",
        "app/platforms/linux.py",

        # Skills
        "app/skills/__init__.py",
        "app/skills/registry.py",
        "app/skills/tools/__init__.py",
        "app/skills/tools/os.py",
        "app/skills/tools/web.py",
        "app/skills/tools/vision.py",
        "app/skills/tools/fs/__init__.py",
        "app/skills/tools/fs/read.py",
        "app/skills/tools/fs/write.py",
        "app/skills/workflows/__init__.py",
        "app/skills/workflows/research.py",
        "app/skills/workflows/clean_workspace.py",

        # Services
        "app/services/__init__.py",
        "app/services/llm/__init__.py",
        "app/services/llm/base.py",
        "app/services/llm/factory.py",
        "app/services/llm/openai.py",
        "app/services/llm/groq.py",
        "app/services/llm/ollama.py",
        "app/services/telemetry/__init__.py",
        "app/services/telemetry/metrics.py",
        "app/services/telemetry/errors.py",
        "app/services/updater/__init__.py",
        "app/services/updater/channels.py",
        "app/services/updater/manifest.py",
        "app/services/updater/rollback.py",
        "app/services/licensing/__init__.py",
        "app/services/licensing/verifier.py",
        "app/services/licensing/entitlements.py",

        # Interfaces
        "app/interfaces/__init__.py",
        "app/interfaces/common/.keep",
        "app/interfaces/cli/__init__.py",
        "app/interfaces/cli/runner.py",
        "app/interfaces/desktop/__init__.py",
        "app/interfaces/desktop/main_window.py",
        "app/interfaces/desktop/voice_bridge.py",
        "app/interfaces/desktop/assets/.keep",
        "app/interfaces/api/__init__.py",
        "app/interfaces/api/server.py",
        "app/interfaces/api/auth.py",
        "app/interfaces/api/rate_limit.py",
    ]
}

def create_structure():
    base_dir = os.getcwd()
    print(f"🚀 Desplegando Arquitectura ArthurOS Unicorn Edition en: {base_dir}")
    
    for item in structure["ArthurOS"]:
        path = os.path.join(base_dir, item)
        directory = os.path.dirname(path)
        
        # Crear carpetas
        if not os.path.exists(directory) and directory:
            os.makedirs(directory)
            
        # Crear archivo
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                # Contenido boilerplate inteligente
                if item == ".gitignore":
                    f.write("var/**\n!var/.keep\n.env\n__pycache__/\n.pytest_cache/\n*.pyc\n*.spec\ndist/\nbuild/")
                elif item == ".env.example":
                    f.write("GROQ_API_KEY=gsk_change_me\nOPENAI_API_KEY=sk-change-me\nENV=dev")
                elif item == "README.md":
                    f.write("# ArthurOS\n\nSistema Operativo de Agentes con Arquitectura Enterprise.")
                elif item == "Makefile":
                    f.write("test:\n\tpytest tests/\nrun:\n\tpython main.py")
                elif item == "pytest.ini":
                    f.write("[pytest]\ntestpaths = tests\nmarkers =\n    unit\n    integration\n    e2e")
                elif item == "app/core/paths.py":
                    f.write("from pathlib import Path\nROOT_DIR = Path(__file__).parent.parent.parent\nVAR_DIR = ROOT_DIR / 'var'")
                else:
                    pass # Archivo vacío
            print(f"✅ Creado: {item}")
        else:
            print(f"⚠️ Ya existe: {item}")

    print("\n✨ ¡Infraestructura Desplegada! Estás listo para construir el futuro. ✨")

if __name__ == "__main__":
    create_structure()