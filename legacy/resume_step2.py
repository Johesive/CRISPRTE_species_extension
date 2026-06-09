import sys
import os

BASE_DIR = "/mnt/volume5/joh_crisprte"
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src', 'azimuth'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'PyExtensions', 'Trie'))

from trie2db import trie2db_2
import trie

FA_PATH   = "/mnt/volume5/joh_crisprte/rn6.fa"
DB_PATH   = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"
TRIE_PATH = "/mnt/volume5/joh_crisprte/rn6.crisprte.trie.data"
GRNA_TSV  = FA_PATH + ".gRNA.tsv"

print("=== Step 2: Loading Trie (FAST) ===")
t = trie.load_fast(TRIE_PATH)

print("=== Step 2: Annotate & Score ===")
trie2db_2(t, GRNA_TSV, DB_PATH)
print("All done!")
