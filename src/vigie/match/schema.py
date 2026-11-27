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

# Annotations (lot M2) : une étiquette par offre et par version du profil.
LABELS_MIGRATION = """
CREATE TABLE labels (
    uid TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    label TEXT NOT NULL,
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (uid, profile_hash)
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

# Un enregistrement par appel au juge (réponses invalides et échecs compris) : jetons d'entrée et de
# sortie renvoyés par l'API, jamais le texte envoyé ni la réponse. ``outcome`` : ok, invalid, error.
LLM_CALLS_MIGRATION = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_llm_calls_run ON llm_calls (run_id);
"""
