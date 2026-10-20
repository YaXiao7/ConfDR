# PodPendingTaxonomy

Taxonomy of misconfigurations that cause Pod Pending, derived from the open coding of the 159 cases (`open-coding.xlsx`).
It has five top-level and 15 second-level categories. A case can belong to more than one category, so the top-level counts sum to more than 159.

| Code | Type | Description | #Cases |
|---|---|---|---:|
| **R** | **Node resource-capacity constraints** | | **32** |
| R1 | Insufficient CPU capacity | CPU requests exceed available schedulable capacity. | 18 |
| R2 | Insufficient memory capacity | Memory requests exceed available schedulable capacity. | 9 |
| R3 | Insufficient GPU capacity | GPU requests exceed available capacity. | 5 |
| R4 | Node Pod limit | The node cannot admit another Pod under its Pod-count limit. | 6 |
| R5 | Node volume limit | Required volumes exceed the applicable node attachment limit. | 1 |
| **A** | **Taint–toleration constraints** | | **33** |
| A1 | Taint–toleration mismatch | The Pod does not tolerate a node taint that prevents scheduling. | 33 |
| **P** | **Affinity and topology constraints** | | **35** |
| P1 | Unsatisfied node selection | Available nodes do not satisfy required node properties. | 19 |
| P2 | Unsatisfied Pod affinity | A required placement relationship with other Pods cannot be satisfied. | 4 |
| P3 | Unsatisfied Pod anti-affinity | Required separation from other Pods cannot be satisfied. | 8 |
| P4 | Unsatisfied topology spread | A hard topology spread requirement cannot be satisfied. | 5 |
| **S** | **Storage dependency constraints** | | **61** |
| S1 | Invalid or missing storage reference | The required storage object is missing or incorrectly referenced. | 9 |
| S2 | Dynamic provisioning conflict | Provisioning settings do not support volume creation. | 18 |
| S3 | PVC–PV binding mismatch | A candidate PV does not meet the PVC's binding requirements. | 17 |
| S4 | Volume topology conflict | Volume accessibility conflicts with Pod placement requirements. | 17 |
| **E** | **Port-allocation constraints** | | **6** |
| E1 | HostPort conflict | A requested host port conflicts with an existing allocation on a candidate node. | 6 |

The machine-readable version used by ConfDR, with the constraints that detect each category, is `ConfDR/config/taxonomy.yaml`.
