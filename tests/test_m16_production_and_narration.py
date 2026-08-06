from __future__ import annotations

import copy
import re
import unittest

from csr_writer.constrained_narration import (
    ConstrainedNarrationError,
    emit_fact_card_envelope,
    ingest_constrained_draft,
    render_verbatim_fact_card,
)
from csr_writer.production_methods import (
    ProductionMethodError,
    assert_llm_drafting_allowed,
    audit_authoring_unit_coverage,
    load_method_extraction_contracts,
    load_production_method_registry,
)
from csr_writer.writing_style import style_report


SOURCE_HASH = "a" * 64


def _policy(*, medical: bool = False) -> dict:
    return {
        "outside_fact_content": "hard_fail",
        "database_numeric_policy": "placeholders_only",
        "missing_required_fact": "hard_fail",
        "medical_review_marker_required": medical,
        "controlled_causality_terms": [
            "明确相关",
            "很可能有关",
            "可能有关",
            "不太可能有关",
            "无关",
            "未评价",
            "无法评价",
        ],
    }


def _source(source_id: str = "protocol_v1") -> dict:
    return {
        "source_id": source_id,
        "file_role": "protocol",
        "version": "synthetic-v1",
        "locator": "脱敏方案对应章节",
        "content_sha256": SOURCE_HASH,
    }


def _fact(
    fact_id: str,
    label: str,
    value,
    *,
    source_id: str | None = "protocol_v1",
    zone: str = "method",
    value_kind: str = "verbatim_text",
    source_kind: str = "protocol",
    verbatim: bool = True,
    required_terms: list[str] | None = None,
    entity_type: str | None = None,
    missing_behavior: str = "hard_fail",
    gap: str | None = None,
) -> dict:
    return {
        "fact_id": fact_id,
        "label": label,
        "zone": zone,
        "value": value,
        "value_kind": value_kind,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_locator": "脱敏来源定位" if source_id else None,
        "required": True,
        "verbatim_required": verbatim,
        "required_terms": list(required_terms or []),
        "entity_type": entity_type,
        "missing_behavior": missing_behavior,
        "fixed_gap_sentence": gap,
    }


def randomization_card() -> dict:
    values = [
        ("随机化方式", "中央随机化"),
        ("分配比", "等比例"),
        ("分层因素", ["研究中心", "基线严重程度"]),
        ("区组长度", "由独立统计师保密保存"),
        ("随机号生成方", "独立统计人员"),
        ("随机系统", "交互式网络应答系统"),
        ("分配隐藏方法", "授权人员登录后按顺序取得分配结果"),
    ]
    facts = [
        _fact(f"V{index:02d}", label, value)
        for index, (label, value) in enumerate(values, start=1)
    ]
    facts.append(
        _fact(
            "S01",
            "随机化实施流程",
            "随机化清单由独立统计人员生成，授权人员通过系统实施分配。",
            value_kind="text",
            verbatim=False,
            required_terms=["随机化", "授权人员"],
        )
    )
    surface_items = [
        (sentence, list(paragraph["fact_ids"]))
        for paragraph in randomization_draft()["paragraphs"]
        for sentence in re.findall(r"[^。！？]+[。！？]?", paragraph["text"])
        if sentence.strip()
    ]
    authorized_surface_forms = [
        {
            "surface_id": f"SF-9-4-3-{index:02d}",
            "text": sentence.strip(),
            "fact_ids": fact_ids,
            "review_status": "confirmed",
            "reviewed_by_role": "protocol_fact_reviewer",
            "review_record_id": f"TEST-SURFACE-9.4.3-{index:02d}",
            "basis": "脱敏测试事实的已审成文表述",
        }
        for index, (sentence, fact_ids) in enumerate(surface_items, start=1)
    ]
    return {
        "schema_version": "1.1",
        "card_id": "M16-METHOD-9-4-3",
        "section_id": "9.4.3",
        "title": "9.4.3 患者的治疗组分配方法",
        "production_method": "extraction",
        "narration": "constrained_llm",
        "source_manifest": [
            _source(),
            {
                "source_id": "sap_v1",
                "file_role": "sap",
                "version": "synthetic-v1",
                "locator": "脱敏 SAP 对应章节",
                "content_sha256": "d" * 64,
            },
        ],
        "facts": facts,
        "authorized_surface_forms": authorized_surface_forms,
        "controlled_entities": [],
        "policy": _policy(),
    }


