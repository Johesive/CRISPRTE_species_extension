import gzip
import time
import os
import pandas as pd
import gtfparse
import pybedtools
from tqdm import tqdm

GTF_FILE   = "Rattus_norvegicus.Rnor_6.0.104.gtf"
RMSK_FILE  = "rmsk.txt"
GENOME_FAI = "rn6.fa.fai"
GENOME_FILE = "rn6.genome"
OUTPUT_BED  = "rn6_fullAnnotation.bed"

PROMOTER_UPSTREAM   = 1000
PROMOTER_DOWNSTREAM = 100


def open_file(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")

def elapsed(t):
    return f"{time.time() - t:.1f}s"


# Save chromosome sizes into .genome file using samtools .fai index
def build_genome_file(fai_path, genome_out):
    print("Reading chromosome sizes from FAI index...")
    t = time.time()

    chrom_sizes = {}
    with open_file(fai_path) as f:
        for line in f:
            if line.strip():
                parts = line.strip().split('\t')
                chrom_sizes[parts[0]] = int(parts[1])

    with open(genome_out, "w") as f:
        for chrom, size in chrom_sizes.items():
            f.write(f"{chrom}\t{size}\n")

    print(f"  Found {len(chrom_sizes)} chromosomes  ({elapsed(t)})")
    return chrom_sizes

# GTF → promoter-TSS / exon / intron / intergenic
def build_gene_intervals(gtf_path, genome_file, chrom_sizes):
    print("Parsing GTF...")
    t = time.time()
    df = gtfparse.read_gtf(gtf_path)
    print(f"  GTF rows: {len(df)}  ({elapsed(t)})")

    # 列名：gtfparse v2 用 seqname 或 seqid，先检测
    chrom_col = "seqname" if "seqname" in df.columns else "seqid"

    # gene — 用 .filter() 替代布尔索引
    genes_df = df.filter(df["feature"] == "gene") \
                 .select([chrom_col, "start", "end", "strand"]) \
                 .to_pandas()
    genes_df = genes_df.rename(columns={chrom_col: "seqname"})
    genes_df["start"] = genes_df["start"] - 1   # 1-based → 0-based
    
    genes_df["seqname"] = genes_df["seqname"].apply(
    lambda x: x if x.startswith("chr") else "chr" + x
    )
    # ★ 只保留 genome 文件里存在的染色体
    valid_chroms = set(chrom_sizes.keys())
    genes_df = genes_df[genes_df["seqname"].isin(valid_chroms)]

    genes_df["name"]  = "gene"
    genes_df["score"] = 0
    gene_bed = pybedtools.BedTool.from_dataframe(
        genes_df[["seqname", "start", "end", "name", "score", "strand"]]
    )

    # exon
    exons_df = df.filter(df["feature"] == "exon") \
                 .select([chrom_col, "start", "end", "strand"]) \
                 .to_pandas()
    exons_df = exons_df.rename(columns={chrom_col: "seqname"})
    exons_df["start"] = exons_df["start"] - 1

    exons_df["seqname"] = exons_df["seqname"].apply(
    lambda x: x if x.startswith("chr") else "chr" + x
    )

    exons_df = exons_df[exons_df["seqname"].isin(valid_chroms)]

    exons_df["name"]  = "exon"
    exons_df["score"] = 0
    exon_bed = pybedtools.BedTool.from_dataframe(
        exons_df[["seqname", "start", "end", "name", "score", "strand"]]
    )

    # promoter-TSS
    t = time.time()
    proms = []
    for _, row in tqdm(genes_df.iterrows(), total=len(genes_df), desc="  Promoters"):
        chrom  = row["seqname"]
        strand = row["strand"]
        tss    = row["start"] if strand == "+" else row["end"] - 1

        if strand == "+":
            s = max(0, tss - PROMOTER_UPSTREAM)
            e = tss + PROMOTER_DOWNSTREAM
        else:
            s = max(0, tss - PROMOTER_DOWNSTREAM)
            e = tss + PROMOTER_UPSTREAM

        chrom_len = chrom_sizes.get(chrom, e)
        e = min(e, chrom_len)

        proms.append((chrom, s, e, "promoter-TSS", 0, strand))

    promoter_df  = pd.DataFrame(proms, columns=["chrom", "start", "end", "name", "score", "strand"])
    promoter_bed = pybedtools.BedTool.from_dataframe(promoter_df)
    print(f"  {len(proms)} promoter intervals  ({elapsed(t)})")

    # intron = gene - exon
    t = time.time()
    intron_bed_raw = gene_bed.subtract(exon_bed)
    intron_rows = [
        (iv.chrom, iv.start, iv.end, "intron", 0, iv.strand)
        for iv in intron_bed_raw
    ]
    intron_df  = pd.DataFrame(intron_rows, columns=["chrom", "start", "end", "name", "score", "strand"])
    intron_bed = pybedtools.BedTool.from_dataframe(intron_df)
    print(f"done ({elapsed(t)})")

    # intergenic = genome - gene
    t = time.time()
    intergenic_bed_raw = gene_bed.sort().complement(g=genome_file)
    intergenic_rows = [
        (iv.chrom, iv.start, iv.end, "intergenic", 0, ".")
        for iv in intergenic_bed_raw
    ]
    intergenic_df  = pd.DataFrame(intergenic_rows, columns=["chrom", "start", "end", "name", "score", "strand"])
    intergenic_bed = pybedtools.BedTool.from_dataframe(intergenic_df)

    return promoter_bed, exon_bed, intron_bed, intergenic_bed


# rmsk.txt → TE
def parse_rmsk(rmsk_path):
    print("Parsing TE...")
    t = time.time()
    rows = []

    with open_file(rmsk_path) as f:
        with tqdm(desc="  Parsing rmsk", unit=" lines") as pbar:
            for line in f:
                pbar.update(1)
                if line.startswith("#") or line.strip() == "":
                    continue
                cols = line.rstrip().split("\t")
                if len(cols) < 13:
                    continue

                chrom      = cols[5]
                start      = int(cols[6])
                end        = int(cols[7])
                rep_name   = cols[10]
                rep_class  = cols[11]
                rep_family = cols[12]
                # RepeatMasker strand is usually in column 9 (0-indexed 8)
                # It can be '+' or 'C' (complement/-/reverse)
                strand_char = cols[8]
                strand = "-" if strand_char == "C" else "+"

                label = f"TE({rep_name};{rep_family};{rep_class})"

                rows.append((chrom, start, end, label, 0, strand))

    print(f"  {len(rows)} TE intervals  ({elapsed(t)})")
    te_df  = pd.DataFrame(rows, columns=["chrom", "start", "end", "name", "score", "strand"])
    te_bed = pybedtools.BedTool.from_dataframe(te_df)
    return te_bed


if __name__ == "__main__":
    t_total = time.time()
    print("=" * 55)

    chrom_sizes = build_genome_file(GENOME_FAI, GENOME_FILE)
    print("=" * 55)

    promoter_bed, exon_bed, intron_bed, intergenic_bed = build_gene_intervals(
        GTF_FILE, GENOME_FILE, chrom_sizes
    )
    print("=" * 55)

    te_bed = parse_rmsk(RMSK_FILE)
    print("=" * 55)

    print("Merging and sorting all intervals...", end=" ", flush=True)
    t = time.time()
    all_bed = (promoter_bed
               .cat(exon_bed,        postmerge=False)
               .cat(te_bed,          postmerge=False)
               .cat(intron_bed,      postmerge=False)
               .cat(intergenic_bed,  postmerge=False)
               .sort())
    all_bed.saveas(OUTPUT_BED)
    print(f"done ({elapsed(t)})")

    print("=" * 55)
    print(f"All done!  Total: {(time.time() - t_total) / 60:.1f} min")
    print(f"Output: {OUTPUT_BED}")