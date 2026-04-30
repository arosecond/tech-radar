# Sample digest

This is a real digest excerpt produced by `tech-radar run` on **2026-04-30** with the
default 3D-reconstruction interest profile. Two papers (out of 27 that survived the
Filter stage that day) are reproduced verbatim below to show what the pipeline
actually outputs.

> The full digest lives in `output/digest-YYYY-MM-DD.md` after every run; that path
> is gitignored in this repo because it changes daily and would otherwise dominate
> the git history.

Sections per paper are produced by the **Summarizer** stage (Qwen 3.6-27B with
`enable_thinking: true`) against a constrained Pydantic schema. Trailing tag
chips and metadata come from the **Tagger** stage.

---

## arXiv cs.CV

### [PhyloSDF: Phylogenetically-Conditioned Neural Generation of 3D Skull Morphology via Residual Flow Matching](http://arxiv.org/abs/2604.25371v1)

*Kaikwan Lau, Gary P. T. Choi — 2026-04-28*

**TL;DR:** 系統樹情報を条件としたニューラル生成モデルにより、少数の標本から生物学的に妥当な3D頭蓋骨形状を生成する手法を提案した。

**Key points:**
- DeepSDFオートデコーダーに系統的一貫性損失を適用し、潜在空間を進化距離と相関させる（Pearson r=0.993）。
- Residual Conditional Flow Matching (Residual CFM) を用い、種重心の解析的検索と学習済み残差予測に生成プロセスを分離。
- 種あたり約4標本から生成可能で、生成メッシュは実際の種内変異の88-129%を再現し、180個すべてが非暗記と検証された。
- Chamfer Distance (0.00181) とmorphometric Fréchet distance (10,641) で、既存の拡散モデルや標準フローマッチング、ガウス混合モデルを上回る。
- 種除外実験で系統外挿能力を示し、潜在空間の滑らかな補間で生物学的に妥当な祖先頭蓋骨の再構築を可能にする。

**Why it matters:** 進化生物学における3D形態生成のデータ希少性課題を解決し、少数標本から系統関係を尊重した新規形状や祖先復元を可能にするため。

**Technical details:**
- 性能: Chamfer Distance 0.00181 / morphometric Fréchet distance 10,641 / intra-species variation 88-129% / latent space correlation Pearson r=0.993
- 処理速度: N/A
- 必要GPU: N/A

**Repository:**
- GitHub: N/A
- License: N/A

`Generative 3D` · `Implicit Neural Representations` · `Diffusion Models for 3D` · `Mesh Reconstruction`
_type: novel_method · datasets: Darwin's Finches micro-CT scans_

---

### [Unconstrained Multi-view Human Pose Estimation with Algebraic Priors](http://arxiv.org/abs/2604.24312v1)

*Xiaolin Qin, Qianlei Wang, Jiacen Liu et al. — 2026-04-27*

**TL;DR:** カメラキャリブレーションが不要な複数視点画像から、代数的制約と時系列一貫性を活用して高精度な3D人体姿勢を推定するフレームワークを提案する。

**Key points:**
- TTR（Triangulation with Transformer Regressor）により明示的なカメラパラメータに依存しないデータ駆動型三角測量を実現し、未キャリブレーション環境での推定を可能にする。
- Gröbner基底に基づく損失関数（GC）で多視点多様体の代数的関係を学習プロセスに埋め込み、射影幾何学的制約を厳密に満たす。
- 人体運動の等変性特性を活用したTERにより時系列の一貫性を確保し、未キャリブレーション特有のスケール曖昧性を低減する。
- 標準ベンチマークで未キャリブレーション多視点人体姿勢推定のSOTAを更新し、完全キャリブレーション手法との性能差を大幅に縮小した。

**Why it matters:** 現実の監視カメラやモバイルデバイスなどキャリブレーション情報が揃わない環境でも、高精度な3D人体姿勢推定を適用可能にするため。

**Technical details:**
- 性能: N/A
- 処理速度: N/A
- 必要GPU: N/A

**Repository:**
- GitHub: N/A
- License: N/A

`Camera Pose Estimation` · `Multi-View Stereo` · `Structure from Motion` · `Implicit Neural Representations`
_type: novel_method_

---

_25 more papers omitted for brevity in this sample. The full daily digest
includes everything that survived Filter on the day, sectioned by arXiv
category, with the same per-paper schema._
