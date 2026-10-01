"""Small construction boundary shared by terminal control and the desktop child."""
import argparse
from pathlib import Path

from .model import PetModel, SpritePet
from .service import PetService, ProcessPetService


class PetFactory:
    @staticmethod
    def options(argv):
        parser = argparse.ArgumentParser(prog="redlotus-pets", exit_on_error=False)
        parser.add_argument("character", nargs="?", default="charcoal")
        parser.add_argument("--resource-dir", type=Path)
        parser.add_argument("--scale", type=float, default=1.)
        args = parser.parse_args(argv)
        if args.resource_dir is not None and (not args.resource_dir.is_absolute() or args.resource_dir.name != args.character):
            raise ValueError("Pet resource directory must be absolute and match the character ID")
        return args

    @staticmethod
    def service() -> PetService:
        return ProcessPetService()

    @staticmethod
    async def model(character: str) -> PetModel:
        return await SpritePet.load(character)
