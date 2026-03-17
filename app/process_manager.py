import asyncio
import logging
import os
import uuid
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from app.models import ServiceDefinition, TerminalInfo

logger = logging.getLogger("ejecutor.process_manager")

# Directorio donde se guardan los logs de cada terminal
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)


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

        terminal_id = f"{service_key}-{uuid.uuid4().hex[:8]}"

        # Construir environment
        env = os.environ.copy()
        env.update(service.env)

        # Archivos de log para este terminal
        log_stdout = open(LOGS_DIR / f"{terminal_id}.stdout.log", "w")
        log_stderr = open(LOGS_DIR / f"{terminal_id}.stderr.log", "w")

        import subprocess

        # Construir y ejecutar el comando usando subprocess tradicional
        # subprocess.Popen no bloquea, por lo que es seguro llamarlo en el thread principal
        if service.shell:
            proc = subprocess.Popen(
                service.command,
                cwd=service.cwd,
                env=env,
                stdout=log_stdout,
                stderr=log_stderr,
                shell=True
            )
        else:
            args = service.command.split()
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

    async def run_once(self, service_key: str, service: ServiceDefinition, command_list: list[str] = None) -> dict:
        """
        Ejecuta un comando de una sola vez y espera a que termine.
        Retorna un dict con el status, exit_code y salida resumida.
        """
        import subprocess
        
        env = os.environ.copy()
        env.update(service.env)
        
        cmd = command_list if command_list else service.command.split()
        
        logger.info(f"🏃 Ejecución one-shot en {service.cwd}: {cmd}")
        
        try:
            # Usamos subprocess.run para bloquear (dentro de thread) y capturar
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=service.cwd,
                env=env,
                capture_output=True,
                text=True,
                shell=service.shell,
                timeout=30 # Timeout para evitar bloqueos infinitos
            )
            
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-1000:], # Últimos 1000 chars
                "stderr": proc.stderr[-1000:],
                "status": "completed" if proc.returncode == 0 else "failed"
            }
        except subprocess.TimeoutExpired as te:
            logger.error("⏰ Timeout agotado en ejecución one-shot")
            return {
                "exit_code": -1,
                "error": "Timeout agotado (30s)",
                "stdout": te.stdout[-500:] if te.stdout else "",
                "stderr": te.stderr[-500:] if te.stderr else "",
                "status": "timeout"
            }
        except Exception as e:
            logger.exception("Error en run_once")
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
