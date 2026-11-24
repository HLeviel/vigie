"""Fournisseurs d'embeddings : un fichier par fournisseur, enregistrés dans ``base.REGISTRY``."""

from vigie.match.embedders.base import REGISTRY
from vigie.match.embedders.bedrock import BedrockEmbedder
from vigie.match.embedders.local import LocalEmbedder

REGISTRY["local"] = LocalEmbedder
REGISTRY["bedrock"] = BedrockEmbedder
