import logging
import os
import shutil
import subprocess
from datetime import datetime
import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks
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
async def handle_command(cmd: ExecutorCommand, background_tasks: BackgroundTasks):
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

    # ── scan (Warden: análisis sobre demanda) ─────────────────────────────────
    elif cmd.action == "scan":
        if cmd.service != "warden":
            return _reject(cmd, "Acción 'scan' solo disponible para warden")

        # Ejecutar warden en modo one-shot sobre el target especificado
        service_def = _registry.get("warden")
        if not service_def:
            return _reject(cmd, "Servicio 'warden' no encontrado")

        try:
            import shlex

            # Comando base sin el target fijo
            # Usamos el binario debug porque el release puede estar bloqueado por un proceso en ejecucion
            base_cmd = "target/debug/warden"
            cmd_parts = [base_cmd]

            # Agregar target (path del proyecto)
            if cmd.target:
                workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                if os.path.isabs(cmd.target):
                    project_path = cmd.target
                else:
                    project_path = os.path.join(workspace_root, cmd.target)
                cmd_parts.append(project_path)
            else:
                cmd_parts.append(".")

            # Agregar formato JSON
            cmd_parts.extend(["--format", "json"])

            # Opciones adicionales
            if options := cmd.options:
                if options.get("history"):
                    cmd_parts.extend(["--history", options["history"]])
                if options.get("only_predictions"):
                    cmd_parts.append("--only-predictions")
                if options.get("only_hotspots"):
                    cmd_parts.append("--only-hotspots")
                if options.get("only_trends"):
                    cmd_parts.append("--only-trends")

            logger.info(f"🔍 Warden scan ejecutando: {' '.join(cmd_parts)}")
            # Warden puede tardar más en proyectos grandes, usar timeout de 120s
            result = await _manager.run_once("warden", service_def, command_list=cmd_parts, timeout=120)

            logger.info(f"✅ Warden scan completado: exit_code={result.get('exit_code')}, status={result.get('status')}")
            stdout_preview = result.get('stdout', '')[:500] if result.get('stdout') else ''
            logger.info(f"   stdout preview: {stdout_preview}...")
            logger.info(f"   stdout contains JSON_RESULT_START: {'JSON_RESULT_START' in str(result.get('stdout', ''))}")
            logger.debug(f"   stdout full: {result.get('stdout', '')}...")

            return _ok(cmd, "Warden scan completado", result)
        except Exception as e:
            logger.exception(f"Error ejecutando warden scan")
            return _reject(cmd, str(e))

    # ── autofix / feature / bugfix (Ejecución de cambios mediante Aider) ───────
    elif cmd.action in ("autofix", "feature", "bugfix"):
        options = cmd.options or {}
        instruction = options.get("instruction", "")
        branch_prefix = options.get("branch_prefix", "skrymir-fix/")
        provider = options.get("provider", "ollama")
        model = options.get("model", "qwen3:8b")
        target_file = cmd.target

        if target_file is None:
            target_file = ""

        if not instruction:
            return _reject(cmd, "Falta 'instruction' para la iteración")
        if cmd.action == "autofix" and not target_file:
            return _reject(cmd, "Falta 'target' (archivo) obligatorio para autofix")

        async def run_autofix_background():
            try:
                import os
                import sys
                import subprocess
                from pathlib import Path

                workspace_root = options.get("workspace_root")
                if not workspace_root:
                    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                target_abs = target_file
                if target_file and not os.path.isabs(target_file):
                    target_abs = os.path.join(workspace_root, target_file)

                if target_file and os.path.isdir(target_abs):
                    project_path = target_abs
                elif target_file:
                    project_path = os.path.dirname(target_abs)
                else:
                    project_path = workspace_root

                if not os.path.isdir(project_path):
                    project_path = workspace_root

                import uuid
                
                branch_name = f"{branch_prefix}{uuid.uuid4().hex[:6]}"

                logger.info(f"🌿 Autofix: cwd={project_path} | aislando en nueva rama {branch_name}...")
                subprocess.run(["git", "stash"], cwd=project_path, capture_output=True)
                checkout_res = subprocess.run(["git", "checkout", "-b", branch_name], cwd=project_path, capture_output=True, text=True)
                if checkout_res.returncode != 0 and "fatal: not a git repository" in checkout_res.stderr:
                    logger.warning("No es un repositorio Git, ignorando comando checkout")
                
                uv_bin = shutil.which("uv") or r"C:\Users\Sergio\AppData\Local\Programs\Python\Python313\Scripts\uv.exe"
                logger.info(f"🤖 uv localizado en: {uv_bin} | Ejecutando Aider...")
                
                aider_cmd = [uv_bin, "tool", "run", "--from", "aider-chat", "aider", "--yes", "--no-show-model-warnings", "--message", instruction]
                
                if target_file and os.path.isfile(target_abs):
                    aider_cmd.append(target_abs)

                # Contextual extra files (for explicit features / bugfixes)
                for c_file in options.get("context_files", []):
                    c_abs = c_file if os.path.isabs(c_file) else os.path.join(workspace_root, c_file)
                    if c_abs and os.path.isfile(c_abs) and c_abs != target_abs:
                        aider_cmd.append(c_abs)
                
                auto_fix_env = os.environ.copy()

                if provider == "ollama":
                    auto_fix_env["OLLAMA_API_BASE"] = "http://localhost:11434"
                    auto_fix_env["OLLAMA_API_KEY"] = "sk-ollama-dummy"
                    aider_cmd.extend(["--model", f"ollama_chat/{model}"])
                elif provider == "anthropic":
                    aider_cmd.extend(["--model", "anthropic/claude-3-5-sonnet"])
                
                backup_files = []
                suggested_files = []
                files_to_backup = []

                if target_file and os.path.isfile(target_abs):
                    files_to_backup.append(target_abs)
                else:
                    # Al ser un directory/proyecto completo, solo respaldar context_files explicitos para evitar miles de archivos lentos
                    for c_file in options.get("context_files", []):
                        c_abs = c_file if os.path.isabs(c_file) else os.path.join(workspace_root, c_file)
                        if os.path.isfile(c_abs):
                            files_to_backup.append(c_abs)
                logger.info(f"💾 Creando backups de {len(files_to_backup)} archivos...")
                backup_count = 0
                for file_path in files_to_backup:
                    try:
                        bak_path = file_path + ".bak"
                        shutil.copy2(file_path, bak_path)
                        backup_files.append({"original": file_path, "backup": bak_path})
                        backup_count += 1
                    except Exception as copy_err:
                        logger.warning(f"⚠️ No se pudo hacer backup de {file_path}: {copy_err}")

                logger.info(f"✅ {backup_count} archivos respaldados con .bak")

                from app.models import ServiceDefinition
                auto_fix_svc = ServiceDefinition(
                    name="autofix_agent",
                    command=uv_bin,
                    cwd=project_path,
                    env=auto_fix_env,
                    shell=False
                )

                result = await _manager.run_once("autofix_agent", auto_fix_svc, command_list=aider_cmd, timeout=300)

                exit_code = result.get("exit_code", -1)
                stdout_out = result.get("stdout", "") or ""
                stderr_out = result.get("stderr", "") or ""

                modified_files = []
                git_diff = ""
                git_status = ""
                try:
                    status_res = subprocess.run(
                        ["git", "status", "--short"],
                        cwd=project_path, capture_output=True, text=True, timeout=10
                    )
                    git_status = status_res.stdout

                    for line in git_status.split('\n'):
                        if line.strip():
                            parts = line.strip().split(maxsplit=1)
                            if len(parts) == 2:
                                status_code, file_path_res = parts
                                modified_files.append({
                                    "path": file_path_res,
                                    "status": status_code
                                })

                    if modified_files:
                        diff_res = subprocess.run(
                            ["git", "diff", "--no-color", "--stat"],
                            cwd=project_path, capture_output=True, text=True, timeout=10
                        )
                        git_diff_stat = diff_res.stdout

                        diff_full_res = subprocess.run(
                            ["git", "diff", "--no-color"],
                            cwd=project_path, capture_output=True, text=True, timeout=15
                        )
                        git_diff = diff_full_res.stdout[:5000]

                    logger.info(f"📁 Archivos modificados por Aider: {len(modified_files)}")
                except Exception as git_err:
                    logger.warning(f"⚠️ No se pudo capturar git diff: {git_err}")

                meaningful_files = [f for f in modified_files if not f['path'].endswith(('.gitignore', 'architect.json', 'report.json'))]

                if exit_code != 0 and not meaningful_files:
                    logger.error(f"❌ Aider falló (código={exit_code}) y no se modificaron archivos significativos")
                    payload = {
                        "branch": branch_name,
                        "aider_exit_code": exit_code,
                        "fix_validated": False,
                        "validation_skipped": True,
                        "error": stderr_out[:500],
                        "modified_files": modified_files,
                        "git_diff_stat": git_diff[:1000] if git_diff else None,
                        "git_status": git_status[:500] if git_status else None,
                    }
                    await report_to_cerebro(payload, failed=True)
                    return

                logger.info(f"✅ Aider completó el fix. Iniciando Validation Gate...")

                build_cmd = None
                build_tool = None

                if os.path.isfile(os.path.join(project_path, "package.json")):
                    build_tool = "nodejs"
                    logger.info("📦 Node.js detectado. Ejecutando npm install...")
                    await asyncio.to_thread(
                        subprocess.run,
                        ["npm", "install"],
                        cwd=project_path,
                        capture_output=True,
                        timeout=120,
                        shell=True
                    )
                    build_cmd = ["npm", "run", "build"]

                elif os.path.isfile(os.path.join(project_path, "Cargo.toml")):
                    build_tool = "rust"
                    build_cmd = ["cargo", "build"]

                elif os.path.isfile(os.path.join(project_path, "pyproject.toml")) or \
                     os.path.isfile(os.path.join(project_path, "setup.py")):
                    build_tool = "python"
                    build_cmd = ["python", "-m", "py_compile"]

                fix_validated = False
                build_exit_code = None
                build_output = ""

                if build_cmd:
                    logger.info(f"🔨 [{build_tool}] Ejecutando: {' '.join(build_cmd)}")
                    build_svc = ServiceDefinition(
                        name=f"validate_{build_tool}",
                        command=build_cmd[0],
                        cwd=project_path,
                        env=auto_fix_env,
                        shell=True
                    )
                    build_result = await _manager.run_once(
                        f"validate_{build_tool}", build_svc,
                        command_list=build_cmd, timeout=120
                    )
                    build_exit_code = build_result.get("exit_code", -1)
                    build_output = (build_result.get("stdout") or "") + (build_result.get("stderr") or "")

                    if build_exit_code == 0:
                        fix_validated = True
                        logger.info(f"✅ Validation Gate PASÓ — Build exitoso en rama {branch_name}")
                    else:
                        fix_validated = False
                        logger.warning(f"⚠️ Validation Gate FALLÓ — Build roto. Fix no aplicable sin revisión.")
                        logger.info("🔄 Restaurando archivos originales desde .bak y creando .suggested...")

                        for backup_info in backup_files:
                            original_path = backup_info["original"]
                            bak_path = backup_info["backup"]
                            try:
                                if os.path.exists(original_path) and os.path.exists(bak_path):
                                    with open(original_path, 'r', encoding='utf-8') as f:
                                        modified_content = f.read()
                                    with open(bak_path, 'r', encoding='utf-8') as f:
                                        original_content = f.read()

                                    if modified_content != original_content:
                                        suggested_path = original_path + ".suggested"
                                        with open(suggested_path, 'w', encoding='utf-8') as f:
                                            f.write(f"# Suggested fix for: {os.path.basename(original_path)}\n")
                                            f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
                                            f.write(f"# Branch: {branch_name}\n")
                                            f.write(f"# Build failed with exit code: {build_exit_code}\n")
                                            f.write(f"# --- ORIGINAL ---\n")
                                            f.write(original_content)
                                            f.write(f"\n# --- SUGGESTED ---\n")
                                            f.write(modified_content)

                                        suggested_files.append({
                                            "original": original_path,
                                            "suggested": suggested_path,
                                            "backup": bak_path
                                        })
                                    shutil.copy2(bak_path, original_path)
                                    os.remove(bak_path)
                            except Exception as restore_err:
                                logger.error(f"   ❌ Error restaurando {original_path}: {restore_err}")
                else:
                    logger.info("⚠️ No se detectó build tool conocido. Validación omitida.")
                    fix_validated = None

                payload = {
                    "branch": branch_name,
                    "aider_exit_code": exit_code,
                    "fix_validated": fix_validated,
                    "build_tool": build_tool,
                    "build_exit_code": build_exit_code,
                    "aider_output": stdout_out[:1000],
                    "build_output": build_output[:1000],
                    "modified_files": modified_files,
                    "git_diff_stat": git_diff[:2000] if git_diff else None,
                    "git_diff_full": git_diff[:8000] if git_diff else None,
                    "git_status": git_status[:500] if git_status else None,
                    "files_count": len(modified_files),
                    "backup_count": len(backup_files),
                    "suggested_files": suggested_files,
                    "suggested_count": len(suggested_files),
                }

                await report_to_cerebro(payload, failed=False)

            except Exception as e:
                logger.exception("Error en Autofix background")
                await report_to_cerebro({"error": str(e)}, failed=True)

        async def report_to_cerebro(payload: dict, failed: bool):
            cerebro_url = options.get("cerebro_url", "http://localhost:4000")
            req_id_stripped = cmd.request_id.split("-", 1)[-1] if cmd.request_id else ""
            event_type = f"{cmd.action}_failed" if failed else f"{cmd.action}_completed"
            
            payload["autofix_id"] = req_id_stripped # Por retrocompatibilidad (Dashboard usa este en vez del command ID a veces)
            payload["request_id"] = req_id_stripped
            payload["target"] = target_file
            
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.post(f"{cerebro_url}/api/events", json={
                        "source": "executor",
                        "type": event_type,
                        "severity": "error" if failed else "info",
                        "timestamp": datetime.now().isoformat(),
                        "payload": payload
                    })
                logger.info(f"🚀 Resultado enviado a Cerebro por webhook ({event_type})")
            except Exception as e:
                logger.error(f"❌ Error reportando a Cerebro: {e}")

        # Encolar la tarea
        background_tasks.add_task(run_autofix_background)
        return _ok(cmd, f"{cmd.action.capitalize()} request encolado", {"status": "enqueued"})

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
