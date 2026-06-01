from abc import ABC, abstractmethod


class Corrector(ABC):
    """Optionale Nachkorrektur-Stufe nach der Transkription.

    Schickt ein fertiges Transkript durch eine Korrektur (z. B. ein lokales
    LLM) und gibt den korrigierten Text zurück.
    """

    @abstractmethod
    def correct(self, text: str) -> str:
        """Gibt den korrigierten Text zurück. Darf bei Fehlern eine
        Exception werfen — der Aufrufer fängt sie ab (siehe Plan §6)."""
