import yaml
import logging
from pathlib import Path
from app.models import ServiceDefinition

logger = logging.getLogger("ejecutor.registry")


class ServiceRegistry:
    """
    Carga y expone el catálogo de servicios desde services.yaml.
    El Ejecutor consulta aquí antes de abrir cualquier terminal.

    Resuelve rutas relativas automáticamente para multiplataforma.
    """

    def __init__(self, config_path: str = "services.yaml"):
        self._services: dict[str, ServiceDefinition] = {}
        self._path = Path(config_path).resolve()
        self._base_dir = self._path.parent  # Directorio base para rutas relativas
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning(f"⚠️  services.yaml no encontrado en {self._path.resolve()}")
            return

        with open(self._path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        services_raw = raw.get("services", {})
        for key, data in services_raw.items():
            # Normalizar cwd a ruta absoluta (para multiplataforma)
            cwd_path = Path(data.get("cwd", ".")).resolve()
            # Si es relativa, resolverla desde el directorio del services.yaml
            if not cwd_path.is_absolute():
                cwd_path = self._base_dir / cwd_path

            data["cwd"] = str(cwd_path)
            self._services[key] = ServiceDefinition(**data)

        logger.info(f"✅ {len(self._services)} servicios cargados: {list(self._services.keys())}")
        logger.debug(f"   Base dir: {self._base_dir}")
        for key, svc in self._services.items():
            logger.debug(f"   [{key}] cwd={svc.cwd}")

    def get(self, service_key: str) -> ServiceDefinition | None:
        return self._services.get(service_key)

    def list_all(self) -> dict[str, ServiceDefinition]:
        return dict(self._services)

    def reload(self) -> None:
        """Recarga el archivo sin reiniciar el servidor."""
        self._services.clear()
        self._load()
