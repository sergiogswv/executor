import asyncio
import logging
import os
import uuid
import subprocess
import platform
from datetime import datetime, timezone
from pathlib import Path
from app.models import ServiceDefinition, TerminalInfo

logger = logging.getLogger("ejecutor.process_manager")

# Directorio donde se guardan los logs de cada terminal
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)


def _normalize_command(command: str, cwd: str, shell: bool) -> str:
    r"""
    Normaliza un comando para multiplataforma (Windows/Linux/Mac).

    - Python: ajusta el intérprete (python3 en Linux/Mac, python en Windows)
    - Binarios: detecta automáticamente la extensión (.exe en Windows, sin extensión en Linux/Mac)
    - Rutas: convierte todos los slashes (/ → \) en Windows
    """
    system = platform.system()
    cwd_path = Path(cwd)

    logger.debug(f"🔧 _normalize_command: command={command}, cwd={cwd}, system={system}")

    # === Paso 1: Normalizar todos los slashes según la plataforma ===
    if system == "Windows":
        # Convertir todos los / a \ para Windows
        command = command.replace("/", "\\")
        # Reemplazar python3 por python (Windows no tiene python3 por defecto)
        command = command.replace("python3", "python")
    else:
        # En Unix, asegurar que los slashes sean /
        command = command.replace("\\", "/")

    logger.debug(f"🔧 Después de normalizar slashes: {command}")

    # === Paso 2: Ajustes específicos de Python en Windows ===
    if system == "Windows":
        # Si el comando usa venv\Scripts\python.exe pero no existe, intentar con python del PATH
        if "venv\\Scripts\\python" in command or ".venv\\Scripts\\python" in command:
            venv_python_win = cwd_path / "venv" / "Scripts" / "python.exe"
            venv_python_dot = cwd_path / ".venv" / "Scripts" / "python.exe"

            if not venv_python_win.exists() and not venv_python_dot.exists():
                command = command.replace(".venv\\Scripts\\python.exe", "python")
                command = command.replace("venv\\Scripts\\python.exe", "python")

    # === Paso 3: Detección automática de binarios y agregar .exe en Windows ===
    parts = command.split()
    if parts:
        first_part = parts[0]
        # Solo procesar si parece una ruta relativa (contiene separador de ruta)
        separator = "\\" if system == "Windows" else "/"
        if separator in first_part:
            # Ruta relativa desde cwd
            bin_path = cwd_path / first_part

            # Intentar con la ruta exacta primero
            if bin_path.exists() and bin_path.is_file():
                logger.debug(f"🔧 Binario encontrado: {bin_path}")
                return command

            # En Windows, intentar con .exe
            if system == "Windows" and not first_part.lower().endswith(".exe"):
                bin_path_exe = cwd_path / (first_part + ".exe")
                if bin_path_exe.exists():
                    command = command.replace(first_part, first_part + ".exe", 1)
                    logger.debug(f"🔧 Binario detectado (.exe agregada): {first_part} → {first_part}.exe")
                    return command

            # En Windows, también intentar en la carpeta release/debug con .exe
            if system == "Windows":
                # Intentar variante .exe en la misma ruta
                base_with_exe = first_part + ".exe"
                bin_path_alt = cwd_path / base_with_exe
                if bin_path_alt.exists():
                    command = command.replace(first_part, base_with_exe, 1)
                    logger.debug(f"🔧 Binario alternativo encontrado: {base_with_exe}")
                    return command

    logger.debug(f"🔧 _normalize_command final: {command}")
    return command