def randomization_draft() -> dict:
    return {
        "paragraphs": [
            {
                "text": (
                    "本研究采用中央随机化，并按等比例分配。"
                    "分层因素为研究中心和基线严重程度。"
                    "随机化清单由独立统计人员生成，授权人员通过系统实施分配。"
                ),
                "fact_ids": ["V01", "V02", "V03", "S01"],
            },
            {
                "text": "区组长度由独立统计师保密保存；随机号生成方为独立统计人员。",
                "fact_ids": ["V04", "V05"],
            },
            {
                "text": (
                    "随机系统为交互式网络应答系统。"
                    "分配隐藏方法为授权人员登录后按顺序取得分配结果。"
                ),
                "fact_ids": ["V06", "V07"],
            },
        ]
    }


def sae_card(*, clinical_course: bool = True, causality: str = "可能有关") -> dict:
    db_source = {
        "source_id": "safety_db",
        "file_role": "frozen_safety_dataset",
        "version": "synthetic-cut",
        "locator": "脱敏 SAE 记录",
        "content_sha256": "b" * 64,
    }
    crf_source = {
        "source_id": "crf_text",
        "file_role": "crf_or_medical_record_free_text",
        "version": "synthetic-reviewed",
        "locator": "脱敏 CRF 临床经过",
        "content_sha256": "c" * 64,
    }
    facts = [
        _fact("A01", "受试者标识", "受试者甲", source_id="safety_db", zone="A", source_kind="confirmed_registry", entity_type="subject_identity"),
        _fact("A02", "相关病史", "既往有季节性过敏史", source_id="safety_db", zone="A", source_kind="confirmed_registry", entity_type="medical_history"),
        _fact("A03", "研究治疗暴露", "{{T16.3.1|POP=SS|ROW=SUBJ_A/EXPOSURE|COL=DOSE}}", source_id="safety_db", zone="A", value_kind="placeholder", source_kind="safety_dataset", entity_type="study_exposure"),
        _fact("A04E", "事件术语", "用药部位反应", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="event_term"),
        _fact("A04", "事件时间线", "{{T16.3.1|POP=SS|ROW=SUBJ_A/EVENT|COL=START_DATE}}", source_id="safety_db", zone="A", value_kind="placeholder", source_kind="safety_dataset", entity_type="event_timeline"),
        _fact("A05", "严重程度", "中度", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="severity"),
        _fact("A06", "严重性标准", "需要住院治疗", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="seriousness_criterion"),
        _fact("A07", "合并用药", "接受对症治疗", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="concomitant_medication"),
        _fact("A08", "检查结果", "{{T16.3.1|POP=SS|ROW=SUBJ_A/TEST|COL=RESULT}}", source_id="safety_db", zone="A", value_kind="placeholder", source_kind="safety_dataset", entity_type="test_result"),
        _fact("A09", "对研究治疗采取的措施", "研究治疗暂停", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="action_taken"),
        _fact("A10", "转归", "已恢复", source_id="safety_db", zone="A", source_kind="safety_dataset", entity_type="outcome"),
        _fact("A11", "研究者因果评价", causality, source_id="safety_db", zone="A", value_kind="controlled_term", source_kind="safety_dataset", entity_type="causality"),
        _fact(
            "B01",
            "临床经过",
            "入院后接受对症治疗，症状逐步缓解。" if clinical_course else None,
            source_id="crf_text" if clinical_course else None,
            zone="B",
            source_kind="crf_free_text",
            entity_type="clinical_course",
            missing_behavior="hard_fail" if clinical_course else "fixed_gap_sentence",
            gap=(None if clinical_course else "本例临床经过待源病历/CRF 叙述资料到位后补充"),
        ),
        _fact(
            "C01",
            "申办方医学综合评价",
            None,
            source_id=None,
            zone="C",
            source_kind="human_slot",
            entity_type="sponsor_medical_synthesis",
            missing_behavior="human_slot",
            gap="申办方医学综合评价由医学负责人另行完成。[待医学负责人确认]",
        ),
    ]
    surface_items = [
        (sentence, list(paragraph["fact_ids"]))
        for paragraph in sae_draft(
            clinical_course=clinical_course, causality=causality
        )["paragraphs"]
        for sentence in re.findall(r"[^。！？]+[。！？]?", paragraph["text"])
        if sentence.strip()
    ]
    return {
        "schema_version": "1.1",
        "card_id": "M16-SAE-A",
        "section_id": "12.3.2",
        "title": "12.3.2 SAE 个例叙述（脱敏合成）",
        "production_method": "llm_drafting",
        "narration": "constrained_llm",
        "source_manifest": [db_source, crf_source],
        "facts": facts,
        "authorized_surface_forms": [
            {
                "surface_id": f"SF-12-3-2-{index:02d}",
                "text": sentence.strip(),
                "fact_ids": fact_ids,
                "review_status": "confirmed",
                "reviewed_by_role": "safety_fact_reviewer",
                "review_record_id": f"TEST-SURFACE-12.3.2-{index:02d}",
                "basis": "脱敏 SAE 事实的已审成文表述",
            }
            for index, (sentence, fact_ids) in enumerate(surface_items, start=1)
        ],
        "controlled_entities": [],
        "policy": _policy(medical=True),
    }


