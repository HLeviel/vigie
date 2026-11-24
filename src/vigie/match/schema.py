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
