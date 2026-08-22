#!/usr/bin/env python3
"""rn6: SQLite → PostgreSQL COPY 兼容 gzip CSV 导出。

table1/table2 逐行导出为 gzip CSV,支持按 rowid 断点续传(每表一个 checkpoint),
并显示进度百分比 / 速率 / 预计剩余时间(ETA)。

用法:
  FRESH=1 python3 -u sqlite2csv.py                    # 强制从头
  python3 -u sqlite2csv.py                            # 普通运行(崩溃后自动续传)
  OUT_DIR=/path BATCH=100000 python3 -u sqlite2csv.py # 覆盖输出目录 / 批大小

环境变量:
  SQLITE_PATH  源库路径(默认 rn6.crisprte.db)
  OUT_DIR      输出目录(默认 /mnt/volume5/joh_crisprte/export/crisprtern6)
  BATCH        每批行数(默认 200000)
  FRESH=1      丢弃已有 checkpoint / 输出,从头重导
"""
import sqlite3, json, csv, gzip, os, io, time

SQLITE_PATH = os.environ.get("SQLITE_PATH", "/mnt/volume5/joh_crisprte/rn6.crisprte.db")
OUT_DIR     = os.environ.get("OUT_DIR", "/mnt/volume5/joh_crisprte/export/crisprtern6")
BATCH       = int(os.environ.get("BATCH", "200000"))
FRESH       = os.environ.get("FRESH") == "1"

def c_int(v):   return None if v is None else int(v)
def c_float(v): return None if v is None else float(v)
def c_str(v):
    if v is None: return None
    return v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
def c_bytea(v):
    if v is None: return None
    b = v if isinstance(v, (bytes, bytearray)) else str(v).encode("utf-8")
    return "\\x" + bytes(b).hex()
def c_gid(v):
    if isinstance(v, (bytes, bytearray)): return int.from_bytes(v, "little", signed=True)
    return int(v)
def c_intarray(v):
    if v is None or v == "" or v == b"": return None
    if isinstance(v, (bytes, bytearray)): v = v.decode("utf-8", "ignore")
    return "{" + ",".join(str(int(x)) for x in json.loads(v)) + "}"

def c_null(v):
    return None   # te_class/te_dup 非 hg38 的 integer,先存 NULL;建好 ttf 后再回填

SPEC = {
    "table1": [
        ("pos", "pos", c_str), ("gid", "gid", c_gid),
        ("gscore_moreno", "gscore_moreno", c_float),
        ("gscore_azimuth", "gscore_azimuth", c_float),
        ("pam", "pam", c_str), ("upstream", "upstream", c_str),
        ("downstream", "downstream", c_str),
        ("anno_class", "anno_class", c_bytea),
        ("te_class", "te_class", c_null),
        ("te_dup", "te_dup", c_null),
    ],
    "table2": [
        ("gid", "gid", c_gid), ("gseq", "gseq", c_str),
        ("mm1", "mm1", c_intarray), ("mm2", "mm2", c_intarray), ("mm3", "mm3", c_intarray),
    ],
}

def ckpt_path(t): return os.path.join(OUT_DIR, f"{t}.ckpt.json")
def out_path(t):  return os.path.join(OUT_DIR, f"{t}.csv.gz")

def load_ckpt(t):
    p = ckpt_path(t)
    if FRESH or not os.path.exists(p):
        return {"last_rowid": 0, "bytes": 0, "rows": 0, "done": False}
    with open(p) as f: return json.load(f)

def save_ckpt(t, ck):                       # 原子写,避免半截 checkpoint
    p = ckpt_path(t); tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ck, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)

def fmt_dur(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h{m:02d}m{s:02d}s"
    if m: return f"{m}m{s:02d}s"
    return f"{s}s"

def export(sq, table):
    spec = SPEC[table]
    sqcols = [s[1] for s in spec]; convs = [s[2] for s in spec]
    outp = out_path(table)
    ck = load_ckpt(table)
    if ck.get("done"):
        print(f"[SKIP] {table} 已完成（{ck['rows']:,} rows）。FRESH=1 可重导。", flush=True); return

    if FRESH or not os.path.exists(outp):
        open(outp, "wb").close()
        ck = {"last_rowid": 0, "bytes": 0, "rows": 0, "done": False}
    else:
        with open(outp, "r+b") as f:        # 丢弃崩溃残留的半个 member
            f.truncate(ck["bytes"])
        if os.path.getsize(outp) < ck["bytes"]:
            ck["bytes"] = os.path.getsize(outp)

    if "total" not in ck:
        print(f"[{table}] 统计总行数 COUNT(*) ...(大表需片刻)", flush=True)
        ck["total"] = sq.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        save_ckpt(table, ck)
    total = ck["total"]

    print(f"[RESUME] {table}: 从 rowid>{ck['last_rowid']} 续,已写 {ck['rows']:,} / {total:,} rows", flush=True)

    cur = sq.cursor()
    cur.execute(f"SELECT rowid, {', '.join(sqcols)} FROM {table} "
                f"WHERE rowid > ? ORDER BY rowid", (ck["last_rowid"],))
    t0 = time.time(); run_start_rows = ck["rows"]
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

            done = ck["rows"]; elapsed = time.time() - t0
            rows_run = done - run_start_rows
            pct = 100.0 * done / total if total else 0.0
            if elapsed > 1 and rows_run > 0:
                rate = rows_run / elapsed
                eta = (total - done) / rate
                print(f"  [{table}] {done:,}/{total:,} ({pct:5.1f}%) | "
                      f"速率 {rate:,.0f} 行/s | 已用时 {fmt_dur(elapsed)} | "
                      f"预计剩余 {fmt_dur(eta)}", flush=True)
            else:
                print(f"  [{table}] {done:,}/{total:,} ({pct:5.1f}%) | 已用时 {fmt_dur(elapsed)}", flush=True)

    ck["done"] = True; save_ckpt(table, ck)
    print(f"[DONE] {table}: {ck['rows']:,} rows -> {outp}", flush=True)
    print(f"        columns: ({', '.join(s[0] for s in spec)})", flush=True)

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"SQLITE_PATH = {SQLITE_PATH}", flush=True)
    print(f"OUT_DIR     = {OUT_DIR}", flush=True)
    print(f"BATCH       = {BATCH}   FRESH = {FRESH}", flush=True)
    sq = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    try:
        for t in ("table1", "table2"):
            print(f"=== export {t} ===", flush=True)
            export(sq, t)
    finally:
        sq.close()
    print("ALL DONE.")

if __name__ == "__main__":
    main()
