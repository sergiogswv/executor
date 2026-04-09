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

_active_autofixes = {}

def _find_tests_for_files(modified_files, project_path):
    """Intenta encontrar archivos de test específicos para los archivos modificados."""
    test_files = []
    for f in modified_files:
        path = f.get("path", "")
        if not path: continue
        
        base = os.path.splitext(path)[0]
        ext = os.path.splitext(path)[1]
        
        candidates = []
        if ext in (".ts", ".js", ".tsx", ".jsx"):
            candidates = [f"{base}.spec{ext}", f"{base}.test{ext}"]
        elif ext == ".py":
            candidates = [f"{base}_test.py", f"tests/test_{os.path.basename(path)}"]
            
        for cand in candidates:
            if os.path.isfile(os.path.join(project_path, cand)):
                test_files.append(cand)
                
    return list(set(test_files))


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
        # El modelo ahora viene configurado desde Cerebro en la request
        provider = options.get("provider", "openrouter")
        model = options.get("model", "google/gemini-2.0-flash-exp:free")
        api_key = options.get("api_key")

        # Normalización de proveedores para compatibilidad con Aider
        if provider == "gemini-open-source":
            provider = "gemini"
        
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
                # workspace_root ya es el directorio raíz del proyecto destino (ej: pro-leads-backend)
                if not workspace_root or not os.path.isdir(workspace_root):
                    project_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                else:
                    project_path = workspace_root

                # Calcular ruta absoluta del archivo target (para backup y verificación)
                target_abs = target_file
                if target_file and not os.path.isabs(target_file):
                    target_abs = os.path.join(project_path, target_file)

                # --- Inferencia de la raíz del proyecto ---
                # Si target_file es absoluto y no tenemos workspace_root explícito, 
                # intentamos encontrar la raíz subiendo niveles.
                if target_file and os.path.isabs(target_abs) and not options.get("workspace_root"):
                    _curr = os.path.dirname(target_abs)
                    for _ in range(4):
                        if os.path.isfile(os.path.join(_curr, "package.json")) or \
                           os.path.isfile(os.path.join(_curr, "pyproject.toml")) or \
                           os.path.isfile(os.path.join(_curr, "Cargo.toml")):
                            project_path = _curr
                            logger.info(f"📍 Raíz del proyecto detectada vía target_file: {project_path}")
                            break
                        _parent = os.path.dirname(_curr)
                        if _parent == _curr: break
                        _curr = _parent

                # --- Normalización de ruta para Windows (evitar error de CMD con rutas UNC/LongPath) ---
                if project_path.startswith("\\\\?\\"):
                    project_path = project_path.replace("\\\\?\\", "")

                # Bloquear ejecuciones adicionales en este proyecto
                if project_path in _active_autofixes:
                    logger.warning(f"⚠️ Ya hay un proceso activo para {project_path}. Ignorando duplicado.")
                    return
                _active_autofixes[project_path] = True
                logger.info(f"🔒 [Executor] Bloqueando proyecto: {project_path}")

                import uuid
                
                branch_name = f"{branch_prefix}{uuid.uuid4().hex[:6]}"

                logger.info(f"🌿 Autofix: cwd={project_path} | target={target_file or '(sin archivo)'} | rama {branch_name}...")
                subprocess.run(["git", "stash"], cwd=project_path, capture_output=True)
                checkout_res = subprocess.run(["git", "checkout", "-b", branch_name], cwd=project_path, capture_output=True, text=True)
                if checkout_res.returncode != 0 and "fatal: not a git repository" in checkout_res.stderr:
                    logger.warning("No es un repositorio Git, ignorando comando checkout")
                
                uv_bin = shutil.which("uv") or r"C:\Users\Sergio\AppData\Local\Programs\Python\Python313\Scripts\uv.exe"
                logger.info(f"🤖 uv localizado en: {uv_bin} | Ejecutando Aider...")

                # ── Detección de intent de eliminación en directorio ─────────────────
                # Los modelos locales tienen safety filters que bloquean "quitar/eliminar".
                # Solución: si el target es un directorio Y la instrucción tiene intent de
                # eliminación ESTRUCTURAL (no de contenido), usar git rm directamente.
                # NOTA: usamos _aider_instruction (variable local) para evitar UnboundLocalError
                # que ocurre cuando se reasigna una variable del scope exterior dentro de una closure.
                _aider_instruction = options.get("instruction", "")
                _aider_instruction_lower = _aider_instruction.lower()

                # Condición 1: palabras de eliminación genéricas
                _delete_keywords = {"quitar", "eliminar", "borrar", "delete", "remove", "erase", "drop"}
                _has_delete_kw = any(kw in _aider_instruction_lower for kw in _delete_keywords)

                # Condición 2: la instrucción se refiere al directorio/módulo mismo,
                # no a CONTENIDO dentro de él (ej: "quita los comentarios" debe pasar a Aider)
                _dir_structural_keywords = {
                    "módulo", "modulo", "carpeta", "directorio", "folder", "module",
                    "todo lo que", "todo el contenido", "todos los archivos", "los archivos de"
                }
                _has_structural_kw = any(kw in _aider_instruction_lower for kw in _dir_structural_keywords)

                # Condición 3: la instrucción menciona el nombre del target explícitamente
                _target_name_mentioned = False
                if target_file:
                    _target_basename = os.path.basename(target_file.rstrip("/\\")).lower()
                    _target_name_mentioned = bool(_target_basename) and _target_basename in _aider_instruction_lower

                # Solo activar git rm si: delete + (estructural O menciona el target)
                _is_dir_delete_intent = _has_delete_kw and (_has_structural_kw or _target_name_mentioned)


                if target_file and _is_dir_delete_intent:
                    target_abs_check = target_abs if os.path.isabs(target_abs) else os.path.join(project_path, target_file)
                    target_abs_check = target_abs_check.rstrip("/\\")
                    if os.path.isdir(target_abs_check):
                        logger.info(f"🗑️ Intent de eliminación detectado en directorio. Usando git rm en vez de Aider para: {target_abs_check}")
                        rm_result = subprocess.run(
                            ["git", "rm", "-r", "--force", target_abs_check],
                            cwd=project_path, capture_output=True, text=True
                        )
                        if rm_result.returncode == 0:
                            logger.info(f"✅ git rm exitoso: {rm_result.stdout.strip()}")
                            # Redirigir: Aider limpia imports rotos (no "elimina" nada)
                            _aider_instruction = (
                                f"Se eliminaron los archivos de '{target_file}'. "
                                f"Por favor revisa el proyecto y elimina todos los imports, "
                                f"referencias y declaraciones que apunten a módulos de '{target_file}' "
                                f"que ya no existen. Limpia app.module.ts y cualquier otro archivo "
                                f"que importe desde esa ruta. No agregues código nuevo, solo limpia las referencias rotas."
                            )
                            logger.info(f"🔧 Instrucción redirigida para limpieza de imports post-rm")
                        else:
                            logger.warning(f"⚠️ git rm falló: {rm_result.stderr[:300]}. Continuando con Aider.")

                aider_cmd = [uv_bin, "tool", "run", "--from", "aider-chat", "aider",
                             "--yes", "--no-show-model-warnings", "--no-auto-commits", "--no-pretty",
                             "--message", _aider_instruction]


                # Pasar el archivo a Aider: intentar ruta absoluta primero, si no existe usar relativa
                # Si el target es un DIRECTORIO, expandir a los archivos individuales dentro de él
                if target_file:
                    target_abs_dir = target_abs if os.path.isabs(target_abs) else os.path.join(project_path, target_file)
                    target_abs_dir = target_abs_dir.rstrip("/\\")

                    if os.path.isdir(target_abs_dir):
                        # Expandir directorio: pasar todos los archivos de código directamente
                        logger.info(f"📂 Target es directorio, expandiendo archivos en: {target_abs_dir}")
                        source_exts = {".ts", ".js", ".py", ".rs", ".go", ".java", ".cs", ".tsx", ".jsx"}
                        dir_files = [
                            os.path.join(target_abs_dir, f)
                            for f in os.listdir(target_abs_dir)
                            if os.path.isfile(os.path.join(target_abs_dir, f))
                            and os.path.splitext(f)[1].lower() in source_exts
                        ]
                        for df in dir_files:
                            aider_cmd.append(df)
                            logger.info(f"  📎 Archivo del directorio: {df}")
                        if not dir_files:
                            logger.warning(f"⚠️ Directorio vacío o sin archivos de código reconocidos: {target_abs_dir}")
                    elif os.path.isfile(target_abs_dir):
                        aider_cmd.append(target_abs_dir)
                        logger.info(f"📎 Archivo target (abs): {target_abs_dir}")
                    elif os.path.isfile(os.path.join(project_path, target_file)):
                        aider_cmd.append(os.path.join(project_path, target_file))
                        logger.info(f"📎 Archivo target (proyecto): {os.path.join(project_path, target_file)}")
                    else:
                        # Pasar relativo y dejar que Aider lo resuelva desde el cwd
                        aider_cmd.append(target_file)
                        logger.warning(f"⚠️ Archivo target no verificado, pasando relativo: {target_file}")

                # Contextual extra files (for explicit features / bugfixes)
                for c_file in options.get("context_files", []):
                    c_abs = c_file if os.path.isabs(c_file) else os.path.join(project_path, c_file)
                    if c_abs and os.path.isfile(c_abs) and c_abs not in aider_cmd:
                        aider_cmd.append(c_abs)

                from app.models import ServiceDefinition
                import re as _re

                auto_fix_env = os.environ.copy()

                # Inyectar API Key según el proveedor
                if api_key:
                    if provider == "openrouter":
                        auto_fix_env["OPENROUTER_API_KEY"] = api_key
                    elif provider == "gemini":
                        auto_fix_env["GEMINI_API_KEY"] = api_key
                    elif provider == "anthropic":
                        auto_fix_env["ANTHROPIC_API_KEY"] = api_key
                    elif provider == "openai":
                        auto_fix_env["OPENAI_API_KEY"] = api_key

                if provider == "ollama":
                    auto_fix_env["OLLAMA_API_BASE"] = "http://localhost:11434"
                    auto_fix_env["OLLAMA_API_KEY"] = "sk-ollama-dummy"
                    aider_cmd.extend(["--model", f"ollama_chat/{model}"])
                else:
                    # Para el resto (gemini, openrouter, anthropic, openai), Aider usa provider/model
                    aider_cmd.extend(["--model", f"{provider}/{model}"])

                # ── Backups antes del loop (para poder rollback si todo falla) ─────────
                backup_files = []
                files_to_backup = []

                if target_file and os.path.isfile(target_abs):
                    files_to_backup.append(target_abs)
                else:
                    for c_file in options.get("context_files", []):
                        c_abs = c_file if os.path.isabs(c_file) else os.path.join(project_path, c_file)
                        if os.path.isfile(c_abs):
                            files_to_backup.append(c_abs)

                logger.info(f"💾 Creando backups de {len(files_to_backup)} archivos...")
                for file_path in files_to_backup:
                    try:
                        bak_path = file_path + ".bak"
                        shutil.copy2(file_path, bak_path)
                        backup_files.append({"original": file_path, "backup": bak_path})
                    except Exception as copy_err:
                        logger.warning(f"⚠️ No se pudo hacer backup de {file_path}: {copy_err}")
                logger.info(f"✅ {len(backup_files)} archivos respaldados con .bak")

                logger.info(f"✅ Setup completo. Iniciando loop de iteraciones (máx={options.get('max_build_retries', 3)})...")


                # ── Detectar build/test tool (una sola vez antes del loop) ──────────
                build_cmd = options.get("build_command")
                test_cmd = options.get("test_command")
                build_tool = "custom" if build_cmd or test_cmd else None

                if not build_cmd or not test_cmd:
                    if os.path.isfile(os.path.join(project_path, "package.json")):
                        build_tool = "nodejs"
                        if not build_cmd: build_cmd = ["npm", "run", "build"]
                        if not test_cmd: test_cmd = ["npm", "test"]
                    elif os.path.isfile(os.path.join(project_path, "Cargo.toml")):
                        build_tool = "rust"
                        if not build_cmd: build_cmd = ["cargo", "build"]
                        if not test_cmd: test_cmd = ["cargo", "test"]
                    elif os.path.isfile(os.path.join(project_path, "pyproject.toml")) or \
                         os.path.isfile(os.path.join(project_path, "setup.py")):
                        build_tool = "python"
                        if not build_cmd: build_cmd = ["python", "-m", "py_compile"]
                        if not test_cmd: test_cmd = ["python", "-m", "pytest"]

                if isinstance(build_cmd, str): build_cmd = build_cmd.split()
                if isinstance(test_cmd, str): test_cmd = test_cmd.split()

                # ── Pre-check: build ANTES de Aider para obtener línea base de errores ──
                # Así solo pasamos a Aider los errores NUEVOS que él introdujo,
                # no errores pre-existentes que ya estaban en el proyecto.
                baseline_errors = set()
                if build_cmd and options.get("require_build", True):
                    logger.info(f"🏗️ Pre-check build (línea base)...")
                    try:
                        pre_build_svc = ServiceDefinition(
                            name="pre_build_baseline",
                            command=build_cmd[0],
                            cwd=project_path,
                            env=auto_fix_env,
                            shell=True
                        )
                        pre_result = await _manager.run_once(
                            "pre_build_baseline", pre_build_svc,
                            command_list=build_cmd, timeout=120
                        )
                        pre_output = (pre_result.get("stdout") or "") + (pre_result.get("stderr") or "")
                        pre_exit = pre_result.get("exit_code", 0)

                        if pre_exit != 0:
                            # Extraer líneas de error para comparar después (ignorar ANSI codes básicos)
                            ansi_escape = _re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
                            clean_pre = ansi_escape.sub('', pre_output)
                            for line in clean_pre.splitlines():
                                line = line.strip()
                                if "error TS" in line or ("error" in line.lower() and line.startswith("src")):
                                    baseline_errors.add(line)
                            logger.warning(f"⚠️ Pre-check: build ya fallaba con {len(baseline_errors)} error(es) pre-existentes. Se excluirán del contexto de retry.")
                        else:
                            logger.info("✅ Pre-check: build limpio antes de Aider.")
                    except Exception as pre_err:
                        logger.warning(f"⚠️ No se pudo correr pre-check build: {pre_err}")

                # ── Loop de reintentos Aider ↔ Build ──────────────────────────────────
                max_iterations = options.get("max_build_retries", 3)
                current_instruction = _aider_instruction  # puede ser la original o la redirigida post-git rm
                iteration_history = []
                fix_validated = None
                build_exit_code = None
                build_output = ""
                exit_code = -1
                stdout_out = ""
                stderr_out = ""
                modified_files = []
                git_diff = ""
                git_status = ""
                suggested_files = []


                for iteration_num in range(1, max_iterations + 1):
                    logger.info(f"🔄 ── Iteración {iteration_num}/{max_iterations} ──────────────────────────")

                    # Construir comando Aider (base + archivos + modelo)
                    iter_aider_cmd = list(aider_cmd)  # copia del base sin --message
                    # Reemplazar el --message con la instrucción actual (puede incluir error de build)
                    msg_idx = iter_aider_cmd.index("--message")
                    iter_aider_cmd[msg_idx + 1] = current_instruction

                    logger.info(f"🤖 Aider instrucción: {current_instruction[:120]}...")

                    from app.models import ServiceDefinition
                    auto_fix_svc = ServiceDefinition(
                        name="autofix_agent",
                        command=uv_bin,
                        cwd=project_path,
                        env=auto_fix_env,
                        shell=False
                    )

                    result = await _manager.run_once("autofix_agent", auto_fix_svc, command_list=iter_aider_cmd, timeout=300)

                    exit_code = result.get("exit_code", -1)
                    stdout_out = result.get("stdout", "") or ""
                    stderr_out = result.get("stderr", "") or ""

                    # Capturar git diff después de Aider
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
                                    modified_files.append({"path": file_path_res, "status": status_code})

                        if modified_files:
                            diff_full_res = subprocess.run(
                                ["git", "diff", "--no-color"],
                                cwd=project_path, capture_output=True, text=True, timeout=15
                            )
                            git_diff = diff_full_res.stdout[:5000]

                        logger.info(f"📁 Iteración {iteration_num}: {len(modified_files)} archivos modificados por Aider")
                    except Exception as git_err:
                        logger.warning(f"⚠️ No se pudo capturar git diff: {git_err}")

                    meaningful_files = [f for f in modified_files if not f['path'].endswith(('.gitignore', 'architect.json', 'report.json', '.aider.chat.history.md'))]

                    # ── Safeguard 1: Limpiar archivos placeholder del modelo ──────────
                    # El modelo a veces genera "path/to/filename.js" o "}" como nombres
                    # de archivo en whole-edit format. Aider los crea como archivos reales.
                    _placeholder_patterns = {"path/to/", "path\\to\\", "example/", "<filename", "filename.js", "filename.ts"}
                    for mf in list(modified_files):
                        mf_path = mf["path"]
                        mf_status = mf.get("status", "")
                        _is_placeholder = (
                            any(p in mf_path for p in _placeholder_patterns) or
                            mf_path.strip() in {"}", "{", "//", "/*"} or
                            (len(mf_path) <= 2 and mf_status == "??")  # archivos de un char como "}"
                        )
                        if _is_placeholder:
                            abs_placeholder = os.path.join(project_path, mf_path)
                            try:
                                if os.path.isfile(abs_placeholder):
                                    os.remove(abs_placeholder)
                                    logger.warning(f"🧹 Archivo placeholder eliminado: {mf_path}")
                            except Exception:
                                pass
                            modified_files = [f for f in modified_files if f["path"] != mf_path]

                    # ── Safeguard 2: Detectar corrupción de archivos con backup ───────
                    # Si uno de los archivos respaldados fue reducido a < 3 líneas o < 30 chars
                    # algo fue muy mal — restaurar y abortar.
                    _corruption_detected = False
                    for backup_info in backup_files:
                        orig = backup_info["original"]
                        try:
                            if os.path.isfile(orig):
                                with open(orig, 'r', encoding='utf-8', errors='ignore') as _f:
                                    _content = _f.read()
                                if len(_content.strip()) < 30 or _content.count('\n') < 2:
                                    logger.error(f"🚨 Corrupción detectada en {orig} ({len(_content)} chars). Restaurando backup...")
                                    shutil.copy2(backup_info["backup"], orig)
                                    _corruption_detected = True
                        except Exception as _ce:
                            logger.warning(f"⚠️ No se pudo verificar integridad de {orig}: {_ce}")
                    if _corruption_detected:
                        iteration_history.append({
                            "iteration": iteration_num,
                            "instruction_preview": current_instruction[:200],
                            "aider_exit_code": exit_code,
                            "files_modified": len(meaningful_files),
                            "build_exit_code": None,
                            "outcome": "corruption_detected"
                        })
                        fix_validated = False
                        break

                    # Si Aider falló completamente sin tocar nada, abortar
                    if exit_code != 0 and not meaningful_files:
                        logger.error(f"❌ Aider falló (código={exit_code}) en iteración {iteration_num} sin modificar archivos")
                        iteration_history.append({
                            "iteration": iteration_num,
                            "instruction_preview": current_instruction[:200],
                            "aider_exit_code": exit_code,
                            "aider_error": stderr_out[:300],
                            "files_modified": 0,
                            "build_exit_code": None,
                            "outcome": "aider_failed"
                        })
                        break

                    # ── Validation Gate ─────────────────────────────────────────────────
                    iter_build_exit_code = None
                    iter_build_output = ""

                    if build_cmd:
                        if build_tool == "nodejs" and iteration_num == 1:
                            # npm install solo una vez (primera iteración)
                            logger.info("📦 Node.js detectado. Ejecutando npm install...")
                            await asyncio.to_thread(
                                subprocess.run,
                                ["npm", "install"],
                                cwd=project_path,
                                capture_output=True,
                                timeout=120,
                                shell=True
                            )

                        logger.info(f"🔨 [{build_tool}] Iteración {iteration_num}: {' '.join(build_cmd)}")
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
                        iter_build_exit_code = build_result.get("exit_code", -1)
                        iter_build_output = (build_result.get("stdout") or "") + (build_result.get("stderr") or "")
                        build_exit_code = iter_build_exit_code
                        build_output = iter_build_output

                        if iter_build_exit_code == 0:
                            logger.info(f"✅ [{build_tool}] Build EXITOSO")
                            fix_validated = True
                            iteration_history.append({
                                "iteration": iteration_num,
                                "instruction_preview": current_instruction[:200],
                                "aider_exit_code": exit_code,
                                "files_modified": len(meaningful_files),
                                "build_exit_code": 0,
                                "outcome": "success"
                            })
                            break

                        else:
                            # Build falló — ¿hay más intentos?
                            logger.warning(f"⚠️ Iteración {iteration_num}: Build FALLÓ (código={iter_build_exit_code})")

                            # ── Safeguard 3: Detección de regresión de errores ────────
                            # Si el número de errores aumentó respecto a la iteración anterior,
                            # Aider está empeorando las cosas. Abortar en vez de seguir iterando.
                            _ansi_clean = _re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
                            _clean_output = _ansi_clean.sub('', iter_build_output)
                            _error_lines_now = [l for l in _clean_output.splitlines() if "error TS" in l or ("error" in l.lower() and l.strip().startswith("src"))]
                            _new_error_count = len(_error_lines_now)
                            _prev_error_count = len(baseline_errors) if iteration_num == 1 else (
                                len([l for l in _ansi_clean.sub('', iteration_history[-1].get("build_error_preview", "")).splitlines()
                                     if "error TS" in l]) if iteration_history else 0
                            )
                            _regression = (_new_error_count > _prev_error_count + 10) and (_new_error_count > len(baseline_errors) + 5)
                            if _regression:
                                logger.error(f"🚨 Safeguard: regresión masiva de errores detectada ({_prev_error_count} → {_new_error_count}). Abortando loop.")

                            iteration_history.append({
                                "iteration": iteration_num,
                                "instruction_preview": current_instruction[:200],
                                "aider_exit_code": exit_code,
                                "files_modified": len(meaningful_files),
                                "build_exit_code": iter_build_exit_code,
                                "build_error_preview": iter_build_output[-1500:],
                                "error_count": _new_error_count,
                                "outcome": "regression_abort" if _regression else "build_failed"
                            })

                            if _regression:
                                fix_validated = False
                                break  # ← Abortar: Aider está empeorando

                            if iteration_num < max_iterations:
                                # Preparar siguiente iteración con el error de build como contexto
                                logger.info(f"🧠 Preparando iteración {iteration_num + 1} con error de compilación como contexto...")
                                # Extraer solo los errores NUEVOS (excluir baseline)
                                _new_error_lines_raw = [
                                    l for l in _ansi_clean.sub('', iter_build_output).splitlines()
                                    if ("error TS" in l or ("error" in l.lower() and l.strip().startswith("src")))
                                ]
                                # Intenta normalizar para filtrar baseline
                                _new_error_lines = [
                                    l for l in _new_error_lines_raw
                                    if l.strip() not in baseline_errors
                                ]
                                if not _new_error_lines:
                                    _new_errors_text = iter_build_output[-2000:]
                                else:
                                    _new_errors_text = "\n".join(_new_error_lines[:15])

                                current_instruction = (
                                    f"## CONTEXTO: Iteración {iteration_num + 1}/{max_iterations}\n\n"
                                    f"**IMPORTANTE:** Los cambios del intento anterior YA ESTÁN APLICADOS. "
                                    f"NO vuelvas a hacer los cambios originales.\n\n"
                                    f"**Tu única tarea:** Corregir LOS SIGUIENTES ERRORES DE COMPILACIÓN sin modificar nada más:\n\n"
                                    f"```\n{_new_errors_text}\n```\n\n"
                                    f"**Instrucciones:**\n"
                                    f"1. PRESERVA todos los cambios existentes\n"
                                    f"2. Corrige SOLO las líneas que causan estos errores específicos\n"
                                    f"3. No elimines funcionalidad para 'arreglar' el build\n"
                                )
                            else:
                                fix_validated = False
                                logger.error(f"❌ Build falló en las {max_iterations} iteraciones. Iniciando rollback...")
                    else:
                        # Sin build tool conocido
                        logger.info("⚠️ No se detectó build tool conocido. Validación omitida.")
                        fix_validated = None
                        iteration_history.append({
                            "iteration": iteration_num,
                            "instruction_preview": current_instruction[:200],
                            "aider_exit_code": exit_code,
                            "files_modified": len(meaningful_files),
                            "build_exit_code": None,
                            "outcome": "no_build_tool"
                        })
                        break  # Sin validación, aceptar el cambio tal cual

                # ── Fin del loop — Rollback si fue necesario ──────────────────────────
                if fix_validated is False:
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
                                        f.write(f"# Build failed after {len(iteration_history)} iteration(s)\n")
                                        f.write(f"# --- ORIGINAL ---\n")
                                        f.write(original_content)
                                        f.write(f"\n# --- SUGGESTED (last Aider attempt) ---\n")
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
                    # ── Métricas del loop de reintentos ──
                    "iterations_count": len(iteration_history),
                    "iterations_max": max_iterations,
                    "iteration_history": iteration_history,
                }

                await report_to_cerebro(payload, failed=False)


            except Exception as e:
                logger.exception("Error en Autofix background")
                await report_to_cerebro({"error": str(e)}, failed=True)
            finally:
                # ── 🔓 Liberar bloqueo del proyecto ──
                if 'project_path' in locals() and project_path in _active_autofixes:
                    _active_autofixes.pop(project_path, None)
                    logger.info(f"🔓 [Executor] Bloqueo liberado para {project_path}")

        async def report_to_cerebro(payload: dict, failed: bool):
            cerebro_url = options.get("cerebro_url", "http://localhost:4000")
            req_id_stripped = cmd.request_id.split("-", 1)[-1] if cmd.request_id else ""
            event_type = f"{cmd.action}_failed" if failed else f"{cmd.action}_completed"
            
            payload["autofix_id"] = req_id_stripped # Por retrocompatibilidad (Dashboard usa este en vez del command ID a veces)
            payload["request_id"] = req_id_stripped
            payload["target"] = target_file
            
            try:
                import httpx
                import uuid
                from datetime import timezone
                async with httpx.AsyncClient() as client:
                    await client.post(f"{cerebro_url}/api/events", json={
                        "id": f"exec-{uuid.uuid4().hex[:8]}",
                        "source": "executor",
                        "type": event_type,
                        "severity": "error" if failed else "info",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
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