def sae_draft(*, clinical_course: bool = True, causality: str = "可能有关") -> dict:
    return {
        "paragraphs": [
            {"text": "受试者甲既往有季节性过敏史。研究治疗暴露为{{T16.3.1|POP=SS|ROW=SUBJ_A/EXPOSURE|COL=DOSE}}。", "fact_ids": ["A01", "A02", "A03"]},
            {"text": "用药部位反应于{{T16.3.1|POP=SS|ROW=SUBJ_A/EVENT|COL=START_DATE}}开始，严重程度为中度，符合需要住院治疗的严重性标准。", "fact_ids": ["A04E", "A04", "A05", "A06"]},
            {"text": "接受对症治疗，检查结果为{{T16.3.1|POP=SS|ROW=SUBJ_A/TEST|COL=RESULT}}。研究治疗暂停。", "fact_ids": ["A07", "A08", "A09"]},
            {"text": f"事件已恢复。研究者判断该事件与研究药物{causality}。", "fact_ids": ["A10", "A11"]},
            {"text": "入院后接受对症治疗，症状逐步缓解。" if clinical_course else "本例临床经过待源病历/CRF 叙述资料到位后补充", "fact_ids": ["B01"]},
            {"text": "申办方医学综合评价由医学负责人另行完成。[待医学负责人确认]", "fact_ids": ["C01"]},
        ]
    }


class M16ProductionMethodTests(unittest.TestCase):
    def test_registry_covers_exactly_120_units_and_32_contracts(self) -> None:
        registry = load_production_method_registry()
        self.assertEqual(120, len(registry["assignments"]))
        self.assertEqual(
            {
                "deterministic_generation": 48,
                "extraction": 29,
                "llm_drafting": 32,
                "mixed": 7,
                "template_slot": 4,
            },
            registry["production_method_counts"],
        )
        self.assertEqual(32, len(load_method_extraction_contracts()["contracts"]))
        audit = audit_authoring_unit_coverage(
            list(registry["assignments"]), registry
        )
        self.assertEqual("pass", audit["status"], audit)

    def test_required_mixed_units_and_nonfree_writer_gate(self) -> None:
        registry = load_production_method_registry()
        required = {"10.1", "10.2", "10.3.1", "10.3.2", "10.3.3", "10.3.4", "10.3.5"}
        self.assertEqual(
            required,
            {
                section_id
                for section_id, item in registry["assignments"].items()
                if item["production_method"] == "mixed"
            },
        )
        for section_id in ("3.1", "5.1", "9.3.1", "9.4.3", "10.1", "12.3.2"):
            with self.subTest(section=section_id), self.assertRaisesRegex(
                ProductionMethodError, "E-PRODUCTION-METHOD-WRITER-BLOCK"
            ):
                assert_llm_drafting_allowed(section_id, registry)
        self.assertEqual(
            "guarded_section_writer",
            assert_llm_drafting_allowed("11.5.4", registry)["narration"],
        )

    def test_sentence_over_60_characters_is_a_hard_failure(self) -> None:
        report = style_report("这是用于验证句长硬阈值的受控句子" * 5 + "。")
        self.assertTrue(report["blocked"])
        self.assertIn(
            "E-STYLE-SENTENCE-LENGTH",
            {item["code"] for item in report["findings"]},
        )


