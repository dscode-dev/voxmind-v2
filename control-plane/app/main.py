import logging
from .bot import VoxmindBot
from .health_server import ControlPlaneHealth, start_health_server
from .settings import settings

logging.basicConfig(level=logging.INFO)

def main():
    health = ControlPlaneHealth()
    start_health_server(
        health=health,
        host=settings.health_host,
        port=settings.health_port,
    )
    bot = VoxmindBot()
    bot.run(health=health)

if __name__ == "__main__":
    main()
