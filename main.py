"""Modular entry point.
Render continues to use runner.py for zero-disruption deployment.
This module exposes the same live application builder when imported.
"""
import bot
import runner

build_app = runner.build_app
run = runner.run
init_db = bot.init_db

if __name__ == "__main__":
    run()
