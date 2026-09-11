"""The console's five views."""

from .activity import ActivityView
from .candidates import CandidatesView
from .cardlab import CardLabView
from .corpus import CorpusView
from .experiments import ExperimentsView

__all__ = [
    "ActivityView",
    "CandidatesView",
    "CardLabView",
    "CorpusView",
    "ExperimentsView",
]
