# Evaluation report

> All scores below are internal synthetic-split results. They do not estimate performance on real clinical cases.

- Rows: 12,600; 32 input features; 7 target classes.
- Split: train 8,820 / validation 1,890 / test 1,890.
- Selected model: Regularized Logistic Regression; validation-fitted temperature = 1.002.
- Optional packages: {"xgboost": "available", "lightgbm": "available", "catboost": "not installed; not benchmarked"}.

## Cross-validation on training partition

| Model | Accuracy mean ± SD | Macro F1 mean ± SD | Log loss mean ± SD |
|---|---:|---:|---:|
| Regularized Logistic Regression | 0.785 ± 0.005 | 0.784 ± 0.006 | 0.618 ± 0.028 |
| Random Forest | 0.758 ± 0.006 | 0.757 ± 0.006 | 0.743 ± 0.031 |
| Extra Trees | 0.755 ± 0.009 | 0.753 ± 0.009 | 0.865 ± 0.081 |
| HistGradientBoosting | 0.777 ± 0.005 | 0.776 ± 0.005 | 0.649 ± 0.021 |

## Final partitions (calibrated probabilities)

| Metric | Train | Validation | Test |
|---|---:|---:|---:|
| accuracy | 0.7920 | 0.8000 | 0.7862 |
| balanced_accuracy | 0.7920 | 0.8000 | 0.7862 |
| macro_precision | 0.7912 | 0.8013 | 0.7854 |
| macro_recall | 0.7920 | 0.8000 | 0.7862 |
| macro_f1 | 0.7908 | 0.7997 | 0.7850 |
| weighted_f1 | 0.7908 | 0.7997 | 0.7850 |
| log_loss | 0.5918 | 0.6017 | 0.6179 |
| multiclass_brier | 0.2962 | 0.2945 | 0.3104 |
| top_label_ece_10_bins | 0.0074 | 0.0174 | 0.0163 |

## Robustness scenarios

Fresh synthetic stress sample (same simulator; not clinical validation): {"accuracy": 0.7666666666666667, "balanced_accuracy": 0.7666666666666667, "macro_precision": 0.7665149513362806, "macro_recall": 0.7666666666666667, "macro_f1": 0.7658250390642517, "weighted_f1": 0.7658250390642517, "log_loss": 0.6994295091685173, "multiclass_brier": 0.33900324765653866, "top_label_ece_10_bins": 0.038852690536398976}

## Calibration

Test top-label reliability bins (confidence compared with empirical accuracy):

| Bin | N | Mean confidence | Accuracy |
|---|---:|---:|---:|
| 0.2–0.3 | 6 | 0.268 | 0.333 |
| 0.3–0.4 | 64 | 0.361 | 0.281 |
| 0.4–0.5 | 143 | 0.454 | 0.490 |
| 0.5–0.6 | 191 | 0.552 | 0.560 |
| 0.6–0.7 | 164 | 0.655 | 0.677 |
| 0.7–0.8 | 226 | 0.753 | 0.765 |
| 0.8–0.9 | 327 | 0.854 | 0.841 |
| 0.9–1.0 | 769 | 0.960 | 0.949 |

Linear coefficients are exported in `linear_coefficients.csv`; grouped absolute magnitudes are associational diagnostics, not causal explanations.

## Per-class test metrics

| Disease | Precision | Recall | F1 | Support |
|---|---:|---:|---:|---:|
| FMD | 0.835 | 0.826 | 0.831 | 270 |
| Lumpy Skin Disease | 0.821 | 0.815 | 0.818 | 270 |
| Hemorrhagic Septicemia | 0.823 | 0.793 | 0.808 | 270 |
| Mastitis | 0.847 | 0.859 | 0.853 | 270 |
| Black Quarter / Blackleg | 0.705 | 0.674 | 0.689 | 270 |
| Anthrax | 0.697 | 0.648 | 0.672 | 270 |
| Healthy | 0.769 | 0.889 | 0.825 | 270 |

## Confusion matrix

Rows are actual classes; columns are predicted classes in the listed order:

`[[223, 6, 7, 1, 18, 4, 11], [9, 220, 13, 7, 6, 7, 8], [5, 17, 214, 2, 7, 24, 1], [5, 5, 4, 232, 9, 5, 10], [8, 4, 3, 19, 182, 31, 23], [11, 7, 19, 11, 28, 175, 19], [6, 9, 0, 2, 8, 5, 240]]`

The dataset generator encodes broad clinical signs and overlaps based on the sources documented in `clinical_sources.md`. The synthetic probability assumptions are not epidemiological estimates. Clinical deployment requires evaluation on independently collected, veterinarian-labelled cases.