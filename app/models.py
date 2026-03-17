from pydantic import BaseModel
from typing import Any


# ─── Definición de un servicio (desde services.yaml) ─────────────────────────

class ServiceDefinition(BaseModel):
    name: str
    command: str
    cwd: str
    port: int | None = None
    shell: bool = False
    env: dict[str, str] = {}


# ─── Comandos entrantes (Cerebro → Ejecutor) ─────────────────────────────────

class ExecutorCommand(BaseModel):
    action: str           # "open" | "close" | "status" | "list" | "kill-all"
    service: str | None = None      # nombre del servicio a abrir/cerrar (ej: "warden")
    terminal_id: str | None = None  # ID de terminal específica (para close)
    request_id: str | None = None


# ─── Estado de una terminal abierta ──────────────────────────────────────────

class TerminalInfo(BaseModel):
    terminal_id: str
    service: str
    service_name: str
    pid: int
    command: str
    cwd: str
    status: str     # "running" | "stopped" | "error"
    started_at: str


# ─── Respuesta del Ejecutor (al Cerebro) ─────────────────────────────────────

class CommandAck(BaseModel):
    request_id: str | None = None
    status: str     # "accepted" | "completed" | "rejected"
    result: Any | None = None
    error: str | None = None


# ─── Respuesta genérica API ───────────────────────────────────────────────────

class ApiResponse(BaseModel):
    ok: bool = True
    message: str = "ok"
    data: Any | None = None
