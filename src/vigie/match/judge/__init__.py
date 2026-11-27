"""Juges LLM : un fichier par fournisseur, enregistrés dans ``base.REGISTRY``."""

from vigie.match.judge.base import REGISTRY
from vigie.match.judge.openrouter import create

REGISTRY["openrouter"] = create
