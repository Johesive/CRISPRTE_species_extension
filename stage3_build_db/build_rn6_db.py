import sys
import os

BASE_DIR = "/mnt/volume5/joh_crisprte"
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src', 'azimuth'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'PyExtensions', 'Trie'))

from trie2db import trie2db_1, trie2db_2

FA_PATH   = "/mnt/volume5/joh_crisprte/rn6.fa"
DB_PATH   = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"
TRIE_PATH = "/mnt/volume5/joh_crisprte/rn6.crisprte.trie.data"
GRNA_TSV  = FA_PATH + ".gRNA.tsv"

print("=== Step 1: Build Trie ===")
t = trie2db_1(FA_PATH, DB_PATH, TRIE_PATH)

print("=== Step 2: Annotate & Score ===")
trie2db_2(t, GRNA_TSV, DB_PATH)

print("All done!")