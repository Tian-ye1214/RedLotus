"""Small construction boundary shared by terminal control and the desktop child."""
from .model import PetModel, SpritePet
from .service import PetService, ProcessPetService


class PetFactory:
    @staticmethod
    def service() -> PetService:
        return ProcessPetService()

    @staticmethod
    async def model(character: str) -> PetModel:
        return await SpritePet.load(character)
