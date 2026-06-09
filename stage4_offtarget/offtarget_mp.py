import sys, os, time, sqlite3, json, gc
import multiprocessing as mp
import tqdm

import threading, os, time

# ===== 护身符:全局停车信号 =====
_STOP = threading.Event()
MEM_FLOOR_GB  = 40        # 系统可用内存低于此值就优雅停车
CHECK_INTERVAL = 60       # 秒：内存检查频率(保持灵敏)        # <<< 改动
LOG_INTERVAL   = 1800     # 秒：日志打印频率(半小时一行)      # <<< 改动

def _read_meminfo_avail_gb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024   # kB -> GB
    return None

def _read_self_rss_gb():
    with open(f"/proc/{os.getpid()}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024 / 1024
    return None

def _mem_watchdog():
    last_log = 0.0
    while not _STOP.is_set():
        rss   = _read_self_rss_gb()
        avail = _read_meminfo_avail_gb()
        now   = time.time()

        # 半小时打一行日志
        if now - last_log >= LOG_INTERVAL:
            ts = time.strftime("%H:%M:%S")
            print(f"[WATCHDOG {ts}] parent_RSS={rss:.1f}G  sys_avail={avail:.1f}G",
                  flush=True)
            last_log = now

        # 但内存检查每 60 秒就做，护身符才灵敏
        if avail is not None and avail < MEM_FLOOR_GB:
            ts = time.strftime("%H:%M:%S")
            print(f"[WATCHDOG {ts}] !!! avail<{MEM_FLOOR_GB}G,触发优雅停车", flush=True)
            _STOP.set()
            break

        time.sleep(CHECK_INTERVAL)
        
        
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"

BASE_DIR  = "/mnt/volume5/joh_crisprte"
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'src'))
sys.path.insert(0, os.path.join(BASE_DIR, 'CRISPRTE', 'PyExtensions', 'Trie'))
import trie

DB_PATH    = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"
TRIE_PATH  = "/mnt/volume5/joh_crisprte/rn6.crisprte.trie.data"
PAGE_SIZE  = 200_000     # 每次从 table2 流式取这么多行，杜绝 fetchall 爆内存
CHUNK_SIZE = 2000        # 每个 worker 任务包大小

# Trie 只在父进程加载一份，worker 靠 fork 的写时复制(COW)共享同一块物理内存
_TRIE_CACHE = None
_USE_MID    = False      # 中间值是否就是 uid（启动时自动探测，决定走不走快路）

def worker_init():
    # 不再在每个 worker 里 load_fast；trie 已通过 fork 继承
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def process_offtargets(args):
    global _TRIE_CACHE, _USE_MID
    if _TRIE_CACHE is None:
        raise RuntimeError("Trie 未被继承(是不是没走 fork?)")
    results = []
    for gid, gseq in args:
        try:
            raw_matches = _TRIE_CACHE.get_approximate_hamming(gseq, 3)
            offtargets = {1: [], 2: [], 3: []}
            if raw_matches:
                for match_seq, mid, dist in raw_matches:
                    dist = int(dist)
                    if dist in offtargets:
                        uid = mid if _USE_MID else _TRIE_CACHE.get_uid(match_seq)
                        if uid is not None:
                            offtargets[dist].append(str(uid))
            mm1 = json.dumps(offtargets[1]) if offtargets[1] else None
            mm2 = json.dumps(offtargets[2]) if offtargets[2] else None
            mm3 = json.dumps(offtargets[3]) if offtargets[3] else None
            results.append((mm1, mm2, mm3, gid))   # gid 保持原始存储形态，给 WHERE 用
        except Exception as e:
            print(f"[Worker Error] Failed to process {gid}: {e}")
    return results

def norm_gid(val):
    if isinstance(val, bytes):
        return int.from_bytes(val, 'little', signed=True)
    return int(val)

def commit_batch(cursor, conn, batch, retries=5):
    for attempt in range(retries):
        try:
            cursor.executemany(
                "UPDATE table2 SET mm1=?, mm2=?, mm3=?, is_scanned=1 WHERE gid=?", batch)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < retries - 1:
                print(f"[Warning] Database locked, retrying ({attempt + 1}/{retries})...")
                time.sleep(5)
                continue
            raise   # 非锁错误立即暴露，绝不静默
    print("[Critical] Batch gave up after retries; 这批未置 is_scanned，下次会自动重扫。")