def _normalize_env(env: dict[str, str] | None, cwd: str) -> dict[str, str]:
    r"""
    Normaliza variables de entorno para multiplataforma.

    - Resuelve rutas relativas a absolutas
    - Windows: usa \ como separador y Scripts/
    - Unix: usa / como separador y bin/
    """
    if not env:
        return {}

    result = env.copy()
    cwd_path = Path(cwd)
    system = platform.system()

    # Si hay PATH, convertir rutas relativas a absolutas
    if "PATH" in result and result["PATH"]:
        separator = ";" if system == "Windows" else ":"
        scripts_dir = "Scripts" if system == "Windows" else "bin"

        path_parts = result["PATH"].split(separator)
        normalized_parts = []
        for part in path_parts:
            # Detectar rutas relativas de venv (.venv/Scripts, ../cerebro/.venv/Scripts, etc.)
            if "venv" in part.lower() and (scripts_dir in part or "bin" in part):
                # Normalizar separadores
                part_normalized = part.replace("/", "\\").replace("\\", "/")
                full_path = cwd_path / part_normalized.replace("/", "\\")
                if full_path.exists():
                    # Convertir a ruta absoluta nativa
                    native_path = str(full_path.resolve())
                    if system != "Windows":
                        native_path = native_path.replace("\\", "/")
                    normalized_parts.append(native_path)
                else:
                    normalized_parts.append(part)
            else:
                normalized_parts.append(part)
        result["PATH"] = separator.join(normalized_parts)

    # Si hay VIRTUAL_ENV, convertir a ruta absoluta
    if "VIRTUAL_ENV" in result:
        venv_path = result["VIRTUAL_ENV"]
        # Solo procesar si es ruta relativa (no empieza con / o letra: en Windows)
        if not Path(venv_path).is_absolute():
            full_venv = cwd_path / venv_path.replace("/", "\\")
            if full_venv.exists():
                resolved = str(full_venv.resolve())
                if system != "Windows":
                    resolved = resolved.replace("\\", "/")
                result["VIRTUAL_ENV"] = resolved

    return result


