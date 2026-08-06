# TFL shell 跨版本决策复用

- 结构变化，重新入队：1
- 未变，沿用已有决定：1
- 上游变化，原决定失效并重新确认：1

## 需关注的变化

### T14.1.4.1 · 结构变化，重新入队

- `/own_structure/deferred_row_contract/frozen_column_ids`：changed；上一版 ["PBO", "C1_0PCT_QD"] → 新版 ["PBO", "C1_0PCT_QD", "C0_5PCT_BID"]
- `/own_structure/physical_shape/grid_columns`：changed；上一版 3 → 新版 4
- `/own_structure/physical_shape/physical_cells`：changed；上一版 9 → 新版 12

### T14.1.4.2 · 上游变化，原决定失效并重新确认

- `/dependencies`：changed；上一版 [{"effective_structure_sha256": "c639fbb649c3888ed58277e7dbba3083ada12437d19f42e1115d6fc8b8800df9", "entity_id": "T14.1.4.1"}] → 新版 [{"effective_structure_sha256": "3eeca010d2bb5df0399a08b7cf74bcd8f69eaf07a34a992a0aaea34b83da6804", "entity_id": "T14.1.4.1"}]

任何结构或依赖变化都不会静默沿用旧决定。
