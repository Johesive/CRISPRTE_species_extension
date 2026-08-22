import sqlite3, json, csv, gzip, os, io, time

SQLITE_PATH = "/mnt/volume5/joh_crisprte/rn6.crisprte.db"
OUT_DIR     = "/tmp/crisprtern6"
BATCH       = 200000
FRESH       = os.environ.get("FRESH") == "1"

def c_int(v):   return None if v is None else int(v)
def c_float(v): return None if v is None else float(v)
def c_str(v):
    if v is None: return None
    return v.decode("utf-8","ignore") if isinstance(v,(bytes,bytearray)) else str(v)
def c_bytea(v):
    if v is None: return None
    b = v if isinstance(v,(bytes,bytearray)) else str(v).encode("utf-8")
    return "\\x" + bytes(b).hex()
def c_gid(v):
    if isinstance(v,(bytes,bytearray)): return int.from_bytes(v,"little",signed=True)
    return int(v)
def c_intarray(v):
    if v is None or v=="" or v==b"": return None
    if isinstance(v,(bytes,bytearray)): v=v.decode("utf-8","ignore")
    return "{" + ",".join(str(int(x)) for x in json.loads(v)) + "}"

def c_null(v):
    return None   # te_class/te_dup 现非 hg38 的 integer，先存 NULL；建好 ttf 后再回填

SPEC = {
 "table1": [
    ("pos","pos",c_str),("gid","gid",c_gid),
    ("gscore_moreno","gscore_moreno",c_float),
    ("gscore_azimuth","gscore_azimuth",c_float),
    ("pam","pam",c_str),("upstream","upstream",c_str),
    ("downstream","downstream",c_str),
    ("anno_class","anno_class",c_bytea),
    ("te_class","te_class",c_null),
    ("te_dup","te_dup",c_null),
 ],
 "table2": [
    ("gid","gid",c_gid),("gseq","gseq",c_str),
    ("mm1","mm1",c_intarray),("mm2","mm2",c_intarray),("mm3","mm3",c_intarray),
 ],
}

def ckpt_path(t): return os.path.join(OUT_DIR, f"{t}.ckpt.json")
def out_path(t):  return os.path.join(OUT_DIR, f"{t}.csv.gz")

def load_ckpt(t):
    p = ckpt_path(t)
    if FRESH or not os.path.exists(p):
        return {"last_rowid": 0, "bytes": 0, "rows": 0, "done": False}
    with open(p) as f: return json.load(f)

def save_ckpt(t, ck):                       # 原子写，避免半截 checkpoint
    p = ckpt_path(t); tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ck, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)

def export(sq, table):
    spec = SPEC[table]
    sqcols = [s[1] for s in spec]; convs = [s[2] for s in spec]
    outp = out_path(table)
    ck = load_ckpt(table)
    if ck.get("done"):
        print(f"[SKIP] {table} 已完成（{ck['rows']} rows）。FRESH=1 可重导。"); return

    if FRESH or not os.path.exists(outp):
        open(outp, "wb").close()
        ck = {"last_rowid": 0, "bytes": 0, "rows": 0, "done": False}
    else:
        with open(outp, "r+b") as f:        # 丢弃崩溃残留的半个 member
            f.truncate(ck["bytes"])
        if os.path.getsize(outp) < ck["bytes"]:
            ck["bytes"] = os.path.getsize(outp)

    print(f"[RESUME] {table}: 从 rowid>{ck['last_rowid']} 续，已写 {ck['rows']} rows", flush=True)

    cur = sq.cursor()
    cur.execute(f"SELECT rowid, {', '.join(sqcols)} FROM {table} "
                f"WHERE rowid > ? ORDER BY rowid", (ck["last_rowid"],))
    t0 = time.time()
    with open(outp, "ab") as f:
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows: break
            buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
            max_rowid = ck["last_rowid"]
            for row in rows:
                rid = row[0]
                w.writerow([conv(v) for conv, v in zip(convs, row[1:])])
                if rid > max_rowid: max_rowid = rid
            member = gzip.compress(buf.getvalue().encode("utf-8"))
            f.write(member); f.flush(); os.fsync(f.fileno())   # ① 数据 member 先落盘
            ck["last_rowid"] = max_rowid
            ck["rows"]      += len(rows)
            ck["bytes"]      = f.tell()
            save_ckpt(table, ck)                               # ② 再更新 checkpoint
            print(f"  [{table}] {ck['rows']} rows, rowid<= {max_rowid} ({time.time()-t0:.1f}s)", flush=True)

    ck["done"] = True; save_ckpt(table, ck)
    print(f"[DONE] {table}: {ck['rows']} rows -> {outp}")
    print(f"        columns: ({', '.join(s[0] for s in spec)})")

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sq = sqlite3.connect(SQLITE_PATH)
    try:
        for t in ("table1","table2"):
            print(f"=== export {t} ===", flush=True)
            export(sq, t)
    finally:
        sq.close()
    print("ALL DONE.")

if __name__ == "__main__":
    main()