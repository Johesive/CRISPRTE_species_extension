import sys
import os
import pandas as pd
import numpy as np
import tqdm
import json

BASE_DIR = "/mnt/volume5/joh_crisprte"
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src', 'azimuth'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src'))

# Mock the Tm_staluc for Azimuth
import Bio.SeqUtils.MeltingTemp as Tm
if not hasattr(Tm, 'Tm_staluc'):
    Tm.Tm_staluc = lambda seq, rna=False, **kwargs: Tm.Tm_NN(seq)

from dbutils import SQLite
from grna_scoring import calculate_moreno_mateos
from azimuth.model_comparison import predict

DB_PATH   = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"
sql = SQLite()

def resume_db_scoring(db_path):
    print("=== Fast Resume: Direct Scoring (No Trie Load) ===")
    conn = sql.connect(db_path)
    cursor = conn.cursor()
    
    import warnings
    warnings.filterwarnings("ignore")
    
    print("Scanning for annotated CSV chunks...")
    csv_files = sorted([f for f in os.listdir(BASE_DIR) if f.startswith("rn6.gRNA.annotated.") and f.endswith(".csv")])
    if not csv_files:
        print("No CSV files found! You have to annotate first.")
        return

    print(f"Found {len(csv_files)} chunk files. Proceeding to score them natively.")
    
    for csv_file in csv_files:
        filepath = os.path.join(BASE_DIR, csv_file)
        print(f"\nProcessing {csv_file}...")
        
        df_chunk = pd.read_csv(filepath, index_col=0)
        df_chunk.insert(5, "gscore_moreno", np.nan)
        df_chunk.insert(6, "gscore_azimuth", np.nan)
        
        # Now score this chunk in pieces of 50000
        for j in tqdm.trange(0, len(df_chunk), 50000):
            df_sub = df_chunk.iloc[j:j+50000, :]
            
            gids = df_sub.iloc[:, 1].tolist()
            gid_str = ','.join(map(str, gids))
            if gid_str:
                cursor.execute(f"SELECT gid, gseq FROM table2 WHERE gid IN ({gid_str})")
                seq_map = dict(cursor.fetchall())
            else:
                seq_map = {}
            
            rows = []
            seqs = []
            for i in range(len(df_sub)):
                row = list(df_sub.iloc[i, :])
                seq = seq_map.get(row[1], '')
                seqs.append(seq)
                row[5] = calculate_moreno_mateos(seq, row[2], row[3], row[4])
                rows.append(row)
            
            # Filter N / invalid
            valid_indices = []
            valid_inputs = []
            all_seqs = list(map(lambda x: rows[x[1]][3][-4:] + x[0] + rows[x[1]][2] + rows[x[1]][4][:3], zip(seqs, range(len(rows)))))
            
            for idx, full_seq in enumerate(all_seqs):
                full_seq = full_seq.upper()
                if 'N' in full_seq or len(full_seq) != 30:
                    rows[idx][6] = 0.0
                else:
                    valid_indices.append(idx)
                    valid_inputs.append(full_seq)
            
            if valid_inputs:
                az = []
                for b_start in range(0, len(valid_inputs), 5000):
                    b_end = min(len(valid_inputs), b_start + 5000)
                    az.extend(predict(np.array(valid_inputs[b_start:b_end])))
                for v_idx, score in zip(valid_indices, az):
                    rows[v_idx][6] = score

            cursor.executemany("INSERT INTO table1 (pos, gid, pam, upstream, downstream, gscore_moreno, gscore_azimuth, anno_class, te_class, te_dup) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()

    print("All chunks scored and inserted! (Note: Off-target prediction with Trie is skipped in this script)")

if __name__ == "__main__":
    resume_db_scoring(DB_PATH)
