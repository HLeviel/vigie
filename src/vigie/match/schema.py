"""Migrations SQL des tables de ``match``.

Les migrations d'une même base ``radar.db`` forment une seule suite versionnée par
``PRAGMA user_version`` : la liste est assemblée dans ``vigie.radar.store``. Ce fichier n'importe
rien, pour éviter un cycle entre les deux modules.
"""

# Clé : (owner, model, text_hash). ``owner`` vaut ``offer:<uid>`` ou ``profile:<hash>:<n>``.
EMBEDDINGS_MIGRATION = """
CREATE TABLE embeddings (
    owner TEXT NOT NULL,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner, model, text_hash)
);
"""

# Clé : (uid, profile_hash, stage, model, prompt_version). Étage ``hybrid`` : ``prompt_version``
# porte la signature du réglage du scoring (poids, pénalités, k) ; étage ``llm`` (M3) : la version
# du prompt.
SCORES_MIGRATION = """
CREATE TABLE scores (
    uid TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    score REAL NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, profile_hash, stage, model, prompt_version)
);
"""