def run_offtarget_analysis(cores=12):
    print(f"=== Off-target Analysis (shared-trie via fork COW, cores={cores}) ===")

    # ---- 阶段A：建列 + 取 TE 名单 + 抓一条样本 gseq，用完立刻关连接 ----
    conn = sqlite3.connect(DB_PATH, timeout=120)
    cursor = conn.cursor()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for col in ['is_scanned', 'mm1', 'mm2', 'mm3']:
        try:
            if col == 'is_scanned':
                cursor.execute("ALTER TABLE table2 ADD COLUMN is_scanned INTEGER DEFAULT 0")
            else:
                cursor.execute(f"ALTER TABLE table2 ADD COLUMN {col} BLOB")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    print(">> 1a. Querying TE target IDs from table1...")
    cursor.execute("SELECT gid FROM table1 WHERE anno_class LIKE 'TE%'")
    te_gids = {norm_gid(row[0]) for row in cursor.fetchall()}
    print(f">> Found {len(te_gids)} TE targets overall.")

    cursor.execute("SELECT gseq FROM table2 LIMIT 1")
    _sample_gseq = cursor.fetchone()[0]
    conn.close()   # fork 之前不持有任何 sqlite 连接(避免 fd 被子进程继承)

    # ---- 阶段B：父进程加载唯一一份 Trie + 自动探测快路 ----
    global _TRIE_CACHE, _USE_MID
    print(">> Loading trie ONCE in parent (~9.9G), 这步要几分钟，正常...")
    _TRIE_CACHE = trie.load_fast(TRIE_PATH)
    if _TRIE_CACHE is None:
        raise RuntimeError(f"Trie load returned None: {TRIE_PATH}")
    print(">> Trie loaded.")

    _sg = _sample_gseq.decode('utf-8', 'ignore') if isinstance(_sample_gseq, bytes) else _sample_gseq
    _probe = list(_TRIE_CACHE.get_approximate_hamming(_sg, 3))[:10]
    _USE_MID = len(_probe) > 0 and all(mid == _TRIE_CACHE.get_uid(ms) for ms, mid, _ in _probe)
    print(f">> fast path (USE_MID) = {_USE_MID}")

    # ---- 阶段C：冻结现有对象 + fork worker，子进程共享父内存里的 trie ----
    gc.freeze()                       # 把已存在对象冻进永久代，GC 不再触碰 → 保住 COW 共享
    ctx = mp.get_context("fork")      # 显式 fork 才能继承父进程内存
    pool = ctx.Pool(processes=cores, initializer=worker_init)
    print(f">> {cores} workers forked (sharing one trie via COW).")

    threading.Thread(target=_mem_watchdog, daemon=True).start()
    
    # ---- 阶段D：父进程开新连接，流式分页 + 写回 ----
    conn = sqlite3.connect(DB_PATH, timeout=120)
    cursor = conn.cursor()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    print(">> 3. Streaming table2 by rowid pages. Let the hunt begin!")
    pbar = tqdm.tqdm(total=len(te_gids), unit="gRNA", desc="Off-target scan")
    last_rowid = 0
    processed = 0
    stopped = False
    try:
        while True:
            cursor.execute(
                "SELECT rowid, gid, gseq FROM table2 INDEXED BY idx_todo "
                "WHERE rowid > ? AND is_scanned = 0 "
                "ORDER BY rowid LIMIT ?",
                (last_rowid, PAGE_SIZE),
            )
            rows = cursor.fetchall()
            if not rows:
                break
            last_rowid = rows[-1][0]               # 过滤前的页尾推进，正确，保持不动

            targets = []
            skip_rowids = []                                                 # <<< 新增
            for _rid, raw_gid, gseq in rows:
                if norm_gid(raw_gid) in te_gids:
                    if isinstance(gseq, bytes):
                        gseq = gseq.decode('utf-8', errors='ignore')
                    targets.append((raw_gid, gseq))
                else:
                    skip_rowids.append(_rid)                                 # <<< 非TE

            if skip_rowids:                                                  # <<< 一次性踢出待办
                cursor.executemany("UPDATE table2 SET is_scanned=2 WHERE rowid=?",
                                [(r,) for r in skip_rowids])
                conn.commit()

            if not targets:
                if _STOP.is_set():
                    stopped = True
                    break
                continue

            sub_chunks = [targets[i:i + CHUNK_SIZE] for i in range(0, len(targets), CHUNK_SIZE)]
            for batch_results in pool.imap_unordered(process_offtargets, sub_chunks):
                if batch_results:
                    commit_batch(cursor, conn, batch_results)
                    processed += len(batch_results)
                    pbar.update(len(batch_results))

            # 本页全部落库后：回收 WAL，避免 -wal 文件无限膨胀          # <<< 改动②
            conn.commit()                                              # <<< 改动②
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")            # <<< 改动②

            # 护身符：整页提交完才检查，确保已扫的行都已落库，续传零损失   # <<< 改动③
            if _STOP.is_set():                                         # <<< 改动③
                print("\n[MAIN] 收到停车信号，优雅退出，可凭 idx_todo 续传", flush=True)  # <<< 改动③
                pool.terminate()                                       # <<< 改动③
                pool.join()                                            # <<< 改动③
                stopped = True                                         # <<< 改动③
                break                                                  # <<< 改动③

    except KeyboardInterrupt:
        print("\n[WARNING] KeyboardInterrupt detected. Terminating all workers immediately...")
        pool.terminate()
        pool.join()
        pbar.close()
        conn.close()
        sys.exit(1)

    # ---- 正常结束 / 优雅停车 的统一收尾 ----
    _STOP.set()                          # <<< 改动③ 通知看门狗线程退出
    if not stopped:                      # <<< 改动③ 只有非停车路径才 close()
        pool.close()                     #         （停车路径已 terminate 过，不能再 close）
        pool.join()
    pbar.close()
    conn.close()                         # <<< 改动③ 注意：删掉了原来重复的 pool.close()/join()

    if processed == 0:
        print("[SUCCESS] Everything is fully processed. No action needed.")
    else:
        print(f"\n[SUCCESS] Complete! Scanned {processed} gRNAs. The database is sealed.")

if __name__ == "__main__":
    run_offtarget_analysis(cores=12)