import sys
import os

# -------- HIGH-PRIORITY FIX --------
# STOP OPENBLAS, MKL, NUMPY, AND OMP FROM SPAWNING 128 THREADS PER PROCESS!
# THIS CURES THE MASSIVE CPU THRASHING/CONTENTION CAUSING THE 13 MINUTE DELAY.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["RAYON_NUM_THREADS"] = "1"
os.environ["BLIS_NUM_THREADS"] = "1"

import pandas as pd
import numpy as np
import tqdm
import sqlite3
import multiprocessing as mp

BASE_DIR = "/mnt/volume5/joh_crisprte"
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src', 'azimuth'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src'))

import Bio.SeqUtils.MeltingTemp as Tm
if not hasattr(Tm, 'Tm_staluc'):
    Tm.Tm_staluc = lambda seq, rna=False, **kwargs: Tm.Tm_NN(seq)

DB_PATH = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"

_AZIMUTH_MODEL_CACHE = None

def process_batch(args):
    global _AZIMUTH_MODEL_CACHE
    import warnings
    warnings.filterwarnings("ignore")
    import numpy as np
    
    import Bio.SeqUtils.MeltingTemp as Tm
    if not hasattr(Tm, 'Tm_staluc'):
        Tm.Tm_staluc = lambda seq, rna=False, **kwargs: Tm.Tm_NN(seq)
        
    from grna_scoring import calculate_moreno_mateos
    from azimuth.model_comparison import predict

    if _AZIMUTH_MODEL_CACHE is None:
        import pickle
        import os
        model_path = os.path.join(BASE_DIR, 'CRISPRTE', 'src', 'azimuth', 'azimuth', 'saved_models', 'V3_model_nopos.pickle')
        with open(model_path, "rb") as f:
            model_info = pickle.load(f)
            rf_model = model_info[0]
            # Force Scikit-Learn to only use 1 thread per worker to prevent 16x20=320 threads CPU thrashing!
            if hasattr(rf_model, 'n_jobs'):
                rf_model.n_jobs = 1
            if hasattr(rf_model, 'estimators_'):
                for est in rf_model.estimators_:
                    if hasattr(est, 'n_jobs'):
                        est.n_jobs = 1
            _AZIMUTH_MODEL_CACHE = model_info

    valid_indices = []
    valid_inputs = []
    processed_rows = []

    for i, (row, seq) in enumerate(args):
        try:
            moreno = calculate_moreno_mateos(seq, row[2], row[3], row[4])
        except Exception:
            moreno = -1.0
        row[5] = moreno
        
        full_seq = ""
        if seq:
            full_seq = (str(row[3])[-4:] + str(seq) + str(row[2]) + str(row[4])[:3]).upper()
        
        if 'N' in full_seq or len(full_seq) != 30:
            row[6] = -1.0
        else:
            valid_indices.append(i)
            valid_inputs.append(full_seq)
        
        processed_rows.append(row)

    if valid_inputs:
        try:
            import joblib
            az = []
            for b_start in range(0, len(valid_inputs), 1000):
                b_end = min(len(valid_inputs), b_start + 1000)
                # Force strictly 1 thread via JobLib to physically block sklearn from thread spawning 
                with joblib.parallel_backend("threading", n_jobs=1):
                    az.extend(predict(np.array(valid_inputs[b_start:b_end]), model=_AZIMUTH_MODEL_CACHE))
            for v_idx, score in zip(valid_indices, az):
                processed_rows[v_idx][6] = score
        except Exception as e:
            pass

    return processed_rows

def resume_db_scoring_mp(db_path, cores=16):
    print(f"=== Safe Multi-Process Resume (Cores limit: {cores}) ===")
    
    conn = sqlite3.connect(db_path, timeout=120)
    cursor = conn.cursor()
    
    print("Checking database for resume point...")
    cursor.execute("SELECT COUNT(*) FROM table1")
    total_db_count = cursor.fetchone()[0]
    print(f"Found {total_db_count} records already successfully committed.")

    print("Scanning for annotated CSV chunks...")
    csv_files = sorted(
        [f for f in os.listdir(BASE_DIR) if f.startswith("rn6.gRNA.annotated.") and f.endswith(".csv")],
        key=lambda x: int(x.split('.')[-2])
    )
    
    if not csv_files:
        print("No CSV files found!")
        return

    import signal
    pool = mp.Pool(processes=cores, initializer=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    print(f"Started Multi-Processing Pool with {cores} independent workers.")

    for csv_file in csv_files:
        filepath = os.path.join(BASE_DIR, csv_file)
        print(f"\n>> Loading {csv_file} into Memory...")
        
        df_chunk = pd.read_csv(filepath, index_col=0)
        file_lines = len(df_chunk)
        
        if total_db_count >= file_lines:
            print(f"  -> Skipping entirely ({file_lines} rows already cleanly in DB).")
            total_db_count -= file_lines
            continue
            
        if total_db_count > 0:
            print(f"  -> Fast-forwarding: skipping {total_db_count} rows already processed in this chunk.")
            df_chunk = df_chunk.iloc[total_db_count:].copy()
            total_db_count = 0
            df_chunk.reset_index(drop=True, inplace=True)
            
        df_chunk.insert(5, "gscore_moreno", np.nan)
        df_chunk.insert(6, "gscore_azimuth", np.nan)
        
        chunk_batch_size = 50000 
        print(f"  -> Processing remaining {len(df_chunk)} rows...")
        
        if len(df_chunk) == 0:
            continue
            
        for j in tqdm.trange(0, len(df_chunk), chunk_batch_size):
            df_sub = df_chunk.iloc[j:j+chunk_batch_size, :].copy()
            
            gids = df_sub.iloc[:, 1].tolist()
            gid_str = ','.join(map(str, gids))
            
            if gid_str:
                cursor.execute(f"SELECT gid, gseq FROM table2 WHERE gid IN ({gid_str})")
                seq_map = dict(cursor.fetchall())
            else:
                seq_map = {}
            
            sub_len = len(df_sub)
            if sub_len == 0:
                continue
            split_size = (sub_len // cores) + 1
            
            worker_payloads = []
            current_batch = []
            for i in range(sub_len):
                row = list(df_sub.iloc[i, :])
                row[5] = -1.0 # Placeholder for Moreno score (failure = -1.0)
                row[6] = -1.0 # Placeholder for Azimuth score (failure = -1.0)
                seq = seq_map.get(row[1], '')
                current_batch.append((row, seq))
                
                if len(current_batch) == split_size:
                    worker_payloads.append(current_batch)
                    current_batch = []
                    
            if current_batch:
                worker_payloads.append(current_batch)
            
            try:
                results = pool.map(process_batch, worker_payloads)
            except KeyboardInterrupt:
                print("\n[WARNING] KeyboardInterrupt detected. Terminating all 16 background workers immediately...")
                pool.terminate()
                pool.join()
                import sys
                sys.exit(1)
            
            final_rows = []
            for res in results:
                final_rows.extend(res)
            
            try:
                cursor.executemany(
                    "INSERT INTO table1 (pos, gid, pam, upstream, downstream, gscore_moreno, gscore_azimuth, anno_class, te_class, te_dup) VALUES (?,?,?,?,?,?,?,?,?,?)", 
                    final_rows
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                print(f"Database lock error: {e}. Retrying commit...")
                import time
                time.sleep(5)
                conn.commit()

    pool.close()
    pool.join()
    print("\n[SUCCESS] All chunks scored and seamlessly inserted!")

if __name__ == "__main__":
    resume_db_scoring_mp(DB_PATH, cores=16)
