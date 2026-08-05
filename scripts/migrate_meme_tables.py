"""
One-time migration: add meme_posts, meme_reactions, meme_comments tables
for the Memes / Locker Room tab.

Run once: python3 migrate_meme_tables.py
"""

import sqlite3
from init_db import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("""
    CREATE TABLE IF NOT EXISTS meme_posts (
        id INTEGER PRIMARY KEY,
        manager_id INTEGER NOT NULL,
        post_type TEXT NOT NULL,
        image_path TEXT,
        link_url TEXT,
        link_type TEXT,
        embed_html TEXT,
        caption TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS meme_reactions (
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(post_id, manager_id, emoji),
        FOREIGN KEY (post_id) REFERENCES meme_posts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS meme_comments (
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL,
        manager_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (post_id) REFERENCES meme_posts(id),
        FOREIGN KEY (manager_id) REFERENCES managers(id)
    )
""")

conn.commit()
print("meme_posts, meme_reactions, meme_comments tables created.")
conn.close()