class M16ConstrainedNarrationTests(unittest.TestCase):
    def test_randomization_prose_back_references_every_contract_field(self) -> None:
        artifact = ingest_constrained_draft(
            emit_fact_card_envelope(randomization_card()), randomization_draft()
        )
        self.assertEqual(8, artifact.metadata["field_count"])
        self.assertEqual(100.0, artifact.metadata["field_coverage_percent"])
        self.assertIn("中央随机化", artifact.markdown)

    def test_missing_contract_field_is_a_hard_failure(self) -> None:
        card = randomization_card()
        card["facts"].pop()
        with self.assertRaisesRegex(
            ConstrainedNarrationError, "E-FACT-CARD-MISSING-CONTRACT-FIELD"
        ):
            emit_fact_card_envelope(card)

    def test_card_external_entity_number_and_field_are_hard_failures(self) -> None:
        envelope = emit_fact_card_envelope(randomization_card())
        cases = []
        missing = randomization_draft()
        missing["paragraphs"][0]["fact_ids"].remove("S01")
        cases.append((missing, "E-FACT-CARD-MISSING-FIELD"))
        entity = randomization_draft()
        entity["paragraphs"][0]["text"] += "另使用 UNLISTED_DRUG_X。"
        cases.append((entity, "E-FACT-CARD-EXTRA-ENTITY"))
        number = randomization_draft()
        number["paragraphs"][0]["text"] += "另设 3 个区组。"
        cases.append((number, "E-FACT-CARD-EXTRA-NUMBER"))
        for draft, code in cases:
            with self.subTest(code=code), self.assertRaises(ConstrainedNarrationError) as raised:
                ingest_constrained_draft(envelope, draft)
            self.assertTrue(any(item["code"] == code for item in raised.exception.report["findings"]))

    def test_sae_zones_causality_and_missing_course(self) -> None:
        normal = ingest_constrained_draft(
            emit_fact_card_envelope(sae_card()), sae_draft()
        )
        self.assertEqual({"A": 12, "B": 1, "C": 1}, normal.metadata["zone_distribution"]["counts"])
        self.assertTrue(normal.metadata["causality_control"]["exact_match"])

        missing = ingest_constrained_draft(
            emit_fact_card_envelope(sae_card(clinical_course=False)),
            sae_draft(clinical_course=False),
        )
        self.assertIn("本例临床经过待源病历/CRF 叙述资料到位后补充", missing.markdown)

        with self.assertRaises(ConstrainedNarrationError) as raised:
            ingest_constrained_draft(
                emit_fact_card_envelope(sae_card()),
                sae_draft(causality="明确相关"),
            )
        self.assertTrue(
            any(item["code"] == "E-FACT-CARD-VERBATIM-MISMATCH" for item in raised.exception.report["findings"])
        )

    def test_verbatim_list_never_calls_an_llm(self) -> None:
        contract = load_method_extraction_contracts()["contracts"]["9.3.1"]
        facts = [
            _fact(
                item["field_id"],
                item["label"],
                f"源文原样字段：{item['label']}",
            )
            for item in contract["fact_card_fields"]
        ]
        card = {
            "schema_version": "1.0",
            "card_id": "M16-VERBATIM-9-3-1",
            "section_id": "9.3.1",
            "title": "9.3.1 入选标准",
            "production_method": "extraction",
            "narration": "verbatim_list",
            "source_manifest": [_source()],
            "facts": facts,
            "controlled_entities": [],
            "policy": _policy(),
        }
        artifact = render_verbatim_fact_card(card)
        self.assertEqual(100.0, artifact.metadata["field_coverage_percent"])
        self.assertFalse(artifact.metadata["external_model_api_called"])
        with self.assertRaisesRegex(ConstrainedNarrationError, "E-FACT-CARD-NO-LLM"):
            emit_fact_card_envelope(card)


if __name__ == "__main__":
    unittest.main()
