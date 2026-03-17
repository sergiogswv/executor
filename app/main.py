import logging
import asyncio
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from app.registry import ServiceRegistry
from app.process_manager import ProcessManager
from app import routes

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ejecutor")

# Instancias globales
registry = ServiceRegistry(settings.services_config)
manager = ProcessManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    routes.init(registry, manager)
    logger.info("⚡ Ejecutor arriba | puerto=%s", settings.port)
    logger.info("📋 Servicios disponibles: %s", list(registry.list_all().keys()))

    # AUTO-START CEREBRO
    if "cerebro" in registry.list_all():
        logger.info("🧠 Auto-iniciando Cerebro...")
        try:
            from app.models import ExecutorCommand
            cmd = ExecutorCommand(action="open", service="cerebro", request_id="boot-cerebro")
            svc_def = registry.get("cerebro")
            await manager.start(svc_def, cmd.request_id, env_overrides=svc_def.env)
        except Exception as e:
            logger.error("❌ No se pudo auto-iniciar Cerebro: %s", e)

    yield
    # Al apagar: cerrar todas las terminales activas
    if manager.list_all():
        logger.info("🔴 Cerrando terminales activas...")
        await manager.kill_all()
    logger.info("⚡ Ejecutor apagado")


app = FastAPI(
    title="Ejecutor — Gestor de Terminales",
    description=(
        "Agente que abre, gestiona y cierra procesos/servicios "
        "bajo instrucción del Cerebro. Los comandos disponibles "
        "están definidos en services.yaml."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router)


@app.get("/status", tags=["root"])
async def status_compatibility():
    """Endpoint para compatibilidad con el Dashboard (vía GET)."""
    from app.routes import _manager
    terminals = _manager.list_all()
    return {
        "ok": True,
        "data": {
            "terminals": [t.model_dump() for t in terminals]
        }
    }


@app.get("/", tags=["root"])
async def root():
    return {
        "service": "ejecutor",
        "version": "0.1.0",
        "active_terminals": len(manager.list_all()),
        "available_services": list(registry.list_all().keys()),
        "docs": "/docs",
    }