class ProcessManager:
    """
    Gestiona los procesos (terminales) abiertos por el Ejecutor.
    Cada proceso tiene un terminal_id único y se puede cerrar individualmente.
    """

    def __init__(self):
        # terminal_id → (process, TerminalInfo, log_files)
        self._terminals: dict[str, tuple[asyncio.subprocess.Process, TerminalInfo, list]] = {}

    def _is_port_in_use(self, port: int) -> bool:
        """Verifica si un puerto TCP está ocupado en localhost."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            # connect_ex retorna 0 si la conexión fue exitosa (puerto ocupado)
            return s.connect_ex(('127.0.0.1', port)) == 0

    async def open(self, service_key: str, service: ServiceDefinition) -> TerminalInfo:
        """
        Abre una nueva terminal ejecutando el comando del servicio.
        stdout/stderr se redirigen a archivos de log para evitar bloqueos.
        Verifica si el puerto está libre antes de iniciar.
        """
        # VALIDACIÓN DE PUERTO (Skrymir se asegura de que esté libre)
        if service.port and self._is_port_in_use(service.port):
            logger.warning(f"⚠️  Puerto {service.port} para '{service_key}' ya está en uso.")
            import platform
            import subprocess
            if platform.system() == "Linux":
                logger.info(f"🎯 Intentando forzar liberación del puerto {service.port}...")
                subprocess.run(["fuser", "-k", f"{service.port}/tcp"], capture_output=True)
                await asyncio.sleep(1.0) # Esperar a que el SO libere el puerto
            elif platform.system() == "Windows":
                logger.info(f"🎯 Buscando proceso que ocupa el puerto {service.port}...")
                try:
                    # Buscar el PID que usa el puerto
                    output = subprocess.check_output(f'netstat -ano | findstr ":{service.port}.*LISTENING"', shell=True).decode()
                    if output:
                        # La salida suele ser: TCP 0.0.0.0:4001 0.0.0.0:0 LISTENING 1234
                        parts = output.strip().split()
                        pid = parts[-1]
                        if pid and pid != "0":
                            logger.info(f"💥 Matando proceso {pid} que ocupa el puerto {service.port}...")
                            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                            await asyncio.sleep(1.5) # Windows tarda un poco más
                except Exception as e:
                    logger.debug(f"No se pudo liberar el puerto {service.port}: {e}")

        terminal_id = f"{service_key}-{uuid.uuid4().hex[:8]}"

        # Construir environment
        env = os.environ.copy()
        env.update(service.env)
        # Normalizar environment para multiplataforma (rutas absolutas en Windows)
        env = _normalize_env(env, service.cwd)

        # Archivos de log para este terminal
        log_stdout = open(LOGS_DIR / f"{terminal_id}.stdout.log", "w")
        log_stderr = open(LOGS_DIR / f"{terminal_id}.stderr.log", "w")

        import subprocess

        # Normalizar comando para multiplataforma
        command = _normalize_command(service.command, service.cwd, service.shell)

        logger.info(f"🔧 [{service_key}] Comando: {command}")
        logger.info(f"   CWD: {service.cwd}")

        # Construir y ejecutar el comando usando subprocess tradicional
        # subprocess.Popen no bloquea, por lo que es seguro llamarlo en el thread principal
        if service.shell:
            proc = subprocess.Popen(
                command,
                cwd=service.cwd,
                env=env,
                stdout=log_stdout,
                stderr=log_stderr,
                shell=True
            )
        else:
            args = command.split()
            proc = subprocess.Popen(
                args,
                cwd=service.cwd,
                env=env,
                stdout=log_stdout,
                stderr=log_stderr,
            )

        info = TerminalInfo(
            terminal_id=terminal_id,
            service=service_key,
            service_name=service.name,
            pid=proc.pid,
            port=service.port,
            command=service.command,
            cwd=service.cwd,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        self._terminals[terminal_id] = (proc, info, [log_stdout, log_stderr])

        logger.info(
            f"🟢 [{service_key}] Levantado | terminal_id={terminal_id} "
            f"pid={proc.pid} cwd={service.cwd}"
        )

        # Monitoreo en background
        asyncio.create_task(self._monitor(terminal_id))

        return info

    async def run_once(self, service_key: str, service: ServiceDefinition, command_list: list[str] = None, timeout: int = 120, report_to_cerebro: bool = True) -> dict:
        """
        Ejecuta un comando de una sola vez y streamea la salida en tiempo real.
        """
        import platform
        from pathlib import Path
        import asyncio
        import httpx
        from app.config import get_settings

        settings = get_settings()
        env = os.environ.copy()
        env.update(service.env)
        env = _normalize_env(env, service.cwd)

        # Preparar comando
        if command_list is None:
            command = _normalize_command(service.command, service.cwd, service.shell)
            cmd_args = command.split()
        else:
            cmd_args = command_list.copy()
            if cmd_args and platform.system() == "Windows":
                bin_path = Path(service.cwd) / cmd_args[0].replace("/", "\\")
                if not Path(cmd_args[0]).is_absolute() and bin_path.exists():
                    cmd_args[0] = str(bin_path)
                elif not Path(cmd_args[0]).is_absolute():
                    bin_path_exe = Path(service.cwd) / (cmd_args[0] + ".exe")
                    if bin_path_exe.exists():
                        cmd_args[0] = str(bin_path_exe)

        logger.info(f"🏃 Ejecución asíncrona one-shot en {service.cwd}: {cmd_args}")

        log_buffer = []
        last_report_time = asyncio.get_event_loop().time()

        async def flush_logs():
            """Envía el búfer acumulado a Cerebro."""
            nonlocal log_buffer, last_report_time
            if not log_buffer: return
            
            combined_msg = "\n".join(log_buffer)
            log_buffer = []
            last_report_time = asyncio.get_event_loop().time()
            
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(f"{settings.cerebro_url}/api/events", json={
                        "source": "executor",
                        "type": "executor_log",
                        "severity": "info",
                        "payload": {
                            "message": combined_msg,
                            "service": service_key,
                            "is_milestone": any(kw in combined_msg for kw in ["Successful", "Commit", "Fixed", "Applied"])
                        }
                    }, timeout=1.5)
            except: pass

        async def send_log_event(msg: str):
            """Helper para acumular y reportar progreso de forma muy agrupada."""
            if not report_to_cerebro: return
            cleaned = msg.strip()
            if not cleaned or len(cleaned) < 2: return
            
            # Filtrar ruido de barras de progreso o caracteres de escape de terminal
            if any(x in cleaned for x in ["\b", "\r", "]]>"]): return
            
            log_buffer.append(cleaned)
            
            now = asyncio.get_event_loop().time()
            # Flush solo si el búfer es MUY grande, ha pasado mucho tiempo (10s), o es el hito final
            is_final_milestone = any(kw in cleaned for kw in ["Successfully", "Commit completed"])
            
            # Subimos umbrales: 25 líneas o 10 segundos de espera
            if is_final_milestone or len(log_buffer) > 25 or (now - last_report_time) > 10.0:
                await flush_logs()

        try:
            # Iniciar proceso con pipes para stdout/stderr
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=service.cwd,
                env=env
            )

            stdout_chunks = []
            stderr_chunks = []

            async def read_stream(stream, collection, is_stderr=False):
                while True:
                    line = await stream.readline()
                    if not line: break
                    try:
                        decoded = line.decode('utf-8', errors='ignore').strip()
                        collection.append(decoded)
                        if decoded:
                            await send_log_event(decoded)
                    except: pass

            # Leer ambos streams en paralelo
            await asyncio.gather(
                read_stream(proc.stdout, stdout_chunks),
                read_stream(proc.stderr, stderr_chunks, is_stderr=True)
            )

            # Flush final para no perder nada
            await flush_logs()

            # Esperar a que el proceso termine con el timeout
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(f"⏰ Timeout agotado ({timeout}s)")
                try: proc.kill()
                except: pass
                return {"exit_code": -1, "status": "timeout", "error": f"Timeout ({timeout}s)"}

            full_stdout = "\n".join(stdout_chunks)
            full_stderr = "\n".join(stderr_chunks)

            return {
                "exit_code": exit_code,
                "stdout": full_stdout[-10000:],
                "stderr": full_stderr[-10000:],
                "status": "completed" if exit_code == 0 else "failed"
            }

        except Exception as e:
            logger.exception("Error en run_once asíncrono")
            return {"exit_code": -1, "error": str(e), "status": "error"}

    async def close(self, terminal_id: str) -> bool:
        """
        Termina un proceso por su terminal_id de forma garantizada.
        Retorna True si se cerró, False si no existía.
        """
        entry = self._terminals.get(terminal_id)
        if not entry:
            logger.warning(f"⚠️  Terminal {terminal_id} no encontrada")
            return False

        proc, info, log_files = entry
        import platform
        import subprocess
        
        logger.info(f"⏳ Cerrando {info.service} (PID {proc.pid})...")

        if platform.system() == "Windows":
            # Matar el árbol de procesos en Windows (necesario si se usó shell=True)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            # Linux: Matar hijos primero para evitar procesos "zombie" o colgados
            try:
                subprocess.run(["pkill", "-9", "-P", str(proc.pid)], capture_output=True)
            except Exception:
                pass
            
            try:
                proc.terminate() # Intento amable
                # Dar tiempo a que termine
                for _ in range(10):
                    if proc.poll() is not None: break
                    await asyncio.sleep(0.1)
                
                if proc.poll() is None:
                    proc.kill() # Forzado
            except Exception:
                pass

        # Cerrar archivos de log
        for f in log_files:
            try:
                f.close()
            except Exception:
                pass

        info.status = "stopped"
        if terminal_id in self._terminals:
            del self._terminals[terminal_id]
            
        logger.info(f"🔴 [{info.service}] Detenido | terminal_id={terminal_id}")

        # VALIDACIÓN DE LIBERACIÓN DE PUERTO
        if info.port and self._is_port_in_use(info.port):
            logger.warning(f"⚠️  [{info.service}] El puerto {info.port} sigue ocupado tras el cierre.")
        elif info.port:
            logger.info(f"✅ [{info.service}] Puerto {info.port} liberado correctamente.")
            
        return True

    async def kill_all(self) -> list[str]:
        """Termina todos los procesos activos. Retorna lista de terminal_ids cerrados."""
        ids = list(self._terminals.keys())
        for tid in ids:
            await self.close(tid)
        logger.info(f"💥 kill-all — {len(ids)} terminales cerradas")
        return ids

    def status(self, terminal_id: str) -> TerminalInfo | None:
        """Retorna el estado de una terminal específica."""
        entry = self._terminals.get(terminal_id)
        return entry[1] if entry else None

    def list_all(self) -> list[TerminalInfo]:
        """Retorna todas las terminales activas."""
        return [info for _, info, _ in self._terminals.values()]

    def find_by_service(self, service_key: str) -> list[TerminalInfo]:
        """Retorna todas las terminales de un servicio específico."""
        return [
            info for _, info, _ in self._terminals.values()
            if info.service == service_key
        ]

    async def _monitor(self, terminal_id: str) -> None:
        """Monitorea en background si el proceso termina inesperadamente."""
        entry = self._terminals.get(terminal_id)
        if not entry:
            return

        proc, info, log_files = entry
        try:
            await asyncio.to_thread(proc.wait)
        except Exception as e:
            logger.error(f"Error esperando proc.wait(): {e}")

        if terminal_id in self._terminals:
            # Cerrar log files
            for f in log_files:
                f.close()
            info.status = "stopped"
            del self._terminals[terminal_id]
            if proc.returncode != 0:
                logger.warning(
                    f"⚠️  [{info.service}] Proceso terminó con exit code {proc.returncode} "
                    f"| terminal_id={terminal_id}"
                )
            else:
                logger.info(f"✅ [{info.service}] Proceso terminó OK | terminal_id={terminal_id}")
