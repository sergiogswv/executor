import logging
from fastapi import APIRouter, HTTPException
from app.models import ExecutorCommand, CommandAck, ApiResponse, TerminalInfo
from app.registry import ServiceRegistry
from app.process_manager import ProcessManager

router = APIRouter(tags=["executor"])
logger = logging.getLogger("ejecutor.routes")

# Instancias compartidas (se inyectan desde main.py)
_registry: ServiceRegistry | None = None
_manager: ProcessManager | None = None


def init(registry: ServiceRegistry, manager: ProcessManager) -> None:
    global _registry, _manager
    _registry = registry
    _manager = manager


# ─── POST /command — Recibir instrucción del Cerebro ─────────────────────────

@router.post("/command", response_model=ApiResponse)
async def handle_command(cmd: ExecutorCommand):
    """
    Endpoint principal. El Cerebro envía instrucciones aquí.

    Acciones soportadas:
    - open      → Abre un servicio por nombre (busca en services.yaml)
    - close     → Cierra una terminal por terminal_id
    - status    → Estado de una terminal o de todas
    - list      → Lista todas las terminales activas
    - kill-all  → Cierra todas las terminales activas
    - reload    → Recarga services.yaml sin reiniciar
    """
    logger.info(f"📨 [Cerebro→Ejecutor] action={cmd.action} service={cmd.service} terminal_id={cmd.terminal_id}")

    # ── open ──────────────────────────────────────────────────────────────────
    if cmd.action == "open":
        if not cmd.service:
            return _reject(cmd, "Campo 'service' requerido para action=open")

        service_def = _registry.get(cmd.service)
        if not service_def:
            available = list(_registry.list_all().keys())
            return _reject(cmd, f"Servicio '{cmd.service}' no encontrado. Disponibles: {available}")

        # Verificar si ya está corriendo
        running = _manager.find_by_service(cmd.service)
        if running:
            logger.warning(f"⚠️  '{cmd.service}' ya está corriendo ({len(running)} instancia/s)")

        try:
            info = await _manager.open(cmd.service, service_def)
            return ApiResponse(
                ok=True,
                message=f"Servicio '{cmd.service}' levantado",
                data=CommandAck(
                    request_id=cmd.request_id,
                    status="accepted",
                    result=info.model_dump(),
                ).model_dump(),
            )
        except Exception as e:
            logger.exception(f"Error abriendo '{cmd.service}'")
            return _reject(cmd, str(e))

    # ── close ─────────────────────────────────────────────────────────────────
    elif cmd.action == "close":
        # Puede cerrar por terminal_id o por nombre de servicio
        if cmd.terminal_id:
            closed = await _manager.close(cmd.terminal_id)
            if not closed:
                return _reject(cmd, f"Terminal '{cmd.terminal_id}' no encontrada o ya cerrada")
            return _ok(cmd, f"Terminal '{cmd.terminal_id}' cerrada", {"terminal_id": cmd.terminal_id})

        elif cmd.service:
            running = _manager.find_by_service(cmd.service)
            if not running:
                return _reject(cmd, f"No hay terminales activas para '{cmd.service}'")
            for t in running:
                await _manager.close(t.terminal_id)
            ids = [t.terminal_id for t in running]
            return _ok(cmd, f"Servicio '{cmd.service}' detenido", {"closed": ids})

        return _reject(cmd, "Necesitas 'service' o 'terminal_id' para action=close")

    # ── status ────────────────────────────────────────────────────────────────
    elif cmd.action == "status":
        if cmd.terminal_id:
            info = _manager.status(cmd.terminal_id)
            if not info:
                return _reject(cmd, f"Terminal '{cmd.terminal_id}' no encontrada")
            return _ok(cmd, "ok", info.model_dump())

        all_terminals = _manager.list_all()
        return _ok(cmd, f"{len(all_terminals)} terminales activas", [t.model_dump() for t in all_terminals])

    # ── list ──────────────────────────────────────────────────────────────────
    elif cmd.action == "list":
        all_terminals = _manager.list_all()
        services_available = list(_registry.list_all().keys())
        return _ok(cmd, "ok", {
            "running": [t.model_dump() for t in all_terminals],
            "available_services": services_available,
        })

    # ── kill-all ──────────────────────────────────────────────────────────────
    elif cmd.action == "kill-all":
        closed = await _manager.kill_all()
        return _ok(cmd, f"{len(closed)} terminales cerradas", {"closed": closed})

    # ── reload ────────────────────────────────────────────────────────────────
    elif cmd.action == "reload":
        _registry.reload()
        services = list(_registry.list_all().keys())
        return _ok(cmd, "services.yaml recargado", {"services": services})

    # ── run (New: One-shot exec) ──────────────────────────────────────────────
    elif cmd.action == "run":
        if not cmd.service:
            return _reject(cmd, "Campo 'service' requerido para action=run")
        
        service_def = _registry.get(cmd.service)
        if not service_def:
            return _reject(cmd, f"Servicio '{cmd.service}' no encontrado")

        try:
            options = cmd.options or {}
            
            # Limpiamos el comando base del servicio si tiene subcomandos por defecto (como 'serve')
            cmd_base = service_def.command
            if options.get("init") and " serve" in cmd_base:
                cmd_base = cmd_base.replace(" serve", "")
                
            cmd_parts = cmd_base.split()
            
            # Construcción del comando: binary [subcommand] [args...]
            if options.get("init"):
                cmd_parts.append("init")
                
            if cmd.target:
                cmd_parts.extend(["--path", cmd.target])
                
            if options.get("force"):
                cmd_parts.append("--force")
                
            if options.get("pattern"):
                cmd_parts.extend(["--pattern", options.get("pattern")])
                
            # Flag clave que acabamos de implementar en Architect
            cmd_parts.append("--yes") 
            
            logger.info(f"🚀 Ejecutando comando one-shot: {' '.join(cmd_parts)}")
            result = await _manager.run_once(cmd.service, service_def, command_list=cmd_parts)
            
            return _ok(cmd, "Comando ejecutado exitosamente", result)
        except Exception as e:
            logger.exception(f"Error ejecutando '{cmd.service}'")
            return _reject(cmd, str(e))

    # ── desconocido ───────────────────────────────────────────────────────────
    else:
        return _reject(cmd, f"Acción '{cmd.action}' no reconocida")


# ─── GET /terminals — Ver estado sin mandar comando ───────────────────────────

@router.get("/terminals", response_model=ApiResponse)
async def list_terminals():
    """Lista todas las terminales activas (sin necesitar un command body)."""
    terminals = _manager.list_all()
    return ApiResponse(
        ok=True,
        message=f"{len(terminals)} terminales activas",
        data=[t.model_dump() for t in terminals],
    )


@router.get("/services", response_model=ApiResponse)
async def list_services():
    """Lista todos los servicios configurados en services.yaml."""
    all_services = _registry.list_all()
    return ApiResponse(
        ok=True,
        message=f"{len(all_services)} servicios disponibles",
        data={k: v.model_dump() for k, v in all_services.items()},
    )


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ejecutor",
        "active_terminals": len(_manager.list_all()),
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ok(cmd: ExecutorCommand, message: str, result) -> ApiResponse:
    return ApiResponse(
        ok=True,
        message=message,
        data=CommandAck(request_id=cmd.request_id, status="completed", result=result).model_dump(),
    )


def _reject(cmd: ExecutorCommand, error: str) -> ApiResponse:
    logger.warning(f"✗ Rechazado: {error}")
    return ApiResponse(
        ok=False,
        message=error,
        data=CommandAck(request_id=cmd.request_id, status="rejected", error=error).model_dump(),
    )
