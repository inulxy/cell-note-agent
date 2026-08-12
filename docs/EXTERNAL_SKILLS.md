# External Skills Strategy

CellNoteAgent should reuse mature external skills and official SOPs where they improve
scientific rigor. The project should **not** reimplement every Scanpy, SnapATAC2, Signac,
ArchR, MACS3, ENCODE QC, or muon workflow from scratch.

The boundary is:

```text
External skills/references = trusted analysis guidance and tool-specific SOPs
CellNoteAgent = orchestration, provenance, canonical peak matrix, QC summaries, data cards, manifest handoff
```

## Why Not Just Call Everything Directly?

External skills can be excellent, but direct runtime use creates problems:

- upstream `main` can change and break reproducibility
- licenses differ by skill/repo
- output paths and object schemas may not match our peak-matrix handoff contract
- some skills are broad runbooks, not deterministic scripts
- external skills must not bypass `peak_matrices/<dataset_id>/cell_x_peak.npz`

So this repository uses `configs/external_skills.json` as a curated registry and
`cell_note_agent.external_skills` as the selection/planning layer.

## Current Trusted Coverage

### Core Agent Skills

- K-Dense `scanpy`: RNA-side QC, preprocessing, clustering, annotation.
- K-Dense `anndata`: h5ad/AnnData I/O and metadata hygiene.
- GPTomics `atac-qc`: ENCODE-style ATAC QC metrics.
- GPTomics `single-cell-atac`: 10x scATAC / Multiome ATAC routing via Signac, ArchR, SnapATAC2, Cell Ranger ATAC/ARC.
- GPTomics `atac-peak-calling`: fragment-derived peak calling.
- GPTomics `consensus-peakset`: fixed peak universe across samples/batches.

### Optional Downstream/Evaluation Skills

- GPTomics `motif-deviation`
- GPTomics `differential-accessibility`
- GPTomics `co-accessibility`
- GPTomics `enhancer-gene-linking`
- GPTomics `nucleosome-positioning`
- GPTomics `footprinting`
- GPTomics `deep-learning-atac`
- GPTomics `allele-specific-accessibility`

### Official References

- SnapATAC2 official docs for scATAC and multiome ATAC processing.
- muon official docs for MuData/multimodal omics workflows.
- Scanpy official docs for scRNA processing.
- ENCODE ATAC-seq standards for QC thresholds and processing expectations.

## Commands

Validate the registry:

```bash
python -m cell_note_agent.external_skills validate
```

List core multiome-related entries:

```bash
python -m cell_note_agent.external_skills list --modality multiome --core-only
```

Plan external skills for a multiome dataset:

```bash
python -m cell_note_agent.external_skills plan \
  --modality multiome \
  --dataset_id pbmc_multiome \
  --results_root results/mvp
```

Plan external skills for fragment-based scATAC:

```bash
python -m cell_note_agent.external_skills plan \
  --modality scatac_fragments \
  --dataset_id pbmc_atac \
  --results_root results/mvp
```

## Provenance Requirements

For any external skill used in a real run, record:

- `skill_id`
- source URL
- repo/path/ref or commit hash
- whether it was consulted, vendored, or executed through a local adapter
- outputs accepted into CellNoteAgent
- human review decision if the skill is an external Agent Skill

## Source Links

- [K-Dense scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills)
- [K-Dense scanpy skill](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/scanpy)
- [GPTomics bioSkills ATAC-seq](https://github.com/GPTomics/bioSkills/tree/main/atac-seq)
- [GPTomics single-cell ATAC skill](https://github.com/GPTomics/bioSkills/tree/main/atac-seq/single-cell-atac)
- [SnapATAC2 docs](https://scverse.org/SnapATAC2/)
- [muon docs](https://muon.scverse.org/)
- [Scanpy docs](https://scanpy.readthedocs.io/en/stable/)
- [ENCODE ATAC-seq standards](https://www.encodeproject.org/atac-seq/)
