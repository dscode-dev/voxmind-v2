import logging
from .bot import VoxmindBot

logging.basicConfig(level=logging.INFO)

def main():
    bot = VoxmindBot()
    bot.run()

if __name__ == "__main__":
    main()