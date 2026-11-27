"""File-based entry points to the existing reviewed composition and TFL procedures."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
from .io_utils import atomic_write_json, load_data
from .errors import ConfigurationError


def register(sub):
    compose=sub.add_parser("compose-csr",help="从审核材料清单执行严格双版本合卷")
    compose.add_argument("--manifest",type=Path,required=True)
    for name, fields in (("tfl-result-apply-docx",("shell-datapack","frozen-map","result")),
                         ("tfl-drift-decide",("queue","decisions","shell-datapack","frozen-map","result"))):
        parser=sub.add_parser(name)
        for field in fields:
            parser.add_argument("--"+field,type=Path,required=True)
        if name.endswith("docx"):
            parser.add_argument("--expected-sha256",required=True)
        parser.add_argument("--output-dir",type=Path,required=True)
    compose.add_argument("--output-dir",type=Path,required=True)


def run(args):
    dest=args.output_dir.resolve()
    if dest.exists() or args.output_dir.is_symlink():
        raise ConfigurationError("请使用新的输出目录")
    dest.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=dest.parent) as tmp:
        out=Path(tmp)/"output"
        out.mkdir()
        if args.command=="compose-csr":
            from .unified_orchestrator import compose_unified_csr
            payload=load_data(args.manifest)
            allowed={"routing","deterministic_bundle","review_decisions","study_facts",
                     "publication_reference_map","ordinary_artifacts","constrained_artifacts"}
            if not isinstance(payload,dict) or set(payload)-allowed:
                raise ConfigurationError("严格合卷清单字段不正确")
            required = {"routing", "deterministic_bundle", "review_decisions", "study_facts"}
            if not required <= set(payload):
                raise ConfigurationError("缺少合卷清单字段：" + "、".join(sorted(required - set(payload))))
            payload.setdefault("publication_reference_map", None)
            base=args.manifest.resolve().parent
            for key,value in list(payload.items()):
                if isinstance(value,str):
                    payload[key]=load_data(base/value)
            for field in ("ordinary_artifacts","constrained_artifacts"):
                for entry in payload.get(field,[]):
                    for key,value in list(entry.items()):
                        if key.endswith("_path"):
                            entry[key]=str((base/value).resolve())
            report=compose_unified_csr(**payload,annotated_output=out/"annotated.docx",clean_output=out/"clean.docx")
            # Paths of the produced documents must remain usable after the staged publish.
            for lane in report.get("outputs",{}).values():
                if isinstance(lane,dict) and "path" in lane:
                    lane["path"]=str(dest/Path(lane["path"]).name)
            atomic_write_json(out/"composition.json", report)
        elif args.command=="tfl-result-apply-docx":
            from .docx_result import apply_docx_result_to_frozen_shell
            pack,report=apply_docx_result_to_frozen_shell(load_data(args.shell_datapack),load_data(args.frozen_map),
                                                          args.result,expected_sha256=args.expected_sha256)
            atomic_write_json(out/"datapack.json",pack)
            atomic_write_json(out/"match.json",report)
        else:
            from .tfl_drift_decisions import reconcile_expected_drift
            record=reconcile_expected_drift(load_data(args.queue),load_data(args.decisions),load_data(args.shell_datapack),
                                            load_data(args.frozen_map),args.result)
            atomic_write_json(out/"reconciliation.json",record)
        dest.mkdir()
        try:
            os.replace(out,dest)
        except OSError:
            dest.rmdir()
            raise
    return 0
