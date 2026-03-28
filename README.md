# CrisprArmyKnife

![forthebadge](https://forthebadge.com/images/badges/60-percent-of-the-time-works-every-time.svg)

My personal Swiss Army Knife for Bioinformatics

## Requirements

- Python (No idea the minimum, I use 3.14)
- Biopython >= 1.86
- Edlib >= 1.3.9.post1

## Recommended

Highly recommend using UV for managing/installing the dependencies and running the tools

[Astral UV](https://astral.sh/uv/) - A modern Python package manager and virtual environment tool

## Tool Directory

### Sequence Analysis

- **[tools/blastools.py](docs/blastools.md)** - BLAST result manipulation
  - Self-hit removal from BLAST reports

- **[tools/ref_filler.py](docs/ref_filler.md)** - Reference sequence expansion
  - Fills alignment gaps by extracting variants from aligned sequences
  - Generates subsequence FASTA files for missing regions

- **[tools/pairwise_align.py](docs/pairwise_align.md)** - Pairwise sequence alignment
  - Aligns sequence pairs using Biopython

- **[tools/ann_to_bed.py](docs/ann_to_bed.md)** - Format conversion
  - Converts UCSC RepMask annotation format to BED format

### Data Processing

- **[tools/clstr_splitter.py](docs/clstr_splitter.md)** - CD-HIT cluster parser
  - Splits CD-HIT cluster output into individual cluster files

- **[tools/dircutadapt.py](docs/dircutadapt.md)** - CutAdapt wrapper
  - Batch adapter trimming for FASTQ files in a directory

- **[tools/subsample.py](docs/subsample.md)** - Subsampling tool
  - Generates subsampled FASTQ files using SeqTK
  - Optional bulk processing from TSV file

- **[tools/extract_index_files.py](docs/extract_index_files.md)** - Index extraction
  - Extracts index sequences from paired-end FASTQ (generates I1/I2 from R1/R2)
  - Supports gzipped input

- **[tools/fasta_subseq_dl.py](docs/fasta_subseq_dl.md)** - Sequence download
  - Downloads FASTA subsequences from NCBI using Accession IDs and coordinates

- **[tools/bed_expansion.py](docs/bed_expansion.md)** - BED region expansion
  - Expands BED regions below a minimum length threshold symmetrically

### Miscellaneous

- **[tools/graph_assembler.py](docs/graph_assembler.md)** - De Bruijn-like sequence assembly
  - Graph-based read assembly using greedy overlap matching
  - Implements Depth-First Search path finding

- **[tools/detect_fusions.py](docs/detect_fusions.md)** - Fusion read detection
  - K-mer based detection of spanning and chimeric fusion reads in paired-end FASTQ
  - Deduplicates k-mers shared between fusion partners to reduce false positives

- **[tools/detect_tandem_repeats.py](docs/detect_tandem_repeats.md)** - Tandem repeat detection
  - Identifies assembled contigs containing tandem repeats of a gene
  - Fetches exon sequences from NCBI by accession and aligns against contigs