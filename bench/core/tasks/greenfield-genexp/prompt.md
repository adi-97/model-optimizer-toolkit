Implement the project described below in the current directory, which is empty. Work only inside this directory.

Problem statement

Researchers have gene expression datasets from healthy individuals and patients with a particular disease. They want to identify genes that behave differently between the two groups, discover potential biomarkers, and understand which biological pathways are associated with the disease.

Build an end-to-end platform that accepts gene expression data and produces a scientific analysis report.

Input data
- Gene expression matrix of raw counts (genes x samples).
- Sample metadata (healthy/disease, age, sex, batch).
- Gene annotation dataset.

Expected output
- Data quality report.
- Differential gene expression analysis with statistical significance and effect sizes.
- PCA and clustering visualizations.
- Candidate biomarker ranking.
- Pathway enrichment analysis against a small gene-set file you bundle.
- Downloadable, self-contained HTML research report.

Required interface (other tools depend on it exactly)
- Python package `genexp` in this directory; create a virtual environment at `.venv` here and install dependencies only there, recorded in `requirements.txt`.
- Command: `.venv\Scripts\python -m genexp run --counts COUNTS.tsv --samples SAMPLES.tsv --genes GENES.tsv --out OUTDIR`
  - `COUNTS.tsv`: tab-separated, first column `gene_id`, then one integer column per sample.
  - `SAMPLES.tsv`: tab-separated, columns `sample_id`, `condition` (`healthy` or `disease`), `age`, `sex`, `batch`.
  - `GENES.tsv`: tab-separated, columns `gene_id`, `symbol`, `biotype`.
- Files written to `OUTDIR`:
  - `report.html`: self-contained (no external scripts, images or stylesheets).
  - `de_results.csv`: columns `gene_id`, `log2fc` (disease relative to healthy), `pvalue`, `padj` (Benjamini-Hochberg).
  - `biomarkers.csv`: columns `gene_id`, `rank` (1 = best).
  - `pca.csv`: columns `sample_id`, `PC1`, `PC2`.
- Everything runs offline.

Include automated tests, run them and the command above on synthetic data before finishing, and write a short README.
