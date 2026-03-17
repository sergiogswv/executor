import sys
import asyncio
import uvicorn
from app.config import get_settings

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        loop="asyncio",
        log_level=settings.log_level.lower(),
    )

